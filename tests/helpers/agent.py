import json

from ollama import ChatResponse

from ops_agent.agent import nodes
from ops_agent.agent.budget import (
    reserve,
)
from ops_agent.agent.decision import planner
from ops_agent.tools import grafana as tools


def decision(action="finish", **updates):
    return {
        "action": action,
        "rationale": "관측 근거를 추가 확인한다.",
        "assessment": "insufficient_evidence",
        "hypotheses": [],
        "limitations": ["서비스 정상 기준 없음"],
        "next_checks": ["HTTP 지표 확인"],
        **updates,
    }


def evidence(action="current_metrics", status="data_available"):
    return {
        "action": action,
        "evidence_id": action,
        "source": "synthetic",
        "tool": "query_prometheus" if "metrics" in action else "query_loki_logs",
        "arguments": {},
        "measurement": {},
        "status": status,
        "summary": {},
        "error_type": "TestError" if status == "tool_error" else None,
        "artifact": "/must/not/be/sent/to/langfuse.json",
        "response": {"raw_log": "RAW_BODY_MARKER"},
    }


def mock_graph(monkeypatch, calls):
    async def collect(state, trace=None):
        reserve(state, "tool")
        calls.append(state["action"])
        result = evidence(state["action"])
        result["tool"], result["arguments"] = tools.query_spec(state, state["action"])
        return result

    async def choose(state, available, trace=None):
        reserve(state, "llm")
        return decision("logs" if "logs" in available else "finish")

    monkeypatch.setattr(nodes, "execute_tool", collect)
    monkeypatch.setattr(nodes, "choose_action", choose)


def fake_model(monkeypatch, state, outputs):
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def chat(self, **kwargs):
            calls.append(kwargs)
            value = outputs[len(calls) - 1]
            if callable(value):
                value = value()
            return ChatResponse(
                model=state["model"],
                done=True,
                done_reason="stop",
                message={"role": "assistant", "content": json.dumps(value)},
                prompt_eval_count=100,
                eval_count=20,
            )

    monkeypatch.setattr(planner, "AsyncClient", Client)
    return calls


def bad_reference():
    return decision(hypotheses=[{"statement": "가설", "evidence_ids": ["logs"]}])
