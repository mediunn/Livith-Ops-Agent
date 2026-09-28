import asyncio
import sys
from pathlib import Path

import pytest
from mcp.types import CallToolResult

from ops_agent.agent.http import session
from ops_agent.agent.http.graph import initial_state
from ops_agent.agent.http.planner import rule_planner
from ops_agent.cli import run_http_agent
from ops_agent.evaluation.http_agent import SCOPE
from ops_agent.tools.http import HTTPQuery


def test_live_collection_path_opens_one_session_and_closes_it(tmp_path, monkeypatch):
    events = []

    class Client:
        def __init__(self, server):
            pass

        async def __aenter__(self):
            self.owner = asyncio.current_task()
            events.append("open")
            return self

        async def __aexit__(self, *args):
            assert asyncio.current_task() is self.owner
            events.append("close")

        async def call_tool(self, name, arguments):
            events.append("query")
            return CallToolResult(content=[], structuredContent={"data": []})

    monkeypatch.setattr(session, "Client", Client)
    monkeypatch.setattr(session, "create_grafana_server", lambda: None)
    state = initial_state(
        tmp_path / "run", HTTPQuery(**SCOPE), symptom="HTTP 변화 확인"
    )
    report = asyncio.run(
        session.run(Path(state["directory"]), state, planner=rule_planner)
    )
    assert report["stop_reason"] == "no_candidates"
    assert events == ["open", "query", "query", "close"]
    assert asyncio.run(session.run(Path(state["directory"]))) == report
    assert len(events) == 4


@pytest.mark.parametrize(
    "options",
    [
        ["--seconds", "0"],
        ["--tool-calls", "1"],
        ["--llm-seconds", "0"],
        ["--route", "invalid"],
        ["--start", "2026-09-26T13:00:00"],
        ["--resume", "../escape"],
        ["--resume", "0" * 32, "--planner", "rules"],
        ["--resume", "0" * 32, "--llm-seconds", "90"],
    ],
)
def test_cli_rejects_invalid_requests_without_artifacts(tmp_path, monkeypatch, options):
    root = tmp_path / "absent"
    monkeypatch.setattr(run_http_agent, "HTTP_AGENT_ROOT", root)
    monkeypatch.setattr(sys, "argv", ["run_http_agent", *options])
    with pytest.raises(SystemExit) as exc:
        run_http_agent.main()
    assert exc.value.code == 2
    assert not root.exists()


def test_cli_passes_scope_and_prints_resume_instruction(tmp_path, monkeypatch, capsys):
    async def fake_run(directory, state, *, step):
        assert step is True and state["planner_kind"] == "rules"
        assert state["request"]["route"] == "/api//concerts"
        assert state["request"]["start_at"].startswith("2026-09-26T13:00")
        return {"status": "paused"}

    monkeypatch.setattr(run_http_agent, "HTTP_AGENT_ROOT", tmp_path)
    monkeypatch.setattr(run_http_agent, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_http_agent",
            "--planner",
            "rules",
            "--step",
            "--route",
            "/api//concerts",
            "--start",
            "2026-09-26T22:00:00",
            "--end",
            "2026-09-26T23:00:00",
            "--timezone",
            "Asia/Seoul",
        ],
    )
    assert run_http_agent.main() == 0
    assert "--resume" in capsys.readouterr().out
