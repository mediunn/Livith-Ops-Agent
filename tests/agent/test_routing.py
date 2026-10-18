import asyncio
import json
import time

import pytest
from ollama import ChatResponse

from ops_agent.agent import routing
from ops_agent.agent.budget import initialize_budget, read_budget
from ops_agent.config import TOKEN_RESERVATION
from ops_agent.persistence.artifacts import read_json


@pytest.fixture
def routing_state(tmp_path):
    initialize_budget(tmp_path)
    return {
        "directory": str(tmp_path),
        "model": "test-local",
        "deadline": time.time() + 30,
        "limits": {
            "tool_calls": 0,
            "llm_calls": 2,
            "reserved_tokens": 2 * TOKEN_RESERVATION,
        },
    }


def install_model(monkeypatch, responses):
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def chat(self, **kwargs):
            calls.append(kwargs)
            value = responses[len(calls) - 1]
            if isinstance(value, BaseException):
                raise value
            if isinstance(value, ChatResponse):
                return value
            return ChatResponse(
                model="test-local",
                done=True,
                done_reason="stop",
                message={"role": "assistant", "content": json.dumps(value)},
                prompt_eval_count=100,
                eval_count=30,
            )

    monkeypatch.setattr(routing, "AsyncClient", Client)
    return calls


@pytest.mark.parametrize(
    "payload,question",
    [
        (
            {
                "action": "proceed",
                "domain": "http",
                "purpose": "compare",
                "route": "/checkout",
                "lookback_minutes": 30,
            },
            "/checkout이 직전 30분보다 느려졌어?",
        ),
        (
            {
                "action": "proceed",
                "domain": "external_api",
                "purpose": "lookup",
                "target_text": "LastFM",
            },
            "LastFM 요청률 확인해줘",
        ),
        (
            {
                "action": "clarify",
                "question": "서비스 HTTP와 외부 API 중 어느 것을 확인할까요?",
            },
            "API 상태 알려줘",
        ),
        (
            {
                "action": "unsupported",
                "unsupported_reason": "현재 p95 조회는 지원하지 않습니다.",
            },
            "p95 확인해줘",
        ),
    ],
)
def test_routing_uses_llm_and_records_budget_without_tools(
    routing_state, tmp_path, monkeypatch, payload, question
):
    calls = install_model(monkeypatch, [payload])
    plan = asyncio.run(routing.route_request(routing_state, question, {}))
    assert plan.action == payload["action"]
    assert len(calls) == 1
    assert read_budget(routing_state)["tool_calls"] == 0
    assert read_budget(routing_state)["llm_calls"] == 1
    receipt = read_json(next(tmp_path.glob("routing-*.json")))
    assert receipt["usage"] == {"input_tokens": 100, "output_tokens": 30}
    assert receipt["prompt_version"] == "routing_v1"


@pytest.mark.parametrize(
    "bad,explicit",
    [
        (
            {
                "action": "proceed",
                "domain": "http",
                "purpose": "lookup",
                "route": "/invented",
            },
            {},
        ),
        (
            {"action": "proceed", "domain": "http", "purpose": "lookup"},
            {"domain": "external_api"},
        ),
        (
            {
                "action": "proceed",
                "domain": "http",
                "purpose": "lookup",
                "target_text": "checkout",
            },
            {},
        ),
        (
            {
                "action": "proceed",
                "domain": "http",
                "purpose": "lookup",
                "time_text": "어제",
            },
            {},
        ),
        (
            {
                "action": "proceed",
                "domain": "external_api",
                "purpose": "lookup",
                "lookback_minutes": 400,
            },
            {},
        ),
    ],
)
def test_invalid_scope_is_repaired_without_guessing_another_domain(
    routing_state, monkeypatch, bad, explicit
):
    calls = install_model(
        monkeypatch,
        [
            bad,
            {"action": "clarify", "question": "조회 대상과 시간 범위를 확인해 주세요."},
        ],
    )
    plan = asyncio.run(routing.route_request(routing_state, "대상 확인", explicit))
    assert plan.action == "clarify"
    assert len(calls) == read_budget(routing_state)["llm_calls"] == 2
    assert read_budget(routing_state)["tool_calls"] == 0


@pytest.mark.parametrize("failure", ["invalid", "timeout", "truncated", "budget"])
def test_routing_failure_never_returns_an_automatic_fallback(
    routing_state, tmp_path, monkeypatch, failure
):
    response = {}
    if failure == "timeout":
        response = TimeoutError()
    elif failure == "truncated":
        response = ChatResponse(
            model="test-local",
            done=True,
            done_reason="length",
            message={"role": "assistant", "content": "{}"},
        )
    elif failure == "budget":
        routing_state["limits"]["llm_calls"] = 0
    calls = install_model(monkeypatch, [response, response])
    with pytest.raises((ValueError, RuntimeError, TimeoutError)):
        asyncio.run(routing.route_request(routing_state, "API 확인", {}))
    assert len(calls) == (
        0 if failure == "budget" else 2 if failure == "invalid" else 1
    )
    assert read_budget(routing_state)["tool_calls"] == 0
    assert len(list(tmp_path.glob("routing-*.json"))) == len(calls)
