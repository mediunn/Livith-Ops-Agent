import asyncio
import json

import pytest
from mcp.types import CallToolResult, TextContent

from ops_agent.collectors import loki
from ops_agent.collectors.loki_parser import parse_loki_response
from ops_agent.collectors.prometheus_parser import parse_prometheus_response


def response(payload):
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def entry(timestamp="1790425505095804000"):
    return {
        "timestamp": timestamp,
        "line": "synthetic test log",
        "labels": {"job": "livith-server"},
        "structuredMetadata": {"detected_level": "info"},
    }


def test_nanoseconds_and_returned_scope():
    summary = parse_loki_response(
        response({"data": [entry("1790425505095804001"), entry()]}), limit=100
    )
    assert summary["first_timestamp_ns"] == "1790425505095804000"
    assert summary["last_timestamp_ns"] == "1790425505095804001"
    assert summary["stream_count"] == 1
    assert summary["level_counts"] == {"info": 2}
    assert summary["scope"] == "returned_logs_only"


def test_empty_is_not_an_error():
    assert parse_loki_response(response({"data": []}), limit=100)["status"] == "no_data"


@pytest.mark.parametrize(
    "metadata,limit",
    [({"resultsTruncated": True}, 100), ({"resultsTruncated": False}, 1)],
)
def test_truncation_caution(metadata, limit):
    result = parse_loki_response(
        response({"data": [entry()], "metadata": metadata}), limit=limit
    )
    assert result["possibly_truncated"] is True


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": None},
        {"data": [{}]},
        {"data": [entry("not-a-timestamp")]},
        {"data": [], "metadata": {"linesReturned": 1}},
        {"data": [], "metadata": {"resultsTruncated": "false"}},
    ],
)
def test_malformed_is_not_no_data(payload):
    with pytest.raises((TypeError, ValueError)):
        parse_loki_response(response(payload), limit=100)


@pytest.fixture
def reference(tmp_path):
    path = tmp_path / "prometheus-reference.json"
    path.write_text(
        json.dumps(
            {
                "tool": "query_prometheus",
                "evidence_id": "prom-test-id",
                "arguments": {
                    "queryType": "range",
                    "startTime": "2026-09-26T12:12:59Z",
                    "endTime": "2026-09-26T12:42:59Z",
                },
            }
        )
    )
    return path


@pytest.mark.parametrize(
    "payload,tool_error,expected",
    [
        ({"data": [entry()]}, False, "data_available"),
        ({"data": []}, False, "no_data"),
        ({"other": []}, False, "parse_error"),
        ({"error": "denied"}, True, "tool_error"),
    ],
)
def test_cli_keeps_window_raw_and_status(
    tmp_path, monkeypatch, reference, payload, tool_error, expected
):
    calls = []
    result = CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload))],
        is_error=tool_error,
    )

    class FakeClient:
        def __init__(self, server):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return result

    monkeypatch.setattr(loki, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(loki, "create_grafana_server", lambda: None)
    monkeypatch.setattr(loki, "Client", FakeClient)
    code = asyncio.run(
        loki.collect(reference, logql=loki.DEFAULT_LOGQL, limit=100, discover=False)
    )
    saved = json.loads(next((tmp_path / "artifacts").glob("loki-*.json")).read_text())
    assert saved["status"] == expected
    assert saved["response"] == result.model_dump(mode="json", by_alias=True)
    assert saved["related_prometheus_evidence_id"] == "prom-test-id"
    assert calls[0][1]["startRfc3339"] == "2026-09-26T12:12:59Z"
    assert calls[0][1]["endRfc3339"] == "2026-09-26T12:42:59Z"
    assert code == (0 if expected in {"data_available", "no_data"} else 1)


def test_invalid_reference_time_rejected(reference):
    data = json.loads(reference.read_text())
    data["arguments"]["startTime"] = "now-30m"
    reference.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        loki.read_reference(reference)


def test_prometheus_zero_remains_data():
    parsed = parse_prometheus_response(
        response({"data": [{"metric": {"api": "apple"}, "values": [[1, "0"]]}]})
    )
    assert parsed["status"] == "data_available"
    assert parsed["series"][0]["all_zero"] is True
