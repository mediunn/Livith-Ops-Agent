import asyncio
import json

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from ops_agent.agent.budget import read_budget
from ops_agent.agent.decision.planner import build_context
from ops_agent.agent.decision.validation import (
    DecisionValidationError,
    validate_decision,
)
from ops_agent.agent.graph import build_graph
from ops_agent.agent.nodes import AgentNodes
from ops_agent.agent.policy import allowed_actions, warning_log_followup
from ops_agent.evaluation.agent import evaluate_state
from ops_agent.reporting.agent_report import build_report
from ops_agent.tools.grafana import query_spec
from tests.helpers.observations import observe


@pytest.mark.parametrize(
    "case,eligible,reason",
    [
        ("zero_no_logs", False, "base_result_complete"),
        ("complete_warning_logs", False, "base_result_complete"),
        ("rate_increase", False, "base_result_complete"),
        ("truncated_info_hidden_warning", True, "base_result_truncated"),
        ("unknown_log_completeness", True, "base_completeness_unknown"),
    ],
)
def test_query_eligibility_and_auditable_basis(current_state, case, eligible, reason):
    observe(current_state, ["current_metrics", "previous_metrics", "logs"], case)
    result = warning_log_followup(current_state)
    assert result == {
        "eligible": eligible,
        "reason": reason,
        "basis_evidence_ids": [current_state["evidence"][-1]["evidence_id"]],
        "queried": False,
    }
    assert (
        "warning_logs" in allowed_actions(current_state, ["warning_logs"])
    ) == eligible
    assert (
        build_context(current_state, ["warning_logs"])["warning_log_followup"] == result
    )
    current_state["stop_reason"] = "model_finished"
    assert build_report(current_state)["warning_log_followup"] == result


@pytest.mark.parametrize(
    "key,value",
    [
        ("server_results_truncated", None),
        ("server_results_truncated", "false"),
        ("limit_reached", None),
        ("possibly_truncated", None),
        ("log_count", True),
        ("log_count", -1),
        ("log_count", 0),
        ("requested_limit", 99),
        ("scope", None),
        ("status", "no_data"),
    ],
)
def test_incomplete_or_inconsistent_metadata_retains_query(current_state, key, value):
    observe(
        current_state, ["current_metrics", "previous_metrics", "logs"], "rate_increase"
    )
    current_state["evidence"][-1]["summary"][key] = value
    assert warning_log_followup(current_state)["eligible"]


@pytest.mark.parametrize("count,eligible", [(99, False), (100, True)])
def test_limit_boundary_is_conservative_even_with_false_flags(
    current_state, count, eligible
):
    observe(
        current_state, ["current_metrics", "previous_metrics", "logs"], "rate_increase"
    )
    current_state["evidence"][-1]["summary"]["log_count"] = count
    assert warning_log_followup(current_state)["eligible"] == eligible


@pytest.mark.parametrize(
    "mutation", ["missing_metrics", "different_window", "wrong_tool"]
)
def test_required_checks_take_priority(current_state, mutation):
    observe(current_state, ["current_metrics", "previous_metrics", "logs"])
    if mutation == "missing_metrics":
        current_state["evidence"].pop(0)
    elif mutation == "different_window":
        current_state["evidence"][-1]["arguments"]["direction"] = "forward"
    else:
        current_state["evidence"][-1]["tool"] = "other"
    assert warning_log_followup(current_state)["reason"] == "required_checks_pending"
    assert "warning_logs" not in allowed_actions(current_state, ["warning_logs"])


def test_blocked_choice_and_resumed_query_cannot_issue_io(current_state):
    observe(current_state, ["current_metrics", "previous_metrics", "logs"])
    with pytest.raises(DecisionValidationError) as exc:
        validate_decision(
            json.dumps({"action": "warning_logs", "claim_ids": []}),
            current_state,
            ["warning_logs"],
        )
    assert exc.value.code == "invalid_action"

    async def forbidden(*args, **kwargs):
        pytest.fail("Blocked optional query reached IO")

    current_state["action"] = "warning_logs"
    result = asyncio.run(AgentNodes(tool_executor=forbidden).query(current_state))
    assert result == {"stop_reason": "optional_query_blocked"}
    current_state.update(result)
    assert build_report(current_state)["status"] == "incomplete"


@pytest.mark.parametrize("expected,queried", [(False, True), (True, False)])
def test_evaluation_catches_both_overquery_and_underquery(
    current_state, expected, queried
):
    actions = ["current_metrics", "previous_metrics", "logs"]
    if queried:
        actions.append("warning_logs")
    observe(current_state, actions)
    result = evaluate_state(
        current_state,
        {"claim_kinds": [], "empty_hypotheses": True, "warning_query": expected},
    )
    assert not result["checks"]["warning_query_selection"]
    assert not result["checks"]["expected_tool_count"]


def test_warning_query_remains_same_scope_subset(current_state):
    base_tool, base = query_spec(current_state, "logs")
    extra_tool, extra = query_spec(current_state, "warning_logs")
    assert base_tool == extra_tool
    assert extra.pop("logql") == base.pop("logql") + ' |~ "(?i)(warn|error)"'
    assert base == extra


def test_sqlite_resume_rechecks_saved_warning_choice_before_io(current_state, tmp_path):
    observe(current_state, ["current_metrics", "previous_metrics", "logs"])
    current_state["action"] = "warning_logs"
    database = str(tmp_path / "pending-query.sqlite")
    config = {"configurable": {"thread_id": current_state["thread_id"]}}
    before = read_budget(current_state)

    async def forbidden(*args, **kwargs):
        pytest.fail("Resumed blocked query reached IO")

    async def scenario():
        async with AsyncSqliteSaver.from_conn_string(database) as saver:
            graph = build_graph(saver, tool_executor=forbidden)
            await graph.aupdate_state(config, current_state, as_node="decide")
            assert (await graph.aget_state(config)).next == ("query",)
        async with AsyncSqliteSaver.from_conn_string(database) as saver:
            return await build_graph(saver, tool_executor=forbidden).ainvoke(
                None, config
            )

    result = asyncio.run(scenario())
    assert result["stop_reason"] == "optional_query_blocked"
    assert result["report"]["status"] == "incomplete"
    assert result["evidence"] == current_state["evidence"]
    assert read_budget(current_state) == before
