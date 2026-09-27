import asyncio
import time

import pytest

from ops_agent.agent import nodes
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
    state.update(action="current_metrics", evidence=[evidence()])
    assert (
        asyncio.run(nodes.AgentNodes().query(state))["stop_reason"]
        == "duplicate_or_invalid_tool"
    )
