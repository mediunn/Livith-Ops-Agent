"""관측 주장 정책: 양성 사례와 잘못된 비교·인용을 함께 검증한다."""

import asyncio
import copy
import json
from datetime import datetime

import pytest
from test_agent import decision, fake_model

from ops_agent.agent.claims import CLAIM_POLICY_VERSION, verified_claims
from ops_agent.agent.decision_validation import (
    DecisionValidationError,
    decision_schema,
    validate_decision,
)
from ops_agent.agent.planner import build_context, choose_action
from ops_agent.reporting.agent_report import build_report
from ops_agent.tools.grafana import measurement_for, query_spec


def observation(state, action, *, value=1, levels=None):
    tool, arguments = query_spec(state, action)
    if tool == "query_prometheus":
        summary = {
            "status": "data_available",
            "series": [
                {
                    "labels": {"api": "kakao"},
                    "sample_count": 1,
                    "valid_sample_count": 1,
                    "invalid_sample_count": 0,
                    "latest_value": value,
                    "last_timestamp": datetime.fromisoformat(
                        arguments["endTime"]
                    ).timestamp(),
                }
            ],
        }
    else:
        levels = levels if levels is not None else {"warn": 2, "info": 1}
        summary = {
            "status": "data_available",
            "scope": "returned_logs_only",
            "log_count": sum(levels.values()),
            "level_counts": levels,
            "possibly_truncated": True,
        }
    return {
        "action": action,
        "evidence_id": action,
        "tool": tool,
        "arguments": arguments,
        "measurement": measurement_for(tool),
        "status": "data_available",
        "summary": summary,
        "error_type": None,
    }


def metrics(state, previous=1, current=2):
    state["evidence"] = [
        observation(state, "previous_metrics", value=previous),
        observation(state, "current_metrics", value=current),
    ]
    return state["evidence"]


def test_positive_log_claim_is_limited_to_returned_levels(state):
    state["evidence"] = [
        observation(state, "warning_logs", levels={"WARN": 2, "error": 1, "info": 3})
    ]
    (claim,) = verified_claims(state)
    assert claim["kind"] == "warning_log_observed"
    assert claim["warning_level_count"] == 3
    assert claim["returned_log_count"] == 6
    assert claim["scope"] == "returned_logs_only"
    assert claim["evidence_ids"] == ["warning_logs"]
    assert claim == verified_claims(copy.deepcopy(state))[0]


@pytest.mark.parametrize(
    "change",
    [
        {"level_counts": {"info": 2, "unknown": 1}},
        {"level_counts": {"warn": True, "info": 2}},
        {"level_counts": {"warn": -1, "info": 4}},
        {"level_counts": {"warn": 3.0}},
        {"log_count": 4},
        {"log_count": 0},
        {"scope": "all_logs"},
    ],
)
def test_regex_match_or_inconsistent_counts_do_not_prove_warning(state, change):
    item = observation(state, "warning_logs")
    item["summary"].update(change)
    state["evidence"] = [item]
    assert verified_claims(state) == []


@pytest.mark.parametrize(
    "previous,current,expected",
    [(1, 2, True), (0, 0.5, True), (0, 0, False), (2, 1, False), (1, 1, False)],
)
def test_only_endpoint_increase_is_claimed(state, previous, current, expected):
    metrics(state, previous, current)
    claims = verified_claims(state)
    assert bool(claims) == expected
    if expected:
        (claim,) = claims
        assert claim["previous_value"] == previous
        assert claim["current_value"] == current
        assert claim["scope"] == "query_window_endpoints_only"
        assert claim["evidence_ids"] == ["previous_metrics", "current_metrics"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("latest_value", float("nan")),
        ("latest_value", float("inf")),
        ("latest_value", -1),
        ("latest_value", True),
        ("last_timestamp", 0),
        ("invalid_sample_count", 1),
        ("valid_sample_count", 0),
        ("sample_count", 2),
        ("labels", {"api": "different"}),
        ("labels", {"api": "kakao", "instance": "other"}),
    ],
)
def test_uncomparable_series_produces_no_claim(state, field, value):
    before, _ = metrics(state)
    before["summary"]["series"][0][field] = value
    assert verified_claims(state) == []


@pytest.mark.parametrize(
    "section,field,value",
    [
        ("arguments", "datasourceUid", "other"),
        ("arguments", "expr", "other"),
        ("arguments", "startTime", "2000-01-01T00:00:00+00:00"),
        ("arguments", "stepSeconds", 300),
        ("measurement", "unit", "count"),
        ("measurement", "rate_window_seconds", 60),
        ("summary", "status", "invalid_data"),
    ],
)
def test_wrong_query_window_or_measurement_produces_no_claim(
    state, section, field, value
):
    before, _ = metrics(state)
    before[section][field] = value
    assert verified_claims(state) == []


def test_missing_previous_and_duplicate_labels_are_not_compared(state):
    before, now = metrics(state)
    state["evidence"] = [now]
    assert verified_claims(state) == []
    state["evidence"] = [before, now]
    now["summary"]["series"] *= 2
    assert verified_claims(state) == []


def test_no_data_and_out_of_window_logs_are_not_positive_signals(state):
    item = observation(state, "logs")
    state["evidence"] = [item]
    item["status"] = "no_data"
    assert verified_claims(state) == []
    item["status"] = "data_available"
    item["arguments"]["endRfc3339"] = "2000-01-01T00:00:00+00:00"
    assert verified_claims(state) == []


def test_claim_selection_schema_and_reference_validation(state):
    metrics(state)
    (claim,) = verified_claims(state)
    schema = decision_schema(state, [])
    assert schema["properties"]["claim_ids"]["items"]["enum"] == [claim["claim_id"]]
    selected = decision(
        claim_ids=[claim["claim_id"]],
        hypotheses=[
            {
                "statement": "호출 수요 변화 가능성: 원인 미확인",
                "evidence_ids": claim["evidence_ids"],
            }
        ],
    )
    assert validate_decision(json.dumps(selected), state, []) == selected
    selected["claim_ids"] = ["invented"]
    with pytest.raises(DecisionValidationError) as exc:
        validate_decision(json.dumps(selected), state, [])
    assert exc.value.code == "unknown_claim"
    selected["claim_ids"] = []
    with pytest.raises(DecisionValidationError) as exc:
        validate_decision(json.dumps(selected), state, [])
    assert exc.value.code == "unsupported_hypothesis"


def test_selected_claim_cannot_justify_unrelated_evidence(state):
    metrics(state)
    state["evidence"].append(observation(state, "logs"))
    claim = next(
        c for c in verified_claims(state) if c["kind"] == "warning_log_observed"
    )
    output = decision(
        claim_ids=[claim["claim_id"]],
        hypotheses=[
            {
                "statement": "관측 밖의 인용",
                "evidence_ids": ["current_metrics"],
            }
        ],
    )
    with pytest.raises(DecisionValidationError) as exc:
        validate_decision(json.dumps(output), state, [])
    assert exc.value.code == "unsupported_hypothesis"


def test_zero_rates_with_no_logs_repair_unsupported_hypothesis(state, monkeypatch):
    metrics(state, 0, 0)
    item = observation(state, "logs", levels={})
    item["status"] = item["summary"]["status"] = "no_data"
    state["evidence"].append(item)
    bad = decision(
        hypotheses=[
            {"statement": "경고 로그가 존재한다", "evidence_ids": ["current_metrics"]}
        ]
    )
    calls = fake_model(monkeypatch, state, [bad, decision()])
    result = asyncio.run(choose_action(state, []))
    assert result["claim_ids"] == result["hypotheses"] == []
    assert len(calls) == 2
    feedback = json.loads(calls[1]["messages"][3]["content"])
    assert feedback["validation_feedback"]["code"] == "unsupported_hypothesis"
    assert feedback["verified_claims"] == []
    assert calls[0]["format"]["properties"]["hypotheses"]["maxItems"] == 0


def test_report_observations_are_computed_but_causal_text_stays_unverified(state):
    metrics(state)
    (claim,) = verified_claims(state)
    state.update(
        decisions=[decision(claim_ids=[claim["claim_id"]])],
        stop_reason="model_finished",
    )
    report = build_report(state)
    assert report["verified_claims"] == [claim]
    assert report["claim_policy_version"] == CLAIM_POLICY_VERSION
    assert report["report_version"] == "agent-report-v3"
    assert report["semantic_review"] == "pending"
    context = build_context(state, [])
    assert context["verified_claims"] == [claim]
    assert context["claim_policy_version"] == CLAIM_POLICY_VERSION
    state["stop_reason"] = "decision_error"
    assert build_report(state)["verified_claims"] == []
