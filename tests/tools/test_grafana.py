import asyncio
from pathlib import Path

import pytest
from mcp.types import CallToolResult, TextContent

from ops_agent.agent.budget import (
    read_budget,
)
from ops_agent.tools import grafana as tools


def test_previous_window_adjacent_not_now(state):
    _, current = tools.query_spec(state, "current_metrics")
    _, previous = tools.query_spec(state, "previous_metrics")
    assert previous["endTime"] == current["startTime"]
    assert tools.query_spec(state, "logs")[1]["endRfc3339"] == current["endTime"]
    with pytest.raises(ValueError):
        tools.query_spec(state, "arbitrary_query")


def test_tool_cache_survives_checkpoint_gap_and_rejects_changed_query(
    state, monkeypatch
):
    calls = []

    class FakeClient:
        def __init__(self, *_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def call_tool(self, name, arguments):
            calls.append(name)
            return CallToolResult(
                content=[TextContent(type="text", text='{"data": []}')]
            )

    monkeypatch.setattr(tools, "Client", FakeClient)
    monkeypatch.setattr(tools, "create_grafana_server", lambda: None)
    state["action"] = "current_metrics"
    first = asyncio.run(tools.execute_tool(state))
    # state가 아직 checkpoint되지 않았다고 가정하고 같은 노드를 재실행한다.
    second = asyncio.run(tools.execute_tool(state))
    assert first == second
    assert "response" not in first
    assert calls == ["query_prometheus"]
    assert read_budget(state)["tool_calls"] == 1
    state["window"]["end"] = "2099-01-01T00:00:00+00:00"
    with pytest.raises(ValueError, match="조회 조건"):
        asyncio.run(tools.execute_tool(state))


def test_custom_window_previous_metrics_uses_equal_duration(state):
    state["window"] = {
        "start": "2025-01-01T13:00:00+00:00",
        "end": "2025-01-01T14:00:00+00:00",
    }
    _, previous = tools.query_spec(state, "previous_metrics")
    assert previous["startTime"] == "2025-01-01T12:00:00+00:00"
    assert previous["endTime"] == state["window"]["start"]
    for action in ("logs", "warning_logs"):
        _, arguments = tools.query_spec(state, action)
        assert arguments["startRfc3339"] == state["window"]["start"]
        assert arguments["endRfc3339"] == state["window"]["end"]


def test_cancelled_read_is_charged_but_not_cached(state, monkeypatch):
    class InterruptedClient:
        def __init__(self, *_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def call_tool(self, *args, **kwargs):
            raise asyncio.CancelledError

    monkeypatch.setattr(tools, "Client", InterruptedClient)
    monkeypatch.setattr(tools, "create_grafana_server", lambda: None)
    state["action"] = "current_metrics"
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(tools.execute_tool(state))
    assert read_budget(state)["tool_calls"] == 1
    assert not (Path(state["directory"]) / "current_metrics.json").exists()
