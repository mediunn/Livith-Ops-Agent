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
from ops_agent.agent.decision_validation import (
    DecisionValidationError,
    decision_schema,
)
from ops_agent.agent.graph import build_graph
from ops_agent.config import NUM_CTX, PROJECT_DIR, TOKEN_RESERVATION
from ops_agent.persistence import session
from ops_agent.persistence.artifacts import read_json
from ops_agent.reporting.agent_report import build_report
from ops_agent.tools import grafana as tools


def decision(action="finish", **updates):
    return {
        "action": action,
        "rationale": "관측 근거를 추가 확인한다.",
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
            assert result["request"] == state["request"]
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
    attempts = 2 if failure == "invalid" else 1
    assert len(receipts) == attempts
    receipt = read_json(receipts[0])
    assert bool(receipt["error_type"]) == bool(failure)
    assert read_budget(state)["llm_calls"] == attempts
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


@pytest.mark.parametrize("custom_window", [False, True])
def test_session_resume_across_processes(tmp_path, custom_window):
    # 두 별도 Python 프로세스에서 CLI와 SQLite 복구를 통합 검증한다.
    script = r"""
import sys
from pathlib import Path
import run_agent
from ops_agent.agent import nodes
from ops_agent.agent.budget import reserve
from ops_agent.agent.planner import build_context
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
    return dict(action=action, evidence_id=action, tool='query_prometheus' if action=='current_metrics' else 'query_loki_logs', arguments={}, measurement={}, status='data_available', summary={}, error_type=None)
async def choose(state, available, trace=None):
    reserve(state, 'llm')
    from ops_agent.persistence.artifacts import save_json
    save_json(root / 'planner-context.json', build_context(state, available))
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
    assert (tmp_path / "calls.txt").read_text().splitlines() == [
        "current_metrics",
        "logs",
    ]
    result = read_json(tmp_path / thread / "report.json")
    assert result["budget"]["tool_calls"] == 2
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
    assert (tmp_path / "calls.txt").read_text().splitlines() == [
        "current_metrics",
        "logs",
    ]


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


def test_old_checkpoint_rejected_before_tool_or_model_call(
    state, tmp_path, monkeypatch
):
    state["version"] = 2
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


@pytest.mark.parametrize("status", ["no_data", "invalid_data", "tool_error"])
def test_schema_and_context_exclude_non_data_hypothesis_ids(state, status):
    state["evidence"] = [evidence(), evidence("logs", status)]
    schema = decision_schema(state, ["warning_logs"])
    items = schema["$defs"]["Hypothesis"]["properties"]["evidence_ids"]["items"]
    assert items["enum"] == ["current_metrics"]
    context = planner.build_context(state, ["warning_logs"])
    assert context["allowed_hypothesis_evidence_ids"] == ["current_metrics"]
    assert context["evidence"][1]["status"] == status
    state["evidence"] = [evidence("logs", status)]
    assert decision_schema(state, [])["properties"]["hypotheses"]["maxItems"] == 0
    # 생성 스키마와 별개로 사후 검사도 우회할 수 없다.
    with pytest.raises(DecisionValidationError) as exc:
        planner.validate_decision(
            json.dumps(
                decision(hypotheses=[{"statement": "가설", "evidence_ids": ["logs"]}])
            ),
            state,
            [],
        )
    assert exc.value.code == "unavailable_evidence"


@pytest.mark.parametrize(
    "content,code",
    [
        ("{}", "schema_error"),
        (json.dumps(decision("logs")), "invalid_action"),
        (
            json.dumps(
                decision(
                    hypotheses=[{"statement": "가설", "evidence_ids": ["missing"]}]
                )
            ),
            "unknown_evidence",
        ),
    ],
)
def test_validation_errors_have_stable_safe_codes(state, content, code):
    with pytest.raises(DecisionValidationError) as exc:
        planner.validate_decision(content, state, [])
    assert exc.value.details()["code"] == code
    assert exc.value.details()["valid"] is False


def fake_model(monkeypatch, state, outputs):
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def chat(self, **kwargs):
            calls.append(kwargs)
            value = outputs[len(calls) - 1]
            if callable(value):
                value = value()
            return ChatResponse(
                model=state["model"],
                done=True,
                done_reason="stop",
                message={"role": "assistant", "content": json.dumps(value)},
                prompt_eval_count=100,
                eval_count=20,
            )

    monkeypatch.setattr(planner, "AsyncClient", Client)
    return calls


def bad_reference():
    return decision(hypotheses=[{"statement": "가설", "evidence_ids": ["logs"]}])


def test_one_repair_preserves_both_responses_and_charges_budget(state, monkeypatch):
    state["evidence"] = [evidence(), evidence("logs", "no_data")]
    calls = fake_model(monkeypatch, state, [bad_reference(), decision()])
    result = asyncio.run(planner.choose_action(state, []))
    assert result["hypotheses"] == []
    assert len(calls) == 2
    assert calls[1]["messages"][2]["role"] == "assistant"
    feedback = json.loads(calls[1]["messages"][3]["content"])
    assert feedback["validation_feedback"]["code"] == "unavailable_evidence"
    assert feedback["allowed_hypothesis_evidence_ids"] == ["current_metrics"]
    receipts = sorted(
        [read_json(p) for p in Path(state["directory"]).glob("decision-*.json")],
        key=lambda r: r["attempt"],
    )
    first, second = receipts
    assert first["validation"]["code"] == "unavailable_evidence"
    assert second["validation"] == {"valid": True, "code": "ok"}
    assert first["decision_id"] == second["decision_id"]
    assert second["repair_of"] == first["attempt_id"]
    assert json.loads(first["response"]["message"]["content"]) == bad_reference()
    assert read_budget(state)["llm_calls"] == 2
    assert read_budget(state)["reserved_tokens"] == 2 * TOKEN_RESERVATION
    assert read_budget(state)["tool_calls"] == 0


def test_failed_repair_terminates_with_diagnostic_in_report(state, monkeypatch):
    state["evidence"] = [evidence(), evidence("logs", "no_data")]
    calls = fake_model(monkeypatch, state, [bad_reference(), bad_reference()])
    update = asyncio.run(nodes.AgentNodes().decide(state))
    assert len(calls) == 2
    assert update["stop_reason"] == "decision_error"
    assert update["decision_error"]["code"] == "unavailable_evidence"
    state.update(update)
    report = build_report(state)
    assert report["status"] == "incomplete"
    assert report["decision_error"]["code"] == "unavailable_evidence"
    assert report["hypotheses"] == []


@pytest.mark.parametrize("constraint", ["llm", "tokens", "time", "context"])
def test_repair_obeys_existing_budgets_and_size_limit(state, monkeypatch, constraint):
    if constraint == "llm":
        state["limits"]["llm_calls"] = 1
    if constraint == "tokens":
        state["limits"]["reserved_tokens"] = TOKEN_RESERVATION

    def first_response():
        if constraint == "time":
            state["deadline"] = time.time() - 1
        if constraint == "context":
            return {"unexpected": "x" * NUM_CTX}
        return bad_reference()

    calls = fake_model(monkeypatch, state, [first_response])
    expected = planner.ContextTooLarge if constraint == "context" else BudgetExceeded
    with pytest.raises(expected):
        asyncio.run(planner.choose_action(state, []))
    assert len(calls) == 1
    assert read_budget(state)["llm_calls"] == 1
    assert len(list(Path(state["directory"]).glob("decision-*.json"))) == 1


def test_saved_prompt_version_is_not_relabelled_on_resume(state, monkeypatch):
    state.pop("prompt_version")  # 이전 v3 체크포인트의 ops_agent_v2 프롬프트
    fake_model(monkeypatch, state, [decision()])
    asyncio.run(planner.choose_action(state, []))
    receipt = read_json(next(Path(state["directory"]).glob("decision-*.json")))
    assert receipt["prompt_version"] == "ops_agent_v2"


@pytest.mark.parametrize(
    "case",
    read_json(PROJECT_DIR / "evals/agent-reference-cases.json")["cases"],
    ids=lambda case: case["case_id"],
)
def test_regression_cases_separate_reference_validity_from_semantic_review(case):
    state = {"evidence": case["evidence"]}
    content = json.dumps(case["decision"])
    with pytest.raises(DecisionValidationError) as exc:
        planner.validate_decision(content, state, case["available_tools"])
    assert exc.value.code == case["expected_claim_validation_error"]
    assert case["semantic_expectation"] == "unsupported_hypothesis"
