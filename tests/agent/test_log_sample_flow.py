import asyncio
import json
import time
from copy import deepcopy
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from ops_agent.agent.budget import read_budget
from ops_agent.agent.decision.planner import build_context, choose_action
from ops_agent.agent.decision.validation import (
    DecisionValidationError,
    decision_schema,
    validate_decision,
)
from ops_agent.agent.graph import build_graph
from ops_agent.agent.log_samples import SAMPLE_ACTION, sample_candidates
from ops_agent.agent.nodes import AgentNodes
from ops_agent.agent.policy import allowed_actions
from ops_agent.evaluation.agent import CASES_PATH, synthetic_collector
from ops_agent.persistence.artifacts import query_artifact_path, read_json, save_json
from ops_agent.reporting.agent_report import build_report
from ops_agent.telemetry.langfuse import ReportTrace
from tests.helpers.agent import fake_model
from tests.telemetry.test_langfuse import FakeClient


@pytest.fixture
def sample_state(current_state):
    state = current_state
    state["version"] = 5
    case = deepcopy(read_json(CASES_PATH)["cases"][0])
    case.update(
        current_rate=1, previous_rate=1, log_levels={"info": 12}, truncated=False
    )
    collect = synthetic_collector(case)
    for action in ("current_metrics", "previous_metrics", "logs"):
        state["action"] = action
        state["evidence"].append(asyncio.run(collect(state)))
    return state


def sample_choice(state, cursor=0, limit=5):
    return {
        "action": SAMPLE_ACTION,
        "claim_ids": [],
        "log_sample_request": {
            "evidence_id": state["evidence"][-1]["evidence_id"],
            "cursor": cursor,
            "limit": limit,
        },
    }


def read_page(state, cursor=0, limit=5):
    choice = sample_choice(state, cursor, limit)
    state.update(action=SAMPLE_ACTION, log_sample_request=choice["log_sample_request"])
    update = asyncio.run(AgentNodes().query(state))
    assert "stop_reason" not in update
    state.update(update)
    return state["log_sample_pages"][-1]


def test_long_unicode_page_fits_model_context_without_changing_saved_samples(
    sample_state, monkeypatch
):
    state = sample_state
    read_page(state)
    for sample in state["log_sample_pages"][-1]["samples"]:
        sample["line"] = "가" * 500
    original = deepcopy(state["log_sample_pages"])
    calls = fake_model(monkeypatch, state, [{"action": "finish", "claim_ids": []}])
    result = asyncio.run(choose_action(state, [SAMPLE_ACTION]))
    assert result["action"] == "finish"
    assert state["log_sample_pages"] == original
    context = json.loads(calls[0]["messages"][1]["content"])
    assert context["log_sample_page"]["samples"]
    assert context["log_sample_context_truncated"] is True
    assert result["log_sample_context_truncated"] is True
    assert len(context["log_sample_page"]["samples"][0]["line"]) < 500


def test_schema_and_context_offer_only_valid_local_candidates(sample_state):
    state = sample_state
    schema = decision_schema(state, [SAMPLE_ACTION])
    assert schema["properties"]["action"]["enum"] == [SAMPLE_ACTION, "finish"]
    assert schema["$defs"]["LogSampleRequest"]["properties"]["evidence_id"]["enum"] == [
        state["evidence"][-1]["evidence_id"]
    ]
    context = build_context(state, [SAMPLE_ACTION])
    assert context["log_sample_candidates"][0]["cursor"] == 0
    assert context["log_sample_page"] is None
    assert context["scope"]["log_bodies_included"] is False


def test_new_investigation_reads_samples_then_queries_and_finishes(
    current_state, tmp_path, monkeypatch
):
    state = current_state
    state["version"] = 5
    case = deepcopy(read_json(CASES_PATH)["cases"][0])
    case.update(
        current_rate=1,
        previous_rate=1,
        log_levels={"info": 2},
        warning_query_levels={"warn": 1},
        truncated=True,
    )

    def choose_sample():
        context = json.loads(calls[-1]["messages"][1]["content"])
        candidate = context["log_sample_candidates"][0]
        return {
            "action": SAMPLE_ACTION,
            "claim_ids": [],
            "log_sample_request": {
                "evidence_id": candidate["evidence_id"],
                "cursor": 0,
                "limit": 5,
            },
        }

    def finish():
        context = json.loads(calls[-1]["messages"][1]["content"])
        return {
            "action": "finish",
            "claim_ids": [c["claim_id"] for c in context["verified_claims"]],
        }

    calls = fake_model(
        monkeypatch,
        state,
        [
            {"action": "previous_metrics", "claim_ids": []},
            {"action": "logs", "claim_ids": []},
            choose_sample,
            {"action": "warning_logs", "claim_ids": []},
            finish,
        ],
    )

    async def scenario():
        async with AsyncSqliteSaver.from_conn_string(
            str(tmp_path / "full.sqlite")
        ) as saver:
            graph = build_graph(saver, tool_executor=synthetic_collector(case))
            return await graph.ainvoke(
                state, {"configurable": {"thread_id": state["thread_id"]}}
            )

    result = asyncio.run(scenario())
    report = result["report"]
    assert report["status"] == "completed"
    assert report["required_checks"]["missing"] == []
    assert report["log_sample_reads"]["pages_read"] == 1
    assert report["log_sample_reads"]["unreviewed_pages"] == []
    assert report["warning_log_followup"]["queried"] is True
    assert report["hypotheses"] == []
    assert read_budget(state)["tool_calls"] == 4
    assert read_budget(state)["llm_calls"] == 5
    context = json.loads(calls[3]["messages"][1]["content"])
    assert context["log_sample_page"]["samples"][0]["line"] == "Synthetic fixture."
    assert "warning_logs" in context["allowed_actions"]


@pytest.mark.parametrize(
    "mutation", ["required", "old_version", "empty", "wrong_scope", "failed"]
)
def test_unavailable_samples_cannot_be_selected_or_executed(sample_state, mutation):
    state = sample_state
    choice = sample_choice(state)
    if mutation == "required":
        state["evidence"].pop(1)
    elif mutation == "old_version":
        state["version"] = 4
    elif mutation == "empty":
        state["evidence"][-1].update(status="no_data", summary={"log_count": 0})
    elif mutation == "wrong_scope":
        state["evidence"][-1]["arguments"]["direction"] = "forward"
    else:
        state["evidence"][-1]["status"] = "tool_error"
    assert SAMPLE_ACTION not in allowed_actions(state, [SAMPLE_ACTION])
    with pytest.raises(DecisionValidationError):
        validate_decision(json.dumps(choice), state, [SAMPLE_ACTION])
    state.update(action=SAMPLE_ACTION, log_sample_request=choice["log_sample_request"])
    assert (
        asyncio.run(AgentNodes().query(state))["stop_reason"]
        == "log_sample_request_blocked"
    )


@pytest.mark.parametrize(
    "request_update",
    [
        None,
        {"cursor": 1},
        {"cursor": True},
        {"limit": 6},
        {"limit": "5"},
        {"evidence_id": "unknown"},
    ],
)
def test_invalid_arguments_are_rejected(sample_state, request_update):
    choice = sample_choice(sample_state)
    if request_update is None:
        choice.pop("log_sample_request")
    else:
        choice["log_sample_request"].update(request_update)
    with pytest.raises(DecisionValidationError):
        validate_decision(json.dumps(choice), sample_state, [SAMPLE_ACTION])


def test_non_sample_action_cannot_smuggle_sample_request(sample_state):
    choice = sample_choice(sample_state)
    choice["action"] = "finish"
    with pytest.raises(DecisionValidationError) as error:
        validate_decision(json.dumps(choice), sample_state, [SAMPLE_ACTION])
    assert error.value.code == "unexpected_log_sample_request"


def test_pages_do_not_repeat_and_share_a_global_limit(sample_state):
    state = sample_state
    before = read_budget(state)
    evidence_before = deepcopy(state["evidence"])
    first = read_page(state, limit=3)
    assert first["next_cursor"] == 3
    for cursor in (0, 2, 4):
        with pytest.raises(DecisionValidationError):
            validate_decision(
                json.dumps(sample_choice(state, cursor)), state, [SAMPLE_ACTION]
            )
    read_page(state, cursor=3)
    assert sample_candidates(state) == []
    assert allowed_actions(state, [SAMPLE_ACTION]) == ["finish"]
    assert state["evidence"] == evidence_before
    assert read_budget(state) == before
    assert len(list((Path(state["directory"]) / "log-samples").glob("*.json"))) == 2
    assert build_report(state)["log_sample_reads"]["page_limit_reached"] is True


def test_only_latest_sample_body_enters_context(sample_state):
    state = sample_state
    read_page(state)["samples"][0]["line"] = "FIRST_PAGE_MARKER"
    read_page(state, cursor=5)["samples"][0]["line"] = "LATEST_PAGE_MARKER"
    context = build_context(state, [])
    assert "FIRST_PAGE_MARKER" not in json.dumps(context)
    assert "LATEST_PAGE_MARKER" in json.dumps(context)
    assert context["scope"]["log_bodies_included"] is True
    report = build_report(state)
    assert "LATEST_PAGE_MARKER" not in json.dumps(report)
    assert len(report["log_sample_reads"]["unreviewed_pages"]) == 2
    assert report["hypotheses"] == []


def test_exhausted_remote_tool_budget_still_allows_local_read(
    sample_state, monkeypatch
):
    state = sample_state
    state["limits"]["tool_calls"] = read_budget(state)["tool_calls"]
    fake_model(monkeypatch, state, [sample_choice(state)])
    state.update(asyncio.run(AgentNodes().decide(state)))
    assert state["action"] == SAMPLE_ACTION
    state.update(asyncio.run(AgentNodes().query(state)))
    assert len(state["log_sample_pages"]) == 1
    assert read_budget(state)["tool_calls"] == 3


def test_invalid_cursor_repair_keeps_allowed_candidates(sample_state, monkeypatch):
    state = sample_state
    calls = fake_model(
        monkeypatch, state, [sample_choice(state, 1), sample_choice(state)]
    )
    result = asyncio.run(choose_action(state, [SAMPLE_ACTION]))
    assert result["log_sample_request"]["cursor"] == 0
    feedback = json.loads(calls[1]["messages"][3]["content"])
    assert feedback["validation_feedback"]["code"] == "invalid_log_sample_request"
    assert feedback["log_sample_candidates"][0]["cursor"] == 0


def test_sqlite_resume_preserves_pages_and_returns_them_to_planner(
    sample_state, tmp_path, monkeypatch
):
    state = sample_state
    calls = fake_model(
        monkeypatch,
        state,
        [
            sample_choice(state),
            sample_choice(state, 5),
            {"action": "finish", "claim_ids": []},
        ],
    )
    database = str(tmp_path / "samples.sqlite")
    config = {"configurable": {"thread_id": state["thread_id"]}}

    async def forbidden(*args, **kwargs):
        pytest.fail("A local page read reached the Grafana collector")

    async def scenario():
        async with AsyncSqliteSaver.from_conn_string(database) as saver:
            graph = build_graph(saver, tool_executor=forbidden)
            await graph.ainvoke(
                state, config, interrupt_after=["query"], durability="sync"
            )
            snapshot = await graph.aget_state(config)
            assert snapshot.next == ("decide",)
            assert len(snapshot.values["log_sample_pages"]) == 1
        async with AsyncSqliteSaver.from_conn_string(database) as saver:
            return await build_graph(saver, tool_executor=forbidden).ainvoke(
                None, config, durability="sync"
            )

    result = asyncio.run(scenario())
    assert result["report"]["status"] == "completed"
    assert result["report"]["report_version"] == "agent-report-v5"
    assert result["report"]["log_sample_reads"]["unreviewed_pages"] == []
    assert result["report"]["log_sample_reads"]["pages_read"] == 2
    assert read_budget(state)["tool_calls"] == 3
    assert read_budget(state)["llm_calls"] == 3
    context = json.loads(calls[1]["messages"][1]["content"])
    assert context["log_sample_candidates"][0]["cursor"] == 5
    assert context["log_sample_page"]["samples"][0]["line"] == "Synthetic fixture."
    assert result["evidence"] == state["evidence"]


def test_resumed_pending_duplicate_is_blocked_before_read(sample_state, tmp_path):
    state = sample_state
    read_page(state)
    state.update(
        action=SAMPLE_ACTION,
        log_sample_request=sample_choice(state)["log_sample_request"],
    )
    config = {"configurable": {"thread_id": state["thread_id"]}}
    database = str(tmp_path / "duplicate.sqlite")

    async def scenario():
        async with AsyncSqliteSaver.from_conn_string(database) as saver:
            await build_graph(saver).aupdate_state(config, state, as_node="decide")
        async with AsyncSqliteSaver.from_conn_string(database) as saver:
            return await build_graph(saver).ainvoke(None, config)

    result = asyncio.run(scenario())
    assert result["stop_reason"] == "log_sample_request_blocked"
    assert len(result["log_sample_pages"]) == 1
    assert result["report"]["status"] == "incomplete"


@pytest.mark.parametrize("failure", ["time", "artifact", "model_budget"])
def test_failures_do_not_claim_sample_was_reviewed(sample_state, failure):
    state = sample_state
    if failure == "model_budget":
        read_page(state)
        state["limits"]["llm_calls"] = 0
        update = asyncio.run(AgentNodes().decide(state))
        assert update["stop_reason"] == "llm_budget"
    else:
        state.update(
            action=SAMPLE_ACTION,
            log_sample_request=sample_choice(state)["log_sample_request"],
        )
        if failure == "time":
            state["deadline"] = time.time() - 1
        else:
            item = state["evidence"][-1]
            path = query_artifact_path(
                state["directory"], item["tool"], item["arguments"]
            )
            record = read_json(path)
            record["thread_id"] = "other"
            save_json(path, record)
        update = asyncio.run(AgentNodes().query(state))
        assert update["stop_reason"] == (
            "time_budget" if failure == "time" else "log_sample_error"
        )
        assert not state["log_sample_pages"]
    state.update(update)
    report = build_report(state)
    assert report["status"] == "incomplete"
    if failure == "model_budget":
        assert len(report["log_sample_reads"]["unreviewed_pages"]) == 1


def test_sample_bodies_and_echoed_responses_stay_out_of_trace(
    sample_state, monkeypatch
):
    from ops_agent.telemetry import langfuse

    state = sample_state
    marker = "LOCAL_ONLY_LOG_BODY"
    read_page(state)["samples"][0]["line"] = marker
    calls = fake_model(
        monkeypatch,
        state,
        [{"action": marker, "claim_ids": []}, {"action": "finish", "claim_ids": []}],
    )
    client = FakeClient()
    monkeypatch.setattr(langfuse, "configured_client", lambda: client)
    record = {
        "run_id": "trace-test",
        "model": state["model"],
        "prompt_version": state["prompt_version"],
        "context_version": "agent-state-v5",
        "prompt_sha256": state["prompt_sha256"],
        "status": "running",
        "report": None,
    }
    trace = ReportTrace(record)
    result = asyncio.run(choose_action(state, [], trace))
    state.update(decisions=[result], stop_reason="model_finished")
    record.update(report=build_report(state), status="completed")
    trace.finish()
    assert marker in json.dumps(calls)
    assert marker not in json.dumps(record)
    assert marker not in json.dumps(client.root.updates)
    for arguments, span in client.root.children:
        assert marker not in json.dumps(arguments)
        assert marker not in json.dumps(span.updates)
    assert any(
        marker in p.read_text()
        for p in Path(state["directory"]).glob("decision-*.json")
    )
