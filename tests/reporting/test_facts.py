import copy
import json

import pytest
from ollama import ChatResponse
from pydantic import ValidationError

from ops_agent.evaluation.reports import CASES_PATH, create_evidence, evaluate_record
from ops_agent.reporting import generator as reports
from ops_agent.reporting.validation import (
    Observations,
    expected_observations,
    validate_observations,
)

CASES = json.loads(CASES_PATH.read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_v3_observations_verified_and_fact_text_from_source(
    tmp_path, monkeypatch, case
):
    path = create_evidence(case, tmp_path)

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def chat(self, **kwargs):
            context = json.loads(kwargs["messages"][1]["content"])["context"]
            return ChatResponse(
                done=True,
                done_reason="stop",
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "assessment": case["expected"]["assessments"][0],
                            "observations": context["observed_values"],
                            "hypotheses": [],
                            "limitations": ["서비스 상태 전체를 알 수 없다."],
                            "next_checks": ["HTTP 오류율을 확인한다."],
                        }
                    ),
                },
            )

    monkeypatch.setattr(reports, "Client", FakeClient)
    record = reports.run_report(path, model="test", prompt_version="ops_report_v3")
    assert record["status"] == "generated"
    assert record["validation"]["observation_values"] is True
    assert record["report"]["facts_source"] == "validated_observations_template"
    assert record["semantic_review"] == "pending"
    assert (
        evaluate_record(record, case["expected"])["checks"]["observation_values"]
        is True
    )
    facts = str(record["report"]["facts"])
    if case["id"] == "warning_present":
        assert "1~2" in facts and "초당 요청 수" in facts
    if case["id"] == "no_data":
        assert "샘플이 반환되지 않았다" in facts
    if case["id"] == "invalid_samples":
        assert "유효한 수치가 없어" in facts
    if case["id"] == "truncated_logs":
        assert "잘렸을 수 있어" in facts and "100건" in facts


@pytest.mark.parametrize(
    "mutation",
    [
        "unit",
        "rate_window",
        "step",
        "status",
        "sample_count",
        "value",
        "missing_series",
        "duplicate_series",
        "labels",
        "log_count",
        "log_level",
        "truncation",
        "null_to_zero",
    ],
)
def test_wrong_observations_rejected(tmp_path, mutation):
    case = next(
        c
        for c in CASES
        if c["id"]
        == ("invalid_samples" if mutation == "null_to_zero" else "warning_present")
    )
    expected = expected_observations(
        reports.build_context(create_evidence(case, tmp_path))
    )
    data = expected.model_dump(mode="json")
    m, l = data["metrics"], data["logs"]
    if mutation == "unit":
        m["unit"] = "unknown"
    elif mutation == "rate_window":
        m["rate_window_seconds"] = 60
    elif mutation == "step":
        m["query_step_seconds"] = 300
    elif mutation == "status":
        m["status"] = "no_data"
    elif mutation == "sample_count":
        m["series"][0]["sample_count"] = 999
    elif mutation in {"value", "null_to_zero"}:
        m["series"][0]["min_value"] = 0
    elif mutation == "missing_series":
        m["series"] = []
    elif mutation == "duplicate_series":
        m["series"] *= 2
    elif mutation == "labels":
        m["series"][0]["labels"] = {"api": "wrong"}
    elif mutation == "log_count":
        l["log_count"] = 0
    elif mutation == "log_level":
        l["level_counts"] = {"info": 1}
    else:
        l["possibly_truncated"] = True
    with pytest.raises(ValueError, match="근거와 다른"):
        validate_observations(Observations.model_validate(data), expected)


def test_no_data_cannot_be_changed_to_valid_zero(tmp_path):
    case = next(c for c in CASES if c["id"] == "no_data")
    expected = expected_observations(
        reports.build_context(create_evidence(case, tmp_path))
    )
    data = expected.model_dump()
    data["metrics"]["status"] = "data_available"
    with pytest.raises(ValueError):
        validate_observations(Observations.model_validate(data), expected)


def test_only_negligible_float_representation_difference_allowed(tmp_path):
    case = next(c for c in CASES if c["id"] == "warning_present")
    expected = expected_observations(
        reports.build_context(create_evidence(case, tmp_path))
    )
    data = expected.model_dump()
    data["metrics"]["series"][0]["max_value"] += 1e-13
    validate_observations(Observations.model_validate(data), expected)
    data["metrics"]["series"][0]["max_value"] += 0.001
    with pytest.raises(ValueError):
        validate_observations(Observations.model_validate(data), expected)


def test_zero_cannot_be_changed_to_tiny_positive_value(tmp_path):
    expected = expected_observations(
        reports.build_context(create_evidence(CASES[0], tmp_path))
    )
    data = expected.model_dump()
    data["metrics"]["series"][0]["max_value"] = 1e-15
    with pytest.raises(ValueError):
        validate_observations(Observations.model_validate(data), expected)


def test_boolean_and_infinite_values_rejected(tmp_path):
    original = expected_observations(
        reports.build_context(create_evidence(CASES[0], tmp_path))
    ).model_dump()
    for value in (True, float("inf")):
        data = copy.deepcopy(original)
        data["metrics"]["series"][0]["min_value"] = value
        with pytest.raises(ValidationError):
            Observations.model_validate(data)


def test_invalid_model_output_saved_without_success_report(tmp_path, monkeypatch):
    path = create_evidence(CASES[0], tmp_path)
    data = expected_observations(reports.build_context(path)).model_dump()
    data["logs"]["log_count"] = 999
    response_text = json.dumps(
        {
            "assessment": "insufficient_evidence",
            "observations": data,
            "hypotheses": [],
            "limitations": ["본문 없음"],
            "next_checks": ["본문 확인"],
        }
    )

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def chat(self, **kwargs):
            return ChatResponse(
                done=True,
                done_reason="stop",
                message={"role": "assistant", "content": response_text},
            )

    monkeypatch.setattr(reports, "Client", FakeClient)
    record = reports.run_report(path, model="test", prompt_version="ops_report_v3")
    assert record["status"] == "factual_validation_error"
    assert record["report"] is None
    assert record["response"]["message"]["content"] == response_text
    assert record["validation"]["schema_and_references"] is True
    assert record["validation"]["observation_values"] is False
    assert not evaluate_record(record, CASES[0]["expected"])["automatic_pass"]
