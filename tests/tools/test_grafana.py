import asyncio
from pathlib import Path

import pytest
from mcp.types import CallToolResult, TextContent

from ops_agent.agent.budget import (
    read_budget,
)
from ops_agent.persistence.artifacts import query_artifact_path, read_json, save_json
from ops_agent.tools import grafana as tools


def test_previous_window_adjacent_not_now(state):
    _, current = tools.query_spec(state, "current_metrics")
    _, previous = tools.query_spec(state, "previous_metrics")
    assert previous["endTime"] == current["startTime"]
    assert tools.query_spec(state, "logs")[1]["endRfc3339"] == current["endTime"]
    with pytest.raises(ValueError):
        tools.query_spec(state, "arbitrary_query")


def test_tool_cache_survives_checkpoint_gap_and_separates_changed_query(
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
    # 다른 조건의 이전 action 파일이 남아 있어도 새 조회를 막지 않는다.
    save_json(
        Path(state["directory"]) / "current_metrics.json",
        read_json(Path(first["artifact"])),
    )
    state["window"] = {
        "start": "2025-01-01T13:00:00+00:00",
        "end": "2025-01-01T14:00:00+00:00",
    }
    third = asyncio.run(tools.execute_tool(state))
    assert third["query_key"] != first["query_key"]
    assert third["evidence_id"] != first["evidence_id"]
    assert third["artifact"] != first["artifact"]
    assert calls == ["query_prometheus", "query_prometheus"]
    assert read_budget(state)["tool_calls"] == 2
    assert len(list((Path(state["directory"]) / "queries").glob("*.json"))) == 2
    assert asyncio.run(tools.execute_tool(state)) == third
    assert len(calls) == 2


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
    tool, arguments = tools.query_spec(state, state["action"])
    assert not query_artifact_path(state["directory"], tool, arguments).exists()


@pytest.mark.parametrize("layout", ["legacy", "keyed"])
def test_saved_cache_reused_without_io_or_budget(state, monkeypatch, layout):
    state["action"] = "current_metrics"
    tool, arguments = tools.query_spec(state, state["action"])
    target = query_artifact_path(state["directory"], tool, arguments)
    source = (
        Path(state["directory"]) / "current_metrics.json"
        if layout == "legacy"
        else target
    )
    record = {
        "thread_id": state["thread_id"],
        "action": state["action"],
        "evidence_id": "preserve-this-id",
        "tool": tool,
        "arguments": arguments,
        "status": "no_data",
        "completed_at": 1,
        "response": {"raw": "local-only"},
        "artifact": str(source),
    }
    save_json(source, record)

    def forbidden(*args, **kwargs):
        pytest.fail("캐시 재사용 중 외부 연결을 시작했습니다.")

    monkeypatch.setattr(tools, "create_grafana_server", forbidden)
    state["deadline"] = 0  # 이미 완료된 조회 복구에 새 예산은 필요하지 않다.
    result = asyncio.run(tools.execute_tool(state))
    assert result["evidence_id"] == "preserve-this-id"
    assert "response" not in result
    assert read_budget(state)["tool_calls"] == 0
    assert read_json(target)["evidence_id"] == "preserve-this-id"
    if layout == "legacy":
        assert read_json(source) == record


@pytest.mark.parametrize("mutation", ["thread", "arguments", "pending"])
def test_keyed_cache_still_rejects_wrong_or_incomplete_record(
    state, monkeypatch, mutation
):
    state["action"] = "current_metrics"
    tool, arguments = tools.query_spec(state, state["action"])
    record = {
        "thread_id": state["thread_id"],
        "tool": tool,
        "arguments": dict(arguments),
        "status": "no_data",
        "completed_at": 1,
    }
    if mutation == "thread":
        record["thread_id"] = "another-thread"
    elif mutation == "arguments":
        record["arguments"]["stepSeconds"] = 120
    else:
        record.update(status="pending", completed_at=None)
    save_json(query_artifact_path(state["directory"], tool, arguments), record)

    def forbidden(*args, **kwargs):
        pytest.fail("잘못된 캐시로 외부 연결을 시작했습니다.")

    monkeypatch.setattr(tools, "create_grafana_server", forbidden)
    with pytest.raises(ValueError):
        asyncio.run(tools.execute_tool(state))
    assert read_budget(state)["tool_calls"] == 0
