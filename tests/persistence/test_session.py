import asyncio
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from ops_agent.agent.budget import (
    read_budget,
)
from ops_agent.agent.graph import build_graph
from ops_agent.config import PROJECT_DIR
from ops_agent.persistence import session
from ops_agent.persistence.artifacts import read_json
from tests.helpers.agent import (
    mock_graph,
)


def test_resume_reopens_sqlite_and_preserves_window(state, tmp_path, monkeypatch):
    calls = []
    mock_graph(monkeypatch, calls)
    config = {"configurable": {"thread_id": state["thread_id"]}}

    async def scenario():
        database = str(tmp_path / "checkpoints.sqlite")
        async with AsyncSqliteSaver.from_conn_string(database) as saver:
            graph = build_graph(saver)
            await graph.ainvoke(
                state, config, interrupt_after=["query"], durability="sync"
            )
            saved = await graph.aget_state(config)
            assert saved.next == ("decide",)
            assert calls == ["current_metrics"]
        async with AsyncSqliteSaver.from_conn_string(database) as saver:
            graph = build_graph(saver)
            result = await graph.ainvoke(None, config, durability="sync")
            assert result["window"] == state["window"]
            assert result["request"] == state["request"]
            assert result["deadline"] == state["deadline"]
            assert result["report"]["status"] == "completed"
            assert not (await graph.aget_state(config)).next

    asyncio.run(scenario())
    assert calls == ["current_metrics", "logs"]
    assert read_budget(state)["tool_calls"] == 2


@pytest.mark.parametrize("custom_window", [False, True])
@pytest.mark.parametrize("compare_previous", [False, True])
def test_session_resume_across_processes(tmp_path, custom_window, compare_previous):
    # 두 별도 Python 프로세스에서 CLI와 SQLite 복구를 통합 검증한다.
    script = r"""
import sys
from pathlib import Path
from ops_agent.cli import run_agent
from ops_agent.agent import nodes
from ops_agent.agent.budget import reserve
from ops_agent.agent.decision.planner import build_context
from ops_agent.persistence import session
root=Path(sys.argv[1])
run_agent.AGENT_ROOT=root
session.AGENT_ROOT=root
session.CHECKPOINT_DB=root/'checkpoints.sqlite'
async def tool(state, trace=None):
    reserve(state, 'tool')
    action=state['action']
    from ops_agent.tools.grafana import query_spec
    from ops_agent.persistence.artifacts import save_json
    tool_name, arguments = query_spec(state, action)
    save_json(root / f'{action}-query.json', arguments)
    with (root/'calls.txt').open('a') as f:
        f.write(action+'\n')
    return dict(action=action, evidence_id=action, tool=tool_name, arguments=arguments, measurement={}, status='data_available', summary={}, error_type=None)
async def choose(state, available, trace=None):
    reserve(state, 'llm')
    from ops_agent.persistence.artifacts import save_json
    save_json(root / 'planner-context.json', build_context(state, available))
    return dict(action='logs' if 'logs' in available else ('previous_metrics' if 'previous_metrics' in available else 'finish'), rationale='test', assessment='insufficient_evidence', hypotheses=[], limitations=['test'], next_checks=['test'])
nodes.execute_tool=tool
nodes.choose_action=choose
sys.argv=['ops_agent.cli.run_agent', *sys.argv[2:]]
raise SystemExit(run_agent.main())
"""
    env = {**os.environ, "OPS_LANGFUSE_ENABLED": "false"}

    def launch(*args):
        return subprocess.run(
            [sys.executable, "-c", script, str(tmp_path), *args],
            cwd=PROJECT_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    options = (
        [
            "--timezone",
            "Asia/Seoul",
            "--start",
            "2025-01-01T22:00:00",
            "--end",
            "2025-01-01T23:00:00",
            "--symptom",
            "호출량 변화",
        ]
        if custom_window
        else []
    )
    if not compare_previous:
        options.append("--no-compare-previous")
    expected_actions = ["current_metrics", "logs"] + (
        ["previous_metrics"] if compare_previous else []
    )
    first = launch("--seconds", "600", "--step", *options)
    assert first.returncode == 0, first.stderr
    thread = next(
        line.split(": ", 1)[1]
        for line in first.stdout.splitlines()
        if line.startswith("Thread ID:")
    )
    status = launch("--status", thread)
    assert status.returncode == 0, status.stderr
    assert (tmp_path / "calls.txt").read_text().splitlines() == ["current_metrics"]
    second = launch("--resume", thread)
    assert second.returncode == 0, second.stderr
    assert (tmp_path / "calls.txt").read_text().splitlines() == expected_actions
    result = read_json(tmp_path / thread / "report.json")
    assert result["budget"]["tool_calls"] == len(expected_actions)
    assert result["request"]["compare_previous"] is compare_previous
    assert result["required_checks"]["missing"] == []
    context = read_json(tmp_path / "planner-context.json")
    assert context["request"] == result["request"]
    assert context["window"] == result["window"]
    assert context["scope"]["environment_filter_applied"] is False
    for invocation in (tmp_path / thread).glob("invocation-*.json"):
        assert read_json(invocation)["request"] == result["request"]
    current = read_json(tmp_path / "current_metrics-query.json")
    logs = read_json(tmp_path / "logs-query.json")
    assert current["startTime"] == logs["startRfc3339"] == result["window"]["start"]
    assert current["endTime"] == logs["endRfc3339"] == result["window"]["end"]
    if custom_window:
        assert result["window"] == {
            "start": "2025-01-01T13:00:00+00:00",
            "end": "2025-01-01T14:00:00+00:00",
        }
        assert result["request"]["timezone"] == "Asia/Seoul"
        assert "2025-01-01T13:00:00+00:00" in status.stdout
    third = launch("--resume", thread)
    assert third.returncode == 0
    assert (tmp_path / "calls.txt").read_text().splitlines() == expected_actions


def test_request_validation_precedes_session_directory_creation(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "AGENT_ROOT", tmp_path)
    args = SimpleNamespace(
        model="qwen2.5:3b",
        seconds=600,
        symptom="test",
        start="2025-01-01T22:00:00",
        end=None,
    )
    with pytest.raises(ValueError, match="함께"):
        session.initial_state(args, "invalid")
    assert not (tmp_path / "invalid").exists()


def test_session_trace_uses_saved_scope(state, tmp_path, monkeypatch):
    state["version"] = 4
    calls = []
    mock_graph(monkeypatch, calls)
    monkeypatch.setattr(session, "CHECKPOINT_DB", tmp_path / "trace.sqlite")
    captured = []
    original_trace = session.ReportTrace

    def capture(record, metadata, **kwargs):
        captured.append(metadata)
        return original_trace(record, metadata, **kwargs)

    monkeypatch.setattr(session, "ReportTrace", capture)
    monkeypatch.setattr(session, "initial_state", lambda *_: state)
    args = SimpleNamespace(resume=None, status=None, step=True)
    assert asyncio.run(session.run(args)) == 0
    args.resume = captured[0]["thread_id"]
    args.step = False
    assert asyncio.run(session.run(args)) == 0
    assert len(captured) == 2
    for metadata in captured:
        assert metadata["service"] == state["request"]["service"]
        assert metadata["environment"] == "unspecified"
        assert metadata["timezone"] == state["request"]["timezone"]
        assert metadata["window"] == state["window"]
        assert metadata["environment_filter_applied"] is False
        assert metadata["metric_service_filter_applied"] is False


@pytest.mark.parametrize("old_version", [2, 3])
def test_old_checkpoint_rejected_before_tool_or_model_call(
    state, tmp_path, monkeypatch, old_version
):
    state["version"] = old_version
    database = tmp_path / "old.sqlite"
    monkeypatch.setattr(session, "CHECKPOINT_DB", database)
    calls = []
    mock_graph(monkeypatch, calls)
    config = {"configurable": {"thread_id": state["thread_id"]}}

    async def scenario():
        async with AsyncSqliteSaver.from_conn_string(str(database)) as saver:
            graph = build_graph(saver)
            await graph.aupdate_state(config, state, as_node="query")
        with pytest.raises(ValueError, match="이전 구조"):
            await session.run(SimpleNamespace(resume=state["thread_id"], status=None))

    asyncio.run(scenario())
    assert not calls
