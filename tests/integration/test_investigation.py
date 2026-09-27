import asyncio
import json

import pytest

from ops_agent.cli import run_investigation as workflow


@pytest.mark.parametrize("failure", [None, "prometheus", "loki", "report", "exception"])
def test_pipeline_passes_exact_files_and_stops_on_failure(
    tmp_path, monkeypatch, failure
):
    monkeypatch.setattr(workflow, "PROJECT_DIR", tmp_path)
    calls = []

    async def metrics(*, output_dir):
        calls.append("prometheus")
        if failure == "exception":
            raise TimeoutError("synthetic timeout")
        path = output_dir / "this-prometheus.json"
        record = {
            "status": "tool_error" if failure == "prometheus" else "no_data",
            "arguments": {
                "startTime": "2026-01-01T00:00:00Z",
                "endTime": "2026-01-01T00:30:00Z",
            },
        }
        path.write_text(json.dumps(record))
        return path, record

    async def logs(reference_path, *, output_dir, **kwargs):
        calls.append("loki")
        assert reference_path == output_dir / "this-prometheus.json"
        path = output_dir / "this-loki.json"
        record = {"status": "tool_error" if failure == "loki" else "no_data"}
        path.write_text(json.dumps(record))
        return path, record

    def report(path, **kwargs):
        calls.append("report")
        assert path.name == "this-loki.json"
        assert kwargs["investigation"]["environment_filter_applied"] is False
        return {
            "run_id": "report-run",
            "status": "factual_validation_error"
            if failure == "report"
            else "generated",
            "report": None if failure == "report" else {"facts": []},
            "validation": {"observation_values": failure != "report"},
            "error": {"type": "ValueError"} if failure == "report" else None,
        }

    monkeypatch.setattr(workflow, "collect_metrics", metrics)
    monkeypatch.setattr(workflow, "collect_logs", logs)
    monkeypatch.setattr(workflow, "run_report", report)
    directory, record = asyncio.run(workflow.investigate(model="test", symptom="test"))
    assert (
        json.loads((directory / "investigation.json").read_text())["status"]
        == record["status"]
    )
    if failure in {"prometheus", "exception"}:
        assert calls == ["prometheus"]
        assert record["status"] == "prometheus_error"
    elif failure == "loki":
        assert calls == ["prometheus", "loki"]
        assert record["status"] == "loki_error"
    else:
        assert calls == ["prometheus", "loki", "report"]
        assert (directory / "report.json").exists()
        assert record["status"] == (
            "factual_validation_error" if failure else "generated"
        )
    assert record["report"] is None if failure else record["report"] is not None
