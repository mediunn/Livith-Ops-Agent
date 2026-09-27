import json

import pytest
from ollama import ChatResponse

from ops_agent.reporting import generator as report_module


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    # 기존 버전의 동작을 보존하는 회귀 검사. v3는 test_facts에서 검증한다.
    monkeypatch.setattr(report_module, "PROMPT_VERSION", "ops_report_v2")
    monkeypatch.setattr(
        report_module,
        "PROMPT_PATH",
        report_module.PROJECT_DIR / "prompts/report/ops_report_v2.txt",
    )
    monkeypatch.setattr(report_module, "ARTIFACTS_DIR", tmp_path)
    prom = {
        "tool": "query_prometheus",
        "evidence_id": "prom-id",
        "status": "data_available",
        "summary": {"status": "data_available", "sample_count": 1},
        "arguments": {
            "queryType": "range",
            "startTime": "2026-09-26T12:00:00Z",
            "endTime": "2026-09-26T12:30:00Z",
        },
        "response": {"private_raw": "not model input"},
    }
    loki = {
        "tool": "query_loki_logs",
        "evidence_id": "loki-id",
        "related_prometheus_evidence_id": "prom-id",
        "related_prometheus_file": "prometheus-test.json",
        "status": "no_data",
        "summary": {"status": "no_data", "log_count": 0},
        "arguments": {
            "queryType": "range",
            "startRfc3339": "2026-09-26T21:00:00+09:00",
            "endRfc3339": "2026-09-26T21:30:00+09:00",
        },
        "response": {"private_raw": "not model input"},
    }
    (tmp_path / "prometheus-test.json").write_text(json.dumps(prom))
    path = tmp_path / "loki-20260926-test.json"
    path.write_text(json.dumps(loki))
    return path


def report_data(evidence_id="prom-id"):
    return {
        "assessment": "insufficient_evidence",
        "facts": [
            {"statement": "샘플 1개가 반환됐다.", "evidence_ids": [evidence_id]},
            {"statement": "로그가 반환되지 않았다.", "evidence_ids": ["loki-id"]},
        ],
        "hypotheses": [],
        "limitations": ["서비스 전체 상태를 알 수 없다."],
        "next_checks": ["HTTP 오류율을 확인한다."],
    }


def test_context_uses_linked_file_and_excludes_raw(evidence):
    # 더 최신인 무관한 파일 및 라벨 탐색 결과를 선택하면 안 된다.
    (evidence.parent / "prometheus-newer.json").write_text("{}")
    (evidence.parent / "loki-labels-newer.json").write_text("{}")
    context = report_module.build_context(None)
    assert [item["evidence_id"] for item in context["evidence"]] == [
        "prom-id",
        "loki-id",
    ]
    assert context["evidence"][1]["status"] == "no_data"
    assert "private_raw" not in json.dumps(context)


@pytest.mark.parametrize("case", ["reference", "window", "status", "summary"])
def test_invalid_evidence_pair_rejected(evidence, case):
    data = json.loads(evidence.read_text())
    if case == "reference":
        data["related_prometheus_evidence_id"] = "different-id"
    elif case == "window":
        data["arguments"]["endRfc3339"] = "2026-09-26T21:31:00+09:00"
    elif case == "status":
        data["status"] = "tool_error"
    else:
        data["summary"]["status"] = "data_available"
    evidence.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        report_module.build_context(evidence)


@pytest.mark.parametrize(
    "case,expected",
    [
        ("valid", "generated"),
        ("unknown_id", "validation_error"),
        ("missing_source", "validation_error"),
        ("invalid_json", "validation_error"),
        ("truncated", "validation_error"),
        ("connection", "generation_error"),
    ],
)
def test_generation_preserves_success_and_failure(
    evidence, monkeypatch, case, expected
):
    data = report_data("unknown" if case == "unknown_id" else "prom-id")
    if case == "missing_source":
        data["facts"].pop()
    raw = json.dumps(data)
    if case == "invalid_json":
        raw = "not json"

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["host"] == "http://127.0.0.1:11434"

        def chat(self, **kwargs):
            if case == "connection":
                raise ConnectionError("synthetic connection failure")
            return ChatResponse(
                model=kwargs["model"],
                message={"role": "assistant", "content": raw},
                done=True,
                done_reason="length" if case == "truncated" else "stop",
                prompt_eval_count=100,
                eval_count=50,
            )

    monkeypatch.setattr(report_module, "Client", FakeClient)
    code = report_module.generate_report(evidence, model="test-model")
    saved = json.loads(next(evidence.parent.glob("report-*.json")).read_text())
    assert saved["status"] == expected
    assert code == (0 if expected == "generated" else 1)
    assert bool(saved["report"]) == (expected == "generated")
    assert saved["system_prompt"] == report_module.PROMPT_PATH.read_text().strip()
    if case != "connection":
        assert saved["response"]["message"]["content"] == raw
        assert saved["usage"] == {"input_tokens": 100, "output_tokens": 50}


def test_missing_prompt_saved_without_model_call(evidence, monkeypatch):
    monkeypatch.setattr(report_module, "PROMPT_PATH", evidence.parent / "missing.txt")
    assert report_module.generate_report(evidence, model="test-model") == 1
    saved = json.loads(next(evidence.parent.glob("report-*.json")).read_text())
    assert saved["status"] == "prompt_error"
    assert saved["response"] is None


@pytest.mark.parametrize("version", ("ops_report_v1", "ops_report_v2"))
def test_prompt_selection_and_raw_assessment_preserved(evidence, monkeypatch, version):
    # 기대 분류와 달라도 생성 단계에서 모델의 판단을 덮어쓰지 않는다.
    data = report_data()
    data["assessment"] = "no_issue_observed"

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def chat(self, **kwargs):
            return ChatResponse(
                message={"role": "assistant", "content": json.dumps(data)},
                done=True,
                done_reason="stop",
            )

    monkeypatch.setattr(report_module, "Client", FakeClient)
    result = report_module.run_report(evidence, model="test", prompt_version=version)
    assert result["status"] == "generated"
    assert result["prompt_version"] == version
    assert result["report"]["assessment"] == "no_issue_observed"
    assert ("coverage" in result["input"]) == (version == "ops_report_v2")
