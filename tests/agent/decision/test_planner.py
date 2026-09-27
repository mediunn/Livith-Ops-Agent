import asyncio
import json
import time
from pathlib import Path

import pytest
from ollama import ChatResponse

from ops_agent.agent import nodes
from ops_agent.agent.budget import (
    BudgetExceeded,
    read_budget,
)
from ops_agent.agent.decision import planner
from ops_agent.config import NUM_CTX, TOKEN_RESERVATION
from ops_agent.persistence.artifacts import read_json
from ops_agent.reporting.agent_report import build_report
from ops_agent.tools.grafana import query_spec
from tests.helpers.agent import (
    bad_reference,
    decision,
    evidence,
    fake_model,
)


def test_model_context_excludes_raw_logs_and_paths(state):
    state["evidence"] = [evidence()]
    context = json.dumps(planner.build_context(state, ["logs"]))
    assert "RAW_BODY_MARKER" not in context
    assert "/must/not" not in context


def test_context_limit_before_call_or_reservation(state):
    state["prompt"] = "x" * NUM_CTX
    with pytest.raises(planner.ContextTooLarge):
        asyncio.run(planner.choose_action(state, []))
    assert read_budget(state)["llm_calls"] == 0


@pytest.mark.parametrize("failure", [None, "length", "invalid", "timeout"])
def test_model_response_and_failure_receipts(state, monkeypatch, failure):
    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def chat(self, **kwargs):
            assert kwargs["format"]["properties"]["action"]["enum"] == ["finish"]
            if failure == "timeout":
                raise TimeoutError
            return ChatResponse(
                model=state["model"],
                done=True,
                done_reason="length" if failure == "length" else "stop",
                message={
                    "role": "assistant",
                    "content": "{}" if failure == "invalid" else json.dumps(decision()),
                },
                prompt_eval_count=123,
                eval_count=45,
            )

    monkeypatch.setattr(planner, "AsyncClient", FakeClient)
    if failure:
        with pytest.raises((TimeoutError, ValueError)):
            asyncio.run(planner.choose_action(state, []))
    else:
        assert asyncio.run(planner.choose_action(state, []))["action"] == "finish"
    receipts = list(Path(state["directory"]).glob("decision-*.json"))
    attempts = 2 if failure == "invalid" else 1
    assert len(receipts) == attempts
    receipt = read_json(receipts[0])
    assert bool(receipt["error_type"]) == bool(failure)
    assert read_budget(state)["llm_calls"] == attempts
    if failure != "timeout":
        assert receipt["usage"]["input_tokens"] == 123


def test_one_repair_preserves_both_responses_and_charges_budget(state, monkeypatch):
    state["evidence"] = [evidence(), evidence("logs", "no_data")]
    calls = fake_model(monkeypatch, state, [bad_reference(), decision()])
    result = asyncio.run(planner.choose_action(state, []))
    assert result["hypotheses"] == []
    assert len(calls) == 2
    assert calls[1]["messages"][2]["role"] == "assistant"
    feedback = json.loads(calls[1]["messages"][3]["content"])
    assert feedback["validation_feedback"]["code"] == "unavailable_evidence"
    assert feedback["allowed_hypothesis_evidence_ids"] == ["current_metrics"]
    receipts = sorted(
        [read_json(p) for p in Path(state["directory"]).glob("decision-*.json")],
        key=lambda r: r["attempt"],
    )
    first, second = receipts
    assert first["validation"]["code"] == "unavailable_evidence"
    assert second["validation"] == {"valid": True, "code": "ok"}
    assert first["decision_id"] == second["decision_id"]
    assert second["repair_of"] == first["attempt_id"]
    assert json.loads(first["response"]["message"]["content"]) == bad_reference()
    assert read_budget(state)["llm_calls"] == 2
    assert read_budget(state)["reserved_tokens"] == 2 * TOKEN_RESERVATION
    assert read_budget(state)["tool_calls"] == 0


def test_failed_repair_terminates_with_diagnostic_in_report(state, monkeypatch):
    state["evidence"] = [evidence(), evidence("logs", "no_data")]
    for item in state["evidence"]:
        item["tool"], item["arguments"] = query_spec(state, item["action"])
    calls = fake_model(monkeypatch, state, [bad_reference(), bad_reference()])
    update = asyncio.run(nodes.AgentNodes().decide(state))
    assert len(calls) == 2
    assert update["stop_reason"] == "decision_error"
    assert update["decision_error"]["code"] == "unavailable_evidence"
    state.update(update)
    report = build_report(state)
    assert report["status"] == "incomplete"
    assert report["decision_error"]["code"] == "unavailable_evidence"
    assert report["hypotheses"] == []


@pytest.mark.parametrize("constraint", ["llm", "tokens", "time", "context"])
def test_repair_obeys_existing_budgets_and_size_limit(state, monkeypatch, constraint):
    if constraint == "llm":
        state["limits"]["llm_calls"] = 1
    if constraint == "tokens":
        state["limits"]["reserved_tokens"] = TOKEN_RESERVATION

    def first_response():
        if constraint == "time":
            state["deadline"] = time.time() - 1
        if constraint == "context":
            return {"unexpected": "x" * NUM_CTX}
        return bad_reference()

    calls = fake_model(monkeypatch, state, [first_response])
    expected = planner.ContextTooLarge if constraint == "context" else BudgetExceeded
    with pytest.raises(expected):
        asyncio.run(planner.choose_action(state, []))
    assert len(calls) == 1
    assert read_budget(state)["llm_calls"] == 1
    assert len(list(Path(state["directory"]).glob("decision-*.json"))) == 1


def test_saved_prompt_version_is_not_relabelled_on_resume(state, monkeypatch):
    state.pop("prompt_version")  # 이전 v3 체크포인트의 ops_agent_v2 프롬프트
    fake_model(monkeypatch, state, [decision()])
    asyncio.run(planner.choose_action(state, []))
    receipt = read_json(next(Path(state["directory"]).glob("decision-*.json")))
    assert receipt["prompt_version"] == "ops_agent_v2"
