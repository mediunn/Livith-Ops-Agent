import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops_agent.agent.budget import read_budget
from ops_agent.agent.http import planner as planners
from ops_agent.agent.http.context import context_and_queries, identity, validate_choice
from ops_agent.agent.http.graph import HTTPNodes, initial_state
from ops_agent.agent.http.session import run
from ops_agent.agent.http.state import HTTPDecision
from ops_agent.evaluation.http_agent import (
    CASES,
    SCOPE,
    evaluate,
    fixture_executor,
    score,
)
from ops_agent.persistence.artifacts import read_json
from ops_agent.tools.http import HTTPQuery


@pytest.fixture
def state(tmp_path):
    return initial_state(
        tmp_path / "run", HTTPQuery(**SCOPE), symptom=CASES[0]["symptom"]
    )


async def seed(state, case=CASES[0]):
    active = {**state, "deadline": time.time() + 30}
    executor = fixture_executor(case)
    state["evidence"] = [
        await executor(active, HTTPQuery(**{**SCOPE, "metric": metric}))
        for metric in ("http_request_rate", "http_mean_latency")
    ]
    return state


def choose(
    context, *, route="/checkout", metric="http_mean_latency", window="previous"
):
    endpoint = next(e for e in context["endpoints"] if e["route"] == route)
    return HTTPDecision(
        action="query",
        endpoint_id=endpoint["id"],
        metric=metric,
        window=window,
        rationale="직전 구간으로 증가 가설을 확인한다.",
    )


def test_choices_change_actual_queries_and_hypothesis_can_be_rejected(state):
    async def planner(active, context):
        latency = [e for e in active["evidence"] if e["action"] == "http_mean_latency"]
        hypothesis = {
            "id": "h1",
            "statement": "checkout 평균 지연이 직전 구간보다 증가했다.",
            "status": "unverified" if len(latency) == 1 else "rejected",
            "evidence_ids": [e["evidence_id"] for e in latency],
        }
        if len(latency) == 1:
            return choose(context).model_copy(
                update={"hypotheses": []}
            ).model_dump() | {"hypotheses": [hypothesis]}
        return {
            "action": "finish",
            "rationale": "두 구간 모두 800ms라 증가 가설을 기각한다.",
            "hypotheses": [hypothesis],
        }

    report = asyncio.run(
        run(
            Path(state["directory"]),
            state,
            planner=planner,
            tool_executor=fixture_executor(CASES[2]),
        )
    )
    assert report["status"] == "completed"
    assert report["budget"]["tool_calls"] == 3
    assert report["facts"][-1]["measurement"]["route"] == "/checkout"
    assert [d["hypotheses"][0]["status"] for d in report["decisions"]] == [
        "unverified",
        "rejected",
    ]
    assert report["interpretation"]["unreviewed_evidence_ids"] == []
    assert score(CASES[2], report)["hypothesis_status_match"]


def test_zero_traffic_endpoint_and_time_windows_remain_selectable(state):
    asyncio.run(seed(state, CASES[1]))
    context, candidates = context_and_queries(state)
    order = next(e["id"] for e in context["endpoints"] if e["route"] == "/orders")
    assert context["endpoints"][0]["route"] == "/internal/poll"
    for window in ("previous", "first_half", "second_half"):
        _, query = validate_choice(
            choose(context, route="/orders", metric="http_request_rate", window=window),
            state,
        )
        assert query == candidates[(order, "http_request_rate", window)]
    second = candidates[(order, "http_request_rate", "second_half")]
    assert second.start_at.hour == 13 and second.start_at.minute == 30
    assert second.end_at.hour == 14
    active = {**state, "deadline": time.time() + 30}
    state["evidence"].append(asyncio.run(fixture_executor(CASES[1])(active, second)))
    assert (order, "http_request_rate", "second_half") not in context_and_queries(
        state
    )[1]


@pytest.mark.parametrize(
    "update",
    [
        {"endpoint_id": "invented"},
        {"window": "all_history"},
        {"expr": "up"},
        {
            "hypotheses": [
                {
                    "id": "h1",
                    "statement": "test",
                    "status": "supported",
                    "evidence_ids": ["invented"],
                }
            ]
        },
        {"action": "finish"},
    ],
)
def test_bad_decisions_rejected_before_tool_io(state, update):
    asyncio.run(seed(state))
    context, _ = context_and_queries(state)
    with pytest.raises(ValueError):
        validate_choice(choose(context).model_dump() | update, state)
    assert read_budget(state)["tool_calls"] == 2


def test_execution_boundary_rejects_scope_change_and_duplicates(state):
    asyncio.run(seed(state))
    nodes = HTTPNodes(None, tool_executor=lambda *args: pytest.fail("must not call"))
    for query in [HTTPQuery(**SCOPE), HTTPQuery(**SCOPE, route="/unseen")]:
        state["pending_query"] = query.model_dump(mode="json")
        with pytest.raises(ValueError):
            asyncio.run(nodes._query(state))


def test_scoped_request_does_not_admit_other_endpoints(state):
    asyncio.run(seed(state))
    state["request"]["route"] = "/checkout"
    # Simulate a backend returning an extra label despite the scope selector.
    for item in state["evidence"]:
        item["query_key"] = identity(
            HTTPQuery.model_validate({**state["request"], "metric": item["action"]})
        )
    context, _ = context_and_queries(state)
    assert [e["route"] for e in context["endpoints"]] == ["/checkout"]


def test_no_data_finishes_without_planner_even_at_tool_limit(state):
    state["limits"]["tool_calls"] = 2

    async def fail(*args):
        pytest.fail("single finish outcome must bypass model")

    report = asyncio.run(
        run(
            Path(state["directory"]),
            state,
            planner=fail,
            tool_executor=fixture_executor(CASES[3]),
        )
    )
    assert report["stop_reason"] == "no_candidates"
    assert report["status"] == "completed"
    assert report["budget"]["llm_calls"] == 0


def test_budget_exhaustion_marks_unreviewed_observations(state):
    state["limits"]["tool_calls"] = 3

    async def planner(active, context):
        return choose(context)

    report = asyncio.run(
        run(
            Path(state["directory"]),
            state,
            planner=planner,
            tool_executor=fixture_executor(CASES[0]),
        )
    )
    assert report["stop_reason"] == "tool_budget"
    assert report["status"] == "incomplete"
    assert len(report["decisions"]) == 1
    assert report["interpretation"]["unreviewed_evidence_ids"] == [
        report["facts"][-1]["evidence_id"]
    ]


def test_sqlite_resume_excludes_pause_and_preserves_evidence(state, monkeypatch):
    directory = Path(state["directory"])
    executor = fixture_executor(CASES[0])
    paused = asyncio.run(
        run(
            directory,
            state,
            step=True,
            planner=planners.rule_planner,
            tool_executor=executor,
        )
    )
    assert paused["status"] == "paused" and paused["evidence_count"] == 1
    first = read_json(next(directory.glob("queries/*.json")))
    original_time = time.time
    monkeypatch.setattr(time, "time", lambda: original_time() + 86400)
    report = asyncio.run(
        run(directory, planner=planners.rule_planner, tool_executor=executor)
    )
    assert report["status"] == "completed"
    assert report["remaining_seconds"] > 170
    assert report["budget"]["tool_calls"] == 4
    assert report["facts"][0]["evidence_id"] == first["evidence_id"]
    assert asyncio.run(run(directory)) == report


def test_deadline_and_invalid_planner_fail_closed(state):
    async def bad(*args):
        return {"action": "query", "expr": "up"}

    nodes = HTTPNodes(bad)
    assert (
        asyncio.run(nodes.decide({**state, "remaining_seconds": 0}))["stop_reason"]
        == "time_budget"
    )
    asyncio.run(seed(state))
    result = asyncio.run(nodes.decide(state))
    assert result["stop_reason"] == "decision_error"
    assert read_budget(state)["tool_calls"] == 2


@pytest.mark.parametrize(
    "responses,expected_calls,error",
    [
        ([{"action": "finish", "rationale": "충분한 근거"}], 1, None),
        (
            [
                {
                    "action": "query",
                    "endpoint_id": "invented",
                    "metric": "http_request_rate",
                    "window": "previous",
                    "rationale": "test",
                },
                {"action": "finish", "rationale": "재검토 후 종료"},
            ],
            2,
            None,
        ),
        ([{}, {}], 2, ValueError),
    ],
)
def test_llm_validation_repair_and_receipts(
    state, monkeypatch, responses, expected_calls, error
):
    asyncio.run(seed(state))
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def chat(self, **kwargs):
            calls.append(kwargs)
            content = json.dumps(responses[len(calls) - 1])
            return SimpleNamespace(
                done=True,
                done_reason="stop",
                prompt_eval_count=100,
                eval_count=20,
                message=SimpleNamespace(content=content),
                model_dump=lambda **_: {"content": content},
            )

    monkeypatch.setattr(planners, "AsyncClient", Client)
    active = {**state, "deadline": time.time() + 30}
    coroutine = planners.llm_planner(active, context_and_queries(state)[0])
    if error:
        with pytest.raises(error):
            asyncio.run(coroutine)
    else:
        assert asyncio.run(coroutine).action == "finish"
    assert len(calls) == read_budget(state)["llm_calls"] == expected_calls
    receipts = list(Path(state["directory"]).glob("decision-*.json"))
    assert len(receipts) == expected_calls
    assert all(read_json(p)["response"] is not None for p in receipts)
    assert calls[0]["format"]["properties"]["action"]["enum"] == ["query", "finish"]


def test_rule_baseline_runs_same_cases_and_reports_misses(tmp_path):
    result = asyncio.run(evaluate(tmp_path / "evaluation"))
    assert len(result["results"]) == 4
    assert all(r["checks"]["completed"] for r in result["results"])
    assert result["results"][0]["checks"]["targeted_previous_query"] is False
    assert all(r["budget"]["llm_calls"] == 0 for r in result["results"])


def test_slow_injected_planner_is_stopped_by_time_budget(state):
    asyncio.run(seed(state))

    async def slow(*args):
        await asyncio.sleep(1)
        pytest.fail("planner should have been cancelled")

    state["remaining_seconds"] = 0.02
    result = asyncio.run(HTTPNodes(slow).decide(state))
    assert result["stop_reason"] == "time_budget"
    assert result["remaining_seconds"] == 0


@pytest.mark.parametrize("failure", ["truncated", "llm_budget", "input_bytes"])
def test_llm_limits_do_not_retry(state, monkeypatch, failure):
    asyncio.run(seed(state))
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def chat(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                done=True,
                done_reason="length",
                prompt_eval_count=100,
                eval_count=1024,
                model_dump=lambda **_: {"done_reason": "length"},
            )

    monkeypatch.setattr(planners, "AsyncClient", Client)
    if failure == "llm_budget":
        state["limits"]["llm_calls"] = 0
    if failure == "input_bytes":
        monkeypatch.setattr(planners, "MAX_INPUT_BYTES", 1)
    with pytest.raises((RuntimeError, ValueError)):
        asyncio.run(
            planners.llm_planner(
                {**state, "deadline": time.time() + 30}, context_and_queries(state)[0]
            )
        )
    assert len(calls) == (1 if failure == "truncated" else 0)
