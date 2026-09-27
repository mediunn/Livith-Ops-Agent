import json

import pytest

from ops_agent.agent.decision import planner
from ops_agent.agent.decision.validation import (
    DecisionValidationError,
    decision_schema,
)
from ops_agent.config import PROJECT_DIR
from ops_agent.persistence.artifacts import read_json
from tests.helpers.agent import decision, evidence


@pytest.mark.parametrize(
    "change",
    [
        {"action": "logs"},
        {"hypotheses": [{"statement": "원인", "evidence_ids": ["unknown"]}]},
        {"limitations": ["  "]},
        {"rationale": "x" * 161},
    ],
)
def test_reject_invalid_model_decision(state, change):
    state["evidence"] = [evidence()]
    with pytest.raises(ValueError):
        planner.validate_decision(json.dumps(decision(**change)), state, [])


def test_no_data_cannot_support_hypothesis(state):
    state["evidence"] = [evidence(status="no_data")]
    result = decision(
        hypotheses=[{"statement": "원인", "evidence_ids": ["current_metrics"]}]
    )
    with pytest.raises(ValueError):
        planner.validate_decision(json.dumps(result), state, [])


@pytest.mark.parametrize("status", ["no_data", "invalid_data", "tool_error"])
def test_schema_and_context_exclude_non_data_hypothesis_ids(state, status):
    state["evidence"] = [evidence(), evidence("logs", status)]
    schema = decision_schema(state, ["warning_logs"])
    items = schema["$defs"]["Hypothesis"]["properties"]["evidence_ids"]["items"]
    assert items["enum"] == ["current_metrics"]
    context = planner.build_context(state, ["warning_logs"])
    assert context["allowed_hypothesis_evidence_ids"] == ["current_metrics"]
    assert context["evidence"][1]["status"] == status
    state["evidence"] = [evidence("logs", status)]
    assert decision_schema(state, [])["properties"]["hypotheses"]["maxItems"] == 0
    # 생성 스키마와 별개로 사후 검사도 우회할 수 없다.
    with pytest.raises(DecisionValidationError) as exc:
        planner.validate_decision(
            json.dumps(
                decision(hypotheses=[{"statement": "가설", "evidence_ids": ["logs"]}])
            ),
            state,
            [],
        )
    assert exc.value.code == "unavailable_evidence"


@pytest.mark.parametrize(
    "content,code",
    [
        ("{}", "schema_error"),
        (json.dumps(decision("logs")), "invalid_action"),
        (
            json.dumps(
                decision(
                    hypotheses=[{"statement": "가설", "evidence_ids": ["missing"]}]
                )
            ),
            "unknown_evidence",
        ),
    ],
)
def test_validation_errors_have_stable_safe_codes(state, content, code):
    with pytest.raises(DecisionValidationError) as exc:
        planner.validate_decision(content, state, [])
    assert exc.value.details()["code"] == code
    assert exc.value.details()["valid"] is False


@pytest.mark.parametrize(
    "case",
    read_json(PROJECT_DIR / "evals/datasets/agent/agent-reference-cases.json")["cases"],
    ids=lambda case: case["case_id"],
)
def test_regression_cases_separate_reference_validity_from_semantic_review(case):
    state = {"evidence": case["evidence"]}
    content = json.dumps(case["decision"])
    with pytest.raises(DecisionValidationError) as exc:
        planner.validate_decision(content, state, case["available_tools"])
    assert exc.value.code == case["expected_claim_validation_error"]
    assert case["semantic_expectation"] == "unsupported_hypothesis"
