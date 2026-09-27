import json

from ops_agent.reporting.agent_report import build_report
from tests.helpers.agent import decision, evidence


def test_report_preserves_no_data_and_excludes_local_paths(state):
    state.update(
        evidence=[evidence(status="no_data"), evidence("logs", "no_data")],
        decisions=[decision(assessment="needs_investigation")],
        stop_reason="model_finished",
    )
    result = build_report(state)
    assert result["assessment"] == "insufficient_evidence"
    assert result["model_assessment"] == "needs_investigation"
    assert result["facts"][0]["status"] == "no_data"
    assert "/must/not" not in json.dumps(result)
    assert "RAW_BODY_MARKER" not in json.dumps(result)


def test_incomplete_report_does_not_reuse_stale_interpretation(state):
    state.update(
        evidence=[evidence(), evidence("previous_metrics")],
        decisions=[
            decision(
                "previous_metrics",
                limitations=["아직 직전 구간 미조회"],
                next_checks=["직전 구간을 조회한다"],
            )
        ],
        stop_reason="decision_error",
    )
    report = build_report(state)
    assert report["model_assessment"] is None
    assert "아직 직전 구간 미조회" not in report["limitations"]
    assert "직전 구간을 조회한다" not in report["next_checks"]
    assert report["decisions"] == state["decisions"]
