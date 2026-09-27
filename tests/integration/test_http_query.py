import asyncio
import json
import sys
from types import SimpleNamespace

import pytest
from mcp.types import CallToolResult, TextContent

from ops_agent.cli import query_http
from ops_agent.persistence.artifacts import read_json


def args(**updates):
    return SimpleNamespace(
        **{
            "metric": "all",
            "start": "2026-09-26T22:00:00",
            "end": "2026-09-26T23:00:00",
            "timezone": "Asia/Seoul",
            "route": "/api/v7/recommendation//concerts",
            "method": "GET",
            **updates,
        }
    )


@pytest.mark.parametrize("failure", [None, "connection", "tool", "cancelled"])
def test_cli_collection_shares_one_session_and_saves_outcome(
    tmp_path, monkeypatch, failure
):
    opened = []
    calls = []

    class Client:
        def __init__(self, server):
            pass

        async def __aenter__(self):
            opened.append(True)
            if failure == "connection":
                raise OSError("synthetic connection failure")
            return self

        async def __aexit__(self, *exc):
            pass

        async def call_tool(self, tool, arguments):
            calls.append(arguments)
            if failure == "cancelled":
                raise asyncio.CancelledError
            return CallToolResult(
                isError=failure == "tool",
                content=[TextContent(type="text", text=json.dumps({"data": []}))],
            )

    monkeypatch.setattr(query_http, "HTTP_ROOT", tmp_path)
    monkeypatch.setattr(query_http, "Client", Client)
    monkeypatch.setattr(query_http, "create_grafana_server", lambda: None)
    queries = query_http.queries_from_args(args())
    if failure == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(query_http.run_queries(queries))
    else:
        result = asyncio.run(query_http.run_queries(queries))
        assert result["status"] == ("completed" if failure is None else "incomplete")
    assert len(opened) == 1
    expected_calls = 2 if failure is None else (0 if failure == "connection" else 1)
    assert len(calls) == expected_calls
    saved = read_json(next(tmp_path.glob("*/result.json")))
    assert saved["budget"]["tool_calls"] == expected_calls
    assert saved["budget"]["llm_calls"] == 0
    if failure is None:
        assert len(saved["facts"]) == 2
        assert saved["facts"][0]["measurement"]["unit"] == "requests_per_second"
        assert saved["facts"][1]["measurement"]["unit"] == "milliseconds"
        assert len(list(tmp_path.glob("*/queries/*.json"))) == 2
    if failure == "cancelled":
        assert saved["status"] == "interrupted"
        assert not list(tmp_path.glob("*/queries/*.json"))


def test_cli_parses_exact_scope_and_utc_window():
    queries = query_http.queries_from_args(args(metric="http_mean_latency"))
    assert len(queries) == 1
    assert queries[0].start_at.isoformat() == "2026-09-26T13:00:00+00:00"
    assert queries[0].route == "/api/v7/recommendation//concerts"


@pytest.mark.parametrize(
    "options",
    [
        ["--start", "2026-09-26T22:00:00"],
        ["--route", "bad"],
        ["--method", "GET|POST"],
        ["--timezone", "Unknown/Zone"],
        ["--seconds", "0"],
    ],
)
def test_cli_rejects_invalid_scope_before_creating_files(
    tmp_path, monkeypatch, options
):
    root = tmp_path / "must-not-exist"
    monkeypatch.setattr(query_http, "HTTP_ROOT", root)
    monkeypatch.setattr(sys, "argv", ["query_http", *options])
    with pytest.raises(SystemExit) as exc:
        query_http.main()
    assert exc.value.code == 2
    assert not root.exists()


@pytest.mark.parametrize("status,code", [("completed", 0), ("incomplete", 1)])
def test_cli_exit_code_and_json_output(monkeypatch, capsys, status, code):
    async def run(queries, *, seconds):
        return {"status": status}

    monkeypatch.setattr(query_http, "run_queries", run)
    monkeypatch.setattr(sys, "argv", ["query_http"])
    assert query_http.main() == code
    assert json.loads(capsys.readouterr().out)["status"] == status
