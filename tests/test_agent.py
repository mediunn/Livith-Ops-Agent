import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from mcp.types import CallToolResult, TextContent
from ollama import ChatResponse

from ops_agent.agent import nodes, planner
from ops_agent.agent.budget import (
    BudgetExceeded,
    initialize_budget,
    read_budget,
    reserve,
)
from ops_agent.agent.graph import build_graph
from ops_agent.config import NUM_CTX, PROJECT_DIR, TOKEN_RESERVATION
from ops_agent.persistence import session
from ops_agent.persistence.artifacts import read_json
from ops_agent.reporting.agent_report import build_report
from ops_agent.tools import grafana as tools


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "AGENT_ROOT", tmp_path)
    return session.initial_state(
        SimpleNamespace(
            model="qwen2.5:3b",
            symptom="test",
            seconds=600,
        ),
        "test-thread",
    )


def decision(action="finish", **updates):
    return {
        "action": action,
        "rationale": "관측 근거 추가 확인",
        "assessment": "insufficient_evidence",
        "hypotheses": [],
        "limitations": ["서비스 정상 기준 없음"],
        "next_checks": ["HTTP 지표 확인"],
        **updates,
    }


def evidence(action="current_metrics", status="data_available"):
    return {
        "action": action,
        "evidence_id": action,
        "source": "synthetic",
        "tool": "query_prometheus" if "metrics" in action else "query_loki_logs",
        "arguments": {},
        "measurement": {},
        "status": status,
        "summary": {},
        "error_type": "TestError" if status == "tool_error" else None,
        "artifact": "/must/not/be/sent/to/langfuse.json",
        "response": {"raw_log": "RAW_BODY_MARKER"},
    }


def mock_graph(monkeypatch, calls):
    async def collect(state, trace=None):
        reserve(state, "tool")
        calls.append(state["action"])
        return evidence(state["action"])

    async def choose(state, available, trace=None):
        reserve(state, "llm")
        return decision("logs" if "logs" in available else "finish")

    monkeypatch.setattr(nodes, "execute_tool", collect)
    monkeypatch.setattr(nodes, "choose_action", choose)


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
            assert result["deadline"] == state["deadline"]
            assert result["report"]["status"] == "completed"
            assert not (await graph.aget_state(config)).next

    asyncio.run(scenario())
    assert calls == ["current_metrics", "logs"]
    assert read_budget(state)["tool_calls"] == 2


@pytest.mark.parametrize(
    "kind,limit,reason",
    [
        ("tool", "tool_calls", "tool_budget"),
        ("llm", "llm_calls", "llm_budget"),
        ("llm", "reserved_tokens", "token_budget"),
    ],
)
def test_budgets_persist_without_refund(state, kind, limit, reason):
    state["limits"][limit] = TOKEN_RESERVATION if limit == "reserved_tokens" else 1
    reserve(state, kind)
    with pytest.raises(BudgetExceeded, match=reason):
        reserve(state, kind)
    budget = read_budget(state)
    assert budget[f"{kind}_calls"] == 1
    with pytest.raises(ValueError, match="초기화"):
        initialize_budget(Path(state["directory"]))


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


def test_previous_window_adjacent_not_now(state):
    _, current = tools.query_spec(state, "current_metrics")
    _, previous = tools.query_spec(state, "previous_metrics")
    assert previous["endTime"] == current["startTime"]
    assert tools.query_spec(state, "logs")[1]["endRfc3339"] == current["endTime"]
    with pytest.raises(ValueError):
        tools.query_spec(state, "arbitrary_query")


@pytest.mark.parametrize(
    "change",
    [
        {"action": "logs"},
        {"hypotheses": [{"statement": "원인", "evidence_ids": ["unknown"]}]},
        {"limitations": ["  "]},
        {"rationale": "x" * 161},
    ],
)
def test_reject_invalid_model_decision(state, change):
    state["evidence"] = [evidence()]
    with pytest.raises(ValueError):
        planner.validate_decision(json.dumps(decision(**change)), state, [])


def test_no_data_cannot_support_hypothesis(state):
    state["evidence"] = [evidence(status="no_data")]
    result = decision(
        hypotheses=[{"statement": "원인", "evidence_ids": ["current_metrics"]}]
    )
    with pytest.raises(ValueError):
        planner.validate_decision(json.dumps(result), state, [])


def test_report_preserves_no_data_and_excludes_local_paths(state):
    state.update(
        evidence=[evidence(status="no_data"), evidence("logs", "no_data")],
        decisions=[decision(assessment="needs_investigation")],
        stop_reason="model_finished",
    )
    result = build_report(state)
    assert result["assessment"] == "insufficient_evidence"
    assert result["model_assessment"] == "needs_investigation"
    assert result["facts"][0]["status"] == "no_data"
    assert "/must/not" not in json.dumps(result)
    assert "RAW_BODY_MARKER" not in json.dumps(result)


def test_model_context_excludes_raw_logs_and_paths(state):
    state["evidence"] = [evidence()]
    context = json.dumps(planner.build_context(state, ["logs"]))
    assert "RAW_BODY_MARKER" not in context
    assert "/must/not" not in context


def test_context_limit_before_call_or_reservation(state):
    state["prompt"] = "x" * NUM_CTX
    with pytest.raises(planner.ContextTooLarge):
        asyncio.run(planner.choose_action(state, []))
    assert read_budget(state)["llm_calls"] == 0


@pytest.mark.parametrize("failure", [None, "length", "invalid", "timeout"])
def test_model_response_and_failure_receipts(state, monkeypatch, failure):
    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def chat(self, **kwargs):
            assert kwargs["format"]["properties"]["action"]["enum"] == ["finish"]
            if failure == "timeout":
                raise TimeoutError
            return ChatResponse(
                model=state["model"],
                done=True,
                done_reason="length" if failure == "length" else "stop",
                message={
                    "role": "assistant",
                    "content": "{}" if failure == "invalid" else json.dumps(decision()),
                },
                prompt_eval_count=123,
                eval_count=45,
            )

    monkeypatch.setattr(planner, "AsyncClient", FakeClient)
    if failure:
        with pytest.raises((TimeoutError, ValueError)):
            asyncio.run(planner.choose_action(state, []))
    else:
        assert asyncio.run(planner.choose_action(state, []))["action"] == "finish"
    receipts = list(Path(state["directory"]).glob("decision-*.json"))
    assert len(receipts) == 1
    receipt = read_json(receipts[0])
    assert bool(receipt["error_type"]) == bool(failure)
    assert read_budget(state)["llm_calls"] == 1
    if failure != "timeout":
        assert receipt["usage"]["input_tokens"] == 123


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


def test_session_resume_across_processes(tmp_path):
    # 두 별도 Python 프로세스에서 CLI와 SQLite 복구를 통합 검증한다.
    script = r"""
import sys
from pathlib import Path
import run_agent
from ops_agent.agent import nodes
from ops_agent.agent.budget import reserve
from ops_agent.persistence import session
root=Path(sys.argv[1])
run_agent.AGENT_ROOT=root
session.AGENT_ROOT=root
session.CHECKPOINT_DB=root/'checkpoints.sqlite'
async def tool(state, trace=None):
    reserve(state, 'tool')
    action=state['action']
    with (root/'calls.txt').open('a') as f:
        f.write(action+'\n')
    return dict(action=action, evidence_id=action, tool='query_prometheus' if action=='current_metrics' else 'query_loki_logs', arguments={}, measurement={}, status='data_available', summary={}, error_type=None)
async def choose(state, available, trace=None):
    reserve(state, 'llm')
    return dict(action='logs' if 'logs' in available else 'finish', rationale='test', assessment='insufficient_evidence', hypotheses=[], limitations=['test'], next_checks=['test'])
nodes.execute_tool=tool
nodes.choose_action=choose
sys.argv=['run_agent.py', *sys.argv[2:]]
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

    first = launch("--seconds", "600", "--step")
    assert first.returncode == 0, first.stderr
    thread = next(
        line.split(": ", 1)[1]
        for line in first.stdout.splitlines()
        if line.startswith("Thread ID:")
    )
    second = launch("--resume", thread)
    assert second.returncode == 0, second.stderr
    assert (tmp_path / "calls.txt").read_text().splitlines() == [
        "current_metrics",
        "logs",
    ]
    result = read_json(tmp_path / thread / "report.json")
    assert result["budget"]["tool_calls"] == 2
    third = launch("--resume", thread)
    assert third.returncode == 0
    assert (tmp_path / "calls.txt").read_text().splitlines() == [
        "current_metrics",
        "logs",
    ]


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


def test_incomplete_report_does_not_reuse_stale_interpretation(state):
    state.update(
        evidence=[evidence(), evidence("previous_metrics")],
        decisions=[
            decision(
                "previous_metrics",
                limitations=["아직 직전 구간 미조회"],
                next_checks=["직전 구간을 조회한다"],
            )
        ],
        stop_reason="decision_error",
    )
    report = build_report(state)
    assert report["model_assessment"] is None
    assert "아직 직전 구간 미조회" not in report["limitations"]
    assert "직전 구간을 조회한다" not in report["next_checks"]
    assert report["decisions"] == state["decisions"]
