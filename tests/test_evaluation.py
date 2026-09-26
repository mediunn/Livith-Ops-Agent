import json

import pytest

from evaluate_reports import CASES_PATH, create_evidence, evaluate_record
from generate_report import build_context

CASES = json.loads(CASES_PATH.read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_synthetic_context_matches_case_without_expected_answers(tmp_path, case):
    context = build_context(create_evidence(case, tmp_path))
    prom, loki = [item["summary"] for item in context["evidence"]]
    assert prom["sample_count"] == len(case["values"])
    assert prom["status"] == ("data_available" if case["values"] else "no_data")
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
