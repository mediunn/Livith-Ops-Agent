import asyncio
import time

import pytest

from ops_agent.agent import nodes
from ops_agent.tools.grafana import query_spec
from tests.helpers.agent import (
    evidence,
)


@pytest.mark.parametrize(
    "status,reason", [("tool_error", "collection_error"), ("expired", "time_budget")]
)
def test_terminal_failure_does_not_call_model(state, monkeypatch, status, reason):
    async def forbidden(*args, **kwargs):
        pytest.fail("종료된 조사에서 모델을 호출했습니다.")

    monkeypatch.setattr(nodes, "choose_action", forbidden)
    if status == "expired":
        state["deadline"] = time.time() - 1
    else:
        state["evidence"] = [evidence(status=status)]
    update = asyncio.run(nodes.AgentNodes().decide(state))
    assert update["stop_reason"] == reason


def test_duplicate_tool_blocked_before_io(state, monkeypatch):
    item = evidence()
    item["tool"], item["arguments"] = query_spec(state, "current_metrics")
    state.update(action="current_metrics", evidence=[item])
    assert (
        asyncio.run(nodes.AgentNodes().query(state))["stop_reason"]
        == "duplicate_or_invalid_tool"
    )


def test_same_action_with_different_arguments_can_run(state):
    item = evidence()
    item["tool"], item["arguments"] = query_spec(state, "current_metrics")
    state.update(action="current_metrics", evidence=[item])
    state["window"] = {
        "start": "2025-01-01T13:00:00+00:00",
        "end": "2025-01-01T14:00:00+00:00",
    }
    calls = []

    async def collect(state, trace=None):
        tool, arguments = query_spec(state, state["action"])
        calls.append(arguments)
        return {
            **item,
            "evidence_id": "new-evidence",
            "tool": tool,
            "arguments": arguments,
        }

    agent = nodes.AgentNodes(tool_executor=collect)
    assert asyncio.run(agent.decide(state))["action"] == "current_metrics"
    result = asyncio.run(agent.query(state))
    assert len(calls) == 1
    assert len(result["evidence"]) == 2
    assert result["evidence"][0]["arguments"] != result["evidence"][1]["arguments"]


def test_available_actions_use_query_conditions(state, monkeypatch):
    current = evidence()
    current["tool"], current["arguments"] = query_spec(state, "current_metrics")
    logs = evidence("logs")
    logs["tool"], logs["arguments"] = query_spec(state, "logs")
    logs["arguments"]["limit"] = 50
    state["evidence"] = [current, logs]

    async def choose(state, available, trace=None):
        assert "logs" in available
        return {"action": "logs"}

    monkeypatch.setattr(nodes, "choose_action", choose)
    assert asyncio.run(nodes.AgentNodes().decide(state))["action"] == "logs"
