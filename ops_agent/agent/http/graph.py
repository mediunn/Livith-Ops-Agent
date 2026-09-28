import asyncio
import hashlib
import time
from pathlib import Path

from langgraph.graph import END, START, StateGraph

from ops_agent.agent.budget import (
    BudgetExceeded,
    initialize_budget,
    read_budget,
    remaining_seconds,
)
from ops_agent.agent.http.context import (
    context_and_queries,
    identity,
    initial_queries,
    validate_choice,
)
from ops_agent.agent.http.state import CONTRACT_VERSION, HTTPAgentState
from ops_agent.config import HTTP_AGENT_PROMPT_PATH, TOKEN_RESERVATION
from ops_agent.tools.http import HTTPQuery, execute_http_query, prepare_query


def initial_state(
    directory: Path,
    scope: HTTPQuery,
    *,
    symptom: str,
    planner_kind="llm",
    model="qwen2.5:3b",
    seconds=180,
    tool_calls=6,
    llm_seconds=45,
) -> dict:
    scope = HTTPQuery.model_validate(scope.model_dump())
    if not symptom.strip() or len(symptom) > 1000:
        raise ValueError("증상은 1~1000자여야 합니다.")
    if (
        planner_kind not in {"llm", "rules"}
        or not model.strip()
        or model.endswith("-cloud")
    ):
        raise ValueError("유효한 planner와 로컬 모델 이름이 필요합니다.")
    if not 1 <= seconds <= 3600 or not 2 <= tool_calls <= 10:
        raise ValueError("시간은 1~3600초, 도구 호출은 2~10회여야 합니다.")
    if not 1 <= llm_seconds <= 180:
        raise ValueError("모델 호출 제한은 1~180초여야 합니다.")
    prompt = HTTP_AGENT_PROMPT_PATH.read_text(encoding="utf-8").strip()
    directory.mkdir(parents=True, exist_ok=False)
    initialize_budget(directory)
    return {
        "contract_version": CONTRACT_VERSION,
        "thread_id": directory.name,
        "directory": str(directory),
        "request": scope.model_dump(mode="json"),
        "symptom": symptom.strip(),
        "planner_kind": planner_kind,
        "model": model,
        "prompt": prompt,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "limits": {
            "tool_calls": tool_calls,
            "llm_calls": 8,
            "llm_seconds": llm_seconds,
            "reserved_tokens": 8 * TOKEN_RESERVATION,
        },
        "remaining_seconds": float(seconds),
        "pending_query": None,
        "evidence": [],
        "decisions": [],
        "stop_reason": "",
        "error_type": None,
        "report": None,
    }


class HTTPNodes:
    def __init__(self, planner, *, tool_executor=execute_http_query):
        self.planner = planner
        self.tool_executor = tool_executor

    async def timed(self, state, operation):
        started = time.monotonic()
        active = {**state, "deadline": time.time() + state["remaining_seconds"]}
        try:
            async with asyncio.timeout(remaining_seconds(active)):
                result = await operation(active)
        except BudgetExceeded as exc:
            result = {"stop_reason": str(exc), "error_type": type(exc).__name__}
        except TimeoutError as exc:
            result = {
                "stop_reason": "time_budget"
                if time.time() >= active["deadline"]
                else "decision_error"
                if operation == self._decide
                else "collection_error",
                "error_type": type(exc).__name__,
            }
        except Exception as exc:  # noqa: BLE001
            result = {
                "stop_reason": "decision_error"
                if operation == self._decide
                else "collection_error",
                "error_type": type(exc).__name__,
            }
        result["remaining_seconds"] = max(
            0.0, state["remaining_seconds"] - (time.monotonic() - started)
        )
        return result

    async def decide(self, state):
        return await self.timed(state, self._decide)

    async def _decide(self, state):
        used = {e["query_key"] for e in state["evidence"]}
        for query in initial_queries(state):
            if identity(query) not in used:
                if read_budget(state)["tool_calls"] >= state["limits"]["tool_calls"]:
                    return {"stop_reason": "tool_budget"}
                return {"pending_query": query.model_dump(mode="json")}
        context, candidates = context_and_queries(state)
        if not candidates:
            return {"stop_reason": "no_candidates"}
        if read_budget(state)["tool_calls"] >= state["limits"]["tool_calls"]:
            return {"stop_reason": "tool_budget"}
        decision, query = validate_choice(await self.planner(state, context), state)
        record = {
            **decision.model_dump(mode="json"),
            "source": state["planner_kind"],
            "query": query.model_dump(mode="json") if query else None,
            "evidence_considered": [e["evidence_id"] for e in state["evidence"]],
        }
        return {
            "decisions": [record],
            "pending_query": record["query"],
            "stop_reason": "planner_finished" if query is None else "",
        }

    async def query(self, state):
        return await self.timed(state, self._query)

    async def _query(self, state):
        query = HTTPQuery.model_validate(state["pending_query"])
        key = identity(query)
        if key in {e["query_key"] for e in state["evidence"]}:
            raise ValueError("duplicate_query")
        allowed = {identity(q) for q in initial_queries(state)}
        _, candidates = context_and_queries(state)
        allowed.update(identity(q) for q in candidates.values())
        if key not in allowed:
            raise ValueError("query_outside_candidates")
        evidence = await self.tool_executor(state, query)
        arguments, measurement = prepare_query(query)
        if (
            evidence.get("query_key") != key
            or evidence.get("arguments") != arguments
            or evidence.get("measurement") != measurement
            or evidence.get("tool") != "query_prometheus"
            or evidence.get("action") != query.metric
            or not evidence.get("evidence_id")
        ):
            raise ValueError("evidence_scope_mismatch")
        return {
            "evidence": [evidence],
            "pending_query": None,
            "stop_reason": ""
            if evidence["status"] in {"data_available", "no_data"}
            else "collection_error",
        }

    def report(self, state):
        last = state["decisions"][-1] if state["decisions"] else None
        return {
            "report": {
                "report_version": CONTRACT_VERSION,
                "thread_id": state["thread_id"],
                "status": "completed"
                if state["stop_reason"] in {"planner_finished", "no_candidates"}
                else "incomplete",
                "stop_reason": state["stop_reason"],
                "error_type": state.get("error_type"),
                "request": state["request"],
                "symptom": state["symptom"],
                "facts_source": "parsed_observations",
                "facts": state["evidence"],
                "decisions": state["decisions"],
                "interpretation": {
                    "source": state["planner_kind"],
                    "semantic_review": "pending",
                    "rationale": last["rationale"] if last else None,
                    "hypotheses": last["hypotheses"] if last else [],
                    "unreviewed_evidence_ids": [
                        e["evidence_id"]
                        for e in state["evidence"]
                        if last is None
                        or e["evidence_id"] not in last["evidence_considered"]
                    ],
                    "hypothesis_status_meaning": "planner의 해석이며 독립적으로 검증된 판정이 아님",
                },
                "budget": read_budget(state),
                "limits": {"llm_seconds": 45, **state["limits"]},
                "remaining_seconds": state["remaining_seconds"],
            }
        }


def build_graph(checkpointer, *, planner, tool_executor=execute_http_query):
    nodes = HTTPNodes(planner, tool_executor=tool_executor)
    builder = StateGraph(HTTPAgentState)
    builder.add_node("decide", nodes.decide)
    builder.add_node("query", nodes.query)
    builder.add_node("report", nodes.report)
    builder.add_edge(START, "decide")
    for source in ("decide", "query"):
        next_node = "query" if source == "decide" else "decide"
        builder.add_conditional_edges(
            source,
            lambda s: "stop" if s["stop_reason"] else "continue",
            {"stop": "report", "continue": next_node},
        )
    builder.add_edge("report", END)
    return builder.compile(checkpointer=checkpointer)
