import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops_agent.agent import nodes
from ops_agent.agent.budget import reserve
from ops_agent.agent.decision.planner import build_context, choose_action
from ops_agent.agent.decision.validation import (
    DecisionValidationError,
    decision_schema,
    validate_decision,
)
from ops_agent.agent.policy import coverage
from ops_agent.agent.request import request_from_args
from ops_agent.persistence.artifacts import read_json
from ops_agent.reporting.agent_report import build_report
from tests.helpers.agent import fake_model
from tests.helpers.observations import observe


def choice(action="finish", claim_ids=None, **extra):
    return {"action": action, "claim_ids": claim_ids or [], **extra}


def test_comparison_contract_is_explicit_and_defaulted():
    default = request_from_args(SimpleNamespace())
    assert default.compare_previous is True
    assert "compare_previous" in default.defaults_applied
    explicit = request_from_args(
        SimpleNamespace(compare_previous=False, symptom="직전 구간 비교")
    )
    assert explicit.compare_previous is False  # 자연어로 명시 설정을 덮어쓰지 않는다.
    assert "compare_previous" not in explicit.defaults_applied
    with pytest.raises(ValueError):
        request_from_args(SimpleNamespace(compare_previous="false"))


@pytest.mark.parametrize("omitted", ["current_metrics", "previous_metrics", "logs"])
def test_each_missing_required_query_blocks_finish(current_state, omitted):
    state = current_state
    observe(
        state,
        [a for a in ("current_metrics", "previous_metrics", "logs") if a != omitted],
    )
    assert coverage(state)["missing"] == [omitted]
    schema = decision_schema(state, ["previous_metrics", "logs", "warning_logs"])
    assert "finish" not in schema["properties"]["action"]["enum"]
    with pytest.raises(DecisionValidationError) as exc:
        validate_decision(json.dumps(choice()), state, [])
    assert exc.value.code == "required_checks_missing"


def test_regex_query_does_not_replace_required_service_logs(current_state):
    observe(current_state, ["current_metrics", "previous_metrics", "warning_logs"])
    assert coverage(current_state)["missing"] == ["logs"]


@pytest.mark.parametrize("mutation", ["window", "tool", "failure"])
def test_coverage_requires_matching_query_and_readable_result(current_state, mutation):
    observe(current_state, ["current_metrics", "previous_metrics", "logs"])
    item = current_state["evidence"][1]
    if mutation == "window":
        item["arguments"]["endTime"] = "2020-01-01T00:00:00Z"
    elif mutation == "tool":
        item["tool"] = "query_loki_logs"
    else:
        item["status"] = "tool_error"
    assert coverage(current_state)["missing"] == ["previous_metrics"]


@pytest.mark.parametrize("status", ["no_data", "invalid_data"])
def test_attempted_readable_query_is_not_health_evidence(current_state, status):
    state = current_state
    observe(state, ["current_metrics", "previous_metrics", "logs"])
    for item in state["evidence"]:
        item["status"] = item["summary"]["status"] = status
    result = validate_decision(json.dumps(choice()), state, [])
    state.update(decisions=[result], stop_reason="model_finished")
    report = build_report(state)
    assert report["status"] == "completed"
    assert report["required_checks"]["missing"] == []
    assert report["health_assessment"] == "not_evaluated"
    assert report["assessment"] == "insufficient_evidence"
    assert any(
        status in s or "데이터" in s or "수치" in s for s in report["limitations"]
    )
    assert report["hypotheses"] == []


def test_comparison_off_removes_previous_action_and_finish_requirement(current_state):
    state = current_state
    state["request"]["compare_previous"] = False
    observe(state, ["current_metrics", "logs"])
    schema = decision_schema(state, ["previous_metrics", "warning_logs"])
    assert schema["properties"]["action"]["enum"] == ["finish"]
    assert validate_decision(json.dumps(choice()), state, [])["action"] == "finish"
    with pytest.raises(DecisionValidationError) as exc:
        validate_decision(
            json.dumps(choice("previous_metrics")), state, ["previous_metrics"]
        )
    assert exc.value.code == "invalid_action"


def test_budget_exhaustion_with_missing_required_query_is_incomplete(
    current_state, monkeypatch
):
    state = current_state
    observe(state, ["current_metrics"])
    state["limits"]["tool_calls"] = 1
    reserve(state, "tool")

    async def forbidden(*args, **kwargs):
        pytest.fail("필수 조회 예산 부족 상태에서 모델 호출")

    monkeypatch.setattr(nodes, "choose_action", forbidden)
    state.update(asyncio.run(nodes.AgentNodes().decide(state)))
    report = build_report(state)
    assert state["stop_reason"] == "tool_budget"
    assert report["status"] == "incomplete"
    assert set(report["required_checks"]["missing"]) == {"logs", "previous_metrics"}
    assert any("미완료 조회" in s for s in report["next_checks"])


def test_missing_check_cannot_be_hidden_by_forged_finish(current_state):
    observe(current_state, ["current_metrics"])
    current_state.update(stop_reason="model_finished", decisions=[])
    report = build_report(current_state)
    assert report["status"] == "incomplete"
    assert report["stop_reason"] == "required_checks_missing"


def test_finish_repair_keeps_original_attempt_and_required_checks(
    current_state, monkeypatch
):
    state = current_state
    observe(state, ["current_metrics", "logs"])
    calls = fake_model(monkeypatch, state, [choice(), choice("previous_metrics")])
    result = asyncio.run(choose_action(state, ["previous_metrics", "warning_logs"]))
    assert result["action"] == "previous_metrics"
    feedback = json.loads(calls[1]["messages"][3]["content"])
    assert feedback["validation_feedback"]["code"] == "required_checks_missing"
    assert feedback["missing_required_checks"] == ["previous_metrics"]
    assert "finish" not in feedback["allowed_actions"]
    assert len(list(Path(state["directory"]).glob("decision-*.json"))) == 2


@pytest.mark.parametrize(
    "extra",
    [
        {"rationale": "현재 요청률은 정상이다."},
        {"limitations": ["문제 없음"]},
        {"next_checks": ["조치 불필요"]},
        {"hypotheses": [{"statement": "오류 없음", "evidence_ids": ["metric"]}]},
        {"assessment": "healthy"},
    ],
)
def test_free_form_assertions_cannot_enter_current_choice(current_state, extra):
    observe(current_state, ["current_metrics", "previous_metrics", "logs"])
    with pytest.raises(DecisionValidationError) as exc:
        validate_decision(json.dumps(choice(**extra)), current_state, [])
    assert exc.value.code == "schema_error"
    assert set(decision_schema(current_state, [])["properties"]) == {
        "action",
        "claim_ids",
    }


def test_injected_narrative_is_preserved_only_in_receipt(current_state, monkeypatch):
    state = current_state
    observe(state, ["current_metrics", "previous_metrics", "logs"])
    marker = "현재 요청률은 정상이다. MODEL_MARKER"
    fake_model(monkeypatch, state, [choice(rationale=marker), choice()])
    result = asyncio.run(choose_action(state, []))
    state.update(decisions=[result], stop_reason="model_finished")
    report = build_report(state)
    assert "MODEL_MARKER" not in json.dumps(report)
    assert any(
        json.loads(read_json(p)["response"]["message"]["content"]).get("rationale")
        == marker
        for p in Path(state["directory"]).glob("decision-*.json")
    )
    assert report["narrative_source"] == "code_generated"
    assert report["model_assessment"] is None
    assert report["report_version"] == "agent-report-v4"


def test_warning_report_contains_scope_limits_and_concrete_followup(current_state):
    state = current_state
    observe(state, ["current_metrics", "previous_metrics", "logs"], "warning_present")
    context = build_context(state, [])
    ids = [c["claim_id"] for c in context["verified_claims"]]
    result = validate_decision(json.dumps(choice(claim_ids=ids)), state, [])
    state.update(decisions=[result], stop_reason="model_finished")
    report = build_report(state)
    assert report["assessment_source"] == "observation_policy"
    assert report["assessment"] == "needs_investigation"
    assert report["health_assessment"] == "not_evaluated"
    assert report["hypotheses"] == []
    assert any("잘렸을" in s for s in report["limitations"])
    assert any("원문·요청 ID" in s for s in report["next_checks"])
    assert report["required_checks"]["missing"] == []
