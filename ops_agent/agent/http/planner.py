"""같은 HTTP 관측·후보를 받는 규칙 planner와 로컬 모델 planner."""

import asyncio
import json
import time
from pathlib import Path
from uuid import uuid4

from ollama import AsyncClient

from ops_agent.agent.budget import remaining_seconds, reserve
from ops_agent.agent.http.context import validate_choice
from ops_agent.agent.http.state import HTTPDecision
from ops_agent.config import NUM_CTX, NUM_PREDICT, OLLAMA_HOST
from ops_agent.persistence.artifacts import save_json

MAX_INPUT_BYTES = 24000  # 토큰 추정치가 아닌 별도의 직렬화 크기 제한.


async def rule_planner(state: dict, context: dict) -> HTTPDecision:
    """최대 평균 지연이 큰 첫 endpoint의 직전 지연·요청률만 비교하는 기준선."""
    endpoint = context["endpoints"][0]["id"]
    for metric in ("http_mean_latency", "http_request_rate"):
        if any(
            q["endpoint_id"] == endpoint
            and q["metric"] == metric
            and "previous" in q["windows"]
            for q in context["available_queries"]
        ):
            return HTTPDecision(
                action="query",
                endpoint_id=endpoint,
                metric=metric,
                window="previous",
                rationale="최대 평균 지연이 큰 엔드포인트의 직전 구간을 비교한다.",
            )
    return HTTPDecision(
        action="finish", rationale="규칙 기준선의 직전 지연·요청률 비교를 마쳤다."
    )


def decision_schema(context: dict) -> dict:
    schema = HTTPDecision.model_json_schema()
    # 조회 인자 누락을 막되 finish에서는 null을 허용한다.
    schema["required"] = list(schema["properties"])
    schema["properties"]["endpoint_id"]["anyOf"][0]["enum"] = [
        e["id"] for e in context["endpoints"]
    ]
    schema["$defs"]["HTTPHypothesis"]["properties"]["evidence_ids"]["items"]["enum"] = [
        e["evidence_id"] for e in context["observations"]
    ]
    return schema


async def llm_planner(state: dict, context: dict) -> HTTPDecision:
    schema = decision_schema(context)
    messages = [
        {"role": "system", "content": state["prompt"]},
        {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
    ]
    for attempt in (1, 2):
        size = len(
            json.dumps(
                {"messages": messages, "schema": schema}, ensure_ascii=False
            ).encode()
        )
        if size > MAX_INPUT_BYTES:
            raise ValueError("http_planner_input_too_large")
        timeout = min(
            state["limits"].get("llm_seconds", 45.0), remaining_seconds(state)
        )
        reserve(state, "llm")
        receipt = {
            "attempt": attempt,
            "model": state["model"],
            "prompt_sha256": state["prompt_sha256"],
            "messages": messages,
            "output_schema": schema,
            "input_bytes": size,
            "response": None,
            "usage": None,
            "validation_error": None,
            "timeout_seconds": timeout,
            "started_at": time.time(),
        }
        started = time.monotonic()
        feedback = None
        try:
            async with asyncio.timeout(timeout):
                response = await AsyncClient(host=OLLAMA_HOST, timeout=timeout).chat(
                    model=state["model"],
                    messages=messages,
                    format=schema,
                    stream=False,
                    options={
                        "temperature": 0,
                        "num_ctx": NUM_CTX,
                        "num_predict": NUM_PREDICT,
                    },
                )
            receipt["response"] = response.model_dump(mode="json")
            receipt["usage"] = {
                "input_tokens": response.prompt_eval_count,
                "output_tokens": response.eval_count,
            }
            if not response.done or response.done_reason == "length":
                raise RuntimeError("output_truncated")
            content = response.message.content or ""
            try:
                decision = HTTPDecision.model_validate_json(content)
                decision, _ = validate_choice(decision, state)
                return decision
            except ValueError:
                feedback = "schema_or_query_or_evidence_invalid"
                receipt["validation_error"] = feedback
        except BaseException as exc:
            receipt["error_type"] = type(exc).__name__
            raise
        finally:
            receipt["elapsed_seconds"] = time.monotonic() - started
            save_json(
                Path(state["directory"]) / f"decision-{uuid4().hex}.json", receipt
            )
        if attempt == 2:
            raise ValueError(feedback)
        messages = [
            *messages,
            {"role": "assistant", "content": content},
            {
                "role": "user",
                "content": "스키마와 available_queries, 실제 evidence_id에 맞게 수정하세요. "
                "finish는 조회 필드가 모두 null입니다. 새 근거는 없습니다.",
            },
        ]
    raise AssertionError("unreachable")
