import json

import pytest

from evaluate_reports import CASES_PATH, create_evidence, evaluate_record
from ops_agent.reporting.generator import build_context, metric_semantics

CASES = json.loads(CASES_PATH.read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_synthetic_context_matches_case_without_expected_answers(tmp_path, case):
    context = build_context(create_evidence(case, tmp_path))
    prom, loki = [item["summary"] for item in context["evidence"]]
    assert prom["sample_count"] == len(case["values"])
    expected_status = (
        "invalid_data"
        if case["id"] == "invalid_samples"
        else "data_available"
        if case["values"]
        else "no_data"
    )
    assert prom["status"] == expected_status
    if case["id"] == "zero_rate":
        assert prom["series"][0]["all_zero"] is True
    assert loki["log_count"] == sum(case["log_levels"].values())
    assert loki["level_counts"] == case["log_levels"]
    assert loki["possibly_truncated"] == case["truncated"]
    serialized = json.dumps(context, ensure_ascii=False)
    assert "expected" not in serialized
    assert "review_checks" not in serialized
    for check in case["expected"]["review_checks"]:
        assert check not in serialized


def test_v2_units_do_not_change_observations_or_legacy_context(tmp_path):
    path = create_evidence(CASES[0], tmp_path)
    legacy = build_context(path, enriched=False)
    enriched = build_context(path)
    assert "coverage" not in legacy
    assert "measurement" not in legacy["evidence"][0]
    measurement = enriched["evidence"][0]["measurement"]
    assert measurement["unit"] == "requests_per_second"
    assert measurement["rate_window_seconds"] == 300
    assert measurement["query_step_seconds"] == 60
    for before, after in zip(legacy["evidence"], enriched["evidence"], strict=True):
        assert before["summary"] == after["summary"]
        assert before["arguments"] == after["arguments"]


def test_unknown_query_is_not_mislabeled_as_request_rate():
    metadata = metric_semantics({"expr": "sum(up)", "stepSeconds": 15})
    assert metadata["unit"] == "unknown"
    assert "rate_window_seconds" not in metadata
    assert metadata["query_step_seconds"] == 15


@pytest.mark.parametrize(
    "status,assessment,hypotheses,passed",
    [
        ("generated", "insufficient_evidence", [], True),
        ("generated", "no_issue_observed", [], False),
        ("generated", "insufficient_evidence", [{"statement": "guess"}], False),
        ("validation_error", None, None, False),
        ("generation_error", None, None, False),
    ],
)
def test_automatic_checks_do_not_claim_semantic_accuracy(
    status, assessment, hypotheses, passed
):
    record = {
        "status": status,
        "report": (
            {"assessment": assessment, "hypotheses": hypotheses}
            if status == "generated"
            else None
        ),
    }
    result = evaluate_record(record, CASES[0]["expected"])
    assert result["automatic_pass"] is passed
    assert result["semantic_review"] == "pending"
