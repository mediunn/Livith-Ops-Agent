import asyncio
import json
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


@pytest.mark.parametrize("output", ["answer", "json"])
@pytest.mark.parametrize(
    "status,reason,exit_code",
    [("completed", "planner_finished", 0), ("incomplete", "tool_budget", 1)],
)
def test_cli_outputs_answer_or_parseable_json(
    tmp_path, monkeypatch, capsys, output, status, reason, exit_code
):
    result = {
        "status": status,
        "stop_reason": reason,
        "answer": {
            "text": "평균 지연이 직전 구간보다 증가했습니다.",
            "evidence_ids": ["current-latency", "previous-latency"],
            "limitations": ["원인은 확인하지 못했습니다."],
        },
        "facts": [{"evidence_id": "current-latency"}],
    }

    async def fake_run(directory, state, *, step):
        return result

    monkeypatch.setattr(run_http_agent, "HTTP_AGENT_ROOT", tmp_path)
    monkeypatch.setattr(run_http_agent, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["run_http_agent", "--output", output])
    assert run_http_agent.main() == exit_code
    captured = capsys.readouterr()
    if output == "json":
        assert json.loads(captured.out) == result
        assert "Thread ID:" in captured.err
    else:
        assert result["answer"]["text"] in captured.out
        assert "current-latency, previous-latency" in captured.out
        assert result["answer"]["limitations"][0] in captured.out
        if status == "incomplete":
            assert "조사 미완료: tool_budget" in captured.out


def test_cli_json_pause_keeps_resume_guidance_off_stdout(tmp_path, monkeypatch, capsys):
    async def fake_run(directory, state, *, step):
        return {"status": "paused", "thread_id": directory.name}

    monkeypatch.setattr(run_http_agent, "HTTP_AGENT_ROOT", tmp_path)
    monkeypatch.setattr(run_http_agent, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["run_http_agent", "--step", "--output", "json"])
    assert run_http_agent.main() == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "paused"
    assert "--resume" in captured.err


@pytest.mark.parametrize(
    "status,reason,exit_code",
    [("incomplete", "time_budget", 1), ("completed", "no_candidates", 0)],
)
def test_cli_without_model_answer_reports_status_and_saved_result(
    tmp_path, monkeypatch, capsys, status, reason, exit_code
):
    async def fake_run(directory, state, *, step):
        return {
            "status": status,
            "stop_reason": reason,
            "answer": None,
            "facts": [{"evidence_id": "current"}],
        }

    monkeypatch.setattr(run_http_agent, "HTTP_AGENT_ROOT", tmp_path)
    monkeypatch.setattr(run_http_agent, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["run_http_agent"])
    assert run_http_agent.main() == exit_code
    output = capsys.readouterr().out
    assert "사용자 답변이 생성되지 않았습니다." in output
    assert reason in output
    assert "저장된 근거: 1건" in output
    assert "report.json" in output
