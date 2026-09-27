import asyncio
import json
from pathlib import Path

import pytest
from test_agent import decision, fake_model

from evaluate_agent import CASES_PATH, evaluate_state, run_case, synthetic_observation
from ops_agent.agent.claims import verified_claims
from ops_agent.agent.decision_validation import (
    DecisionValidationError,
    validate_decision,
)
from ops_agent.agent.planner import choose_action
from ops_agent.persistence.artifacts import read_json

CASES = read_json(CASES_PATH)["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_eval_fixture_uses_real_parsers_and_known_claims(state, case):
    # 고정 평가 시각으로 마지막 샘플을 조회 구간 끝에 맞춘다.
    state["window"] = {
        "start": "2026-01-01T00:00:00+00:00",
        "end": "2026-01-01T00:02:00+00:00",
    }
    state["evidence"] = [
        synthetic_observation(state, case, a)
        for a in ("current_metrics", "previous_metrics", "logs", "warning_logs")
    ]
    assert {c["kind"] for c in verified_claims(state)} == set(
        case["expected"]["claim_kinds"]
    )
    assert all(e["source"] == "synthetic_fixture" for e in state["evidence"])


@pytest.mark.parametrize("rationale", ["추가 정보를 얻", "문장이 끝났지만\n두 줄이다."])
def test_rationale_format_failure_is_repaired_and_preserved(
    state, monkeypatch, rationale
):
    calls = fake_model(monkeypatch, state, [decision(rationale=rationale), decision()])
    result = asyncio.run(choose_action(state, []))
    assert result["rationale"] == decision()["rationale"]
    assert len(calls) == 2
    feedback = json.loads(calls[1]["messages"][3]["content"])
    assert feedback["validation_feedback"]["code"] == "rationale_format"
    receipts = [read_json(p) for p in Path(state["directory"]).glob("decision-*.json")]
    failed = next(r for r in receipts if not r["validation"]["valid"])
    assert (
        json.loads(failed["response"]["message"]["content"])["rationale"] == rationale
    )


def test_rationale_boundary_does_not_claim_semantic_validation(state):
    # 형식상 완결처럼 보여도 의미상 끊긴 문장은 이 검사만으로 발견하지 못한다.
    assert validate_decision(
        json.dumps(decision(rationale="추가 정보를 얻.")), state, []
    )
    with pytest.raises(DecisionValidationError) as exc:
        validate_decision(json.dumps(decision(rationale="추가 정보를 얻")), state, [])
    assert exc.value.code == "rationale_format"


def test_full_eval_graph_runs_planner_and_keeps_expected_out_of_input(
    state, monkeypatch
):
    def select(action):
        context = json.loads(calls[-1]["messages"][1]["content"])
        return decision(
            action, claim_ids=[c["claim_id"] for c in context["verified_claims"]]
        )

    calls = fake_model(
        monkeypatch,
        state,
        [
            decision("previous_metrics"),
            lambda: select("logs"),
            lambda: select("finish"),
        ],
    )
    case = next(c for c in CASES if c["id"] == "rate_increase")
    row = asyncio.run(run_case(case, state["model"], 60, "test-suite", 1))
    assert row["evaluation"]["automatic_pass"]
    assert row["actions"] == ["current_metrics", "previous_metrics", "logs"]
    assert row["budget"]["llm_calls"] == 3
    assert row["budget"]["tool_calls"] == 3
    assert row["usage"] == {"input_tokens": 300, "output_tokens": 60}
    assert row["evaluation"]["semantic_review"] == "pending"
    assert (Path(row["directory"]) / "evaluation.sqlite").exists()
    for call in calls:
        context = json.loads(call["messages"][1]["content"])
        assert "expected" not in context
        assert "case_id" not in context
        assert "current_rate" not in context
        assert "warning_query_levels" not in context
        assert all("line" not in e["summary"] for e in context["evidence"])


def test_evaluator_detects_positive_claim_omission_and_early_finish(state):
    state.update(
        decisions=[decision()],
        report={"status": "completed", "hypotheses": [], "verified_claims": []},
    )
    result = evaluate_state(
        state, {"claim_kinds": ["warning_log_observed"], "empty_hypotheses": True}
    )
    assert not result["checks"]["selected_expected_claims"]
    assert not result["checks"]["requested_comparison_and_logs"]
    assert not result["automatic_pass"]


def test_dry_run_calls_neither_model_nor_grafana(state, monkeypatch):
    import evaluate_agent

    def forbidden(*args, **kwargs):
        pytest.fail("dry-run에서 추적·모델 실행을 시도했습니다.")

    monkeypatch.setattr(evaluate_agent, "ReportTrace", forbidden)
    row = asyncio.run(run_case(CASES[0], state["model"], 60, "dry", 1, dry_run=True))
    assert row["status"] == "dry_run"
    assert (
        len(list(Path(row["directory"]).glob("*.json"))) == 5
    )  # 4 observations + budget
