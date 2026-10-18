import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops_agent.agent.budget import read_budget, reserve
from ops_agent.agent.http import planner as planners
from ops_agent.agent.http.context import context_and_queries, identity, validate_choice
from ops_agent.agent.http.graph import HTTPNodes, initial_state
from ops_agent.agent.http.session import run
from ops_agent.agent.http.state import HTTPDecision
from ops_agent.config import TOKEN_RESERVATION
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


def finish_reply(text="확보한 근거로 조사를 마쳤습니다.", evidence_ids=(), **updates):
    return {
        "action": "finish",
        "rationale": "확보한 근거와 확인 한계를 정리해 종료한다.",
        "answer": {
            "text": text,
            "evidence_ids": list(evidence_ids),
            "limitations": ["조회한 지표만으로 원인을 확정할 수 없습니다."],
        },
        **updates,
    }


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
        return finish_reply(
            "두 구간의 평균 지연이 모두 800ms로 증가가 확인되지 않았습니다.",
            [e["evidence_id"] for e in latency],
            rationale="두 구간 모두 800ms라 증가 가설을 기각한다.",
            hypotheses=[hypothesis],
        )

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
    assert "증가가 확인되지 않았습니다" in report["answer"]["text"]
    assert (
        report["answer"]["evidence_ids"]
        == report["interpretation"]["hypotheses"][0]["evidence_ids"]
    )
    directory = Path(state["directory"])
    assert read_json(directory / "report.json")["answer"] == report["answer"]
    assert asyncio.run(run(directory)) == report


def test_new_evidence_and_query_purpose_survive_sqlite_resume(state):
    directory = Path(state["directory"])
    contexts = []

    async def planner(active, context):
        contexts.append(context)
        if len(contexts) == 1:
            update = context["investigation_update"]
            assert update["last_query"] is None
            assert update["hypotheses_before_new_evidence"] == []
            assert update["new_evidence_ids"] == [
                e["evidence_id"] for e in active["evidence"]
            ]
            return choose(context).model_dump() | {
                "hypotheses": [
                    {
                        "id": "h1",
                        "statement": "checkout 지연 증가",
                        "status": "insufficient_evidence",
                        "evidence_ids": [active["evidence"][1]["evidence_id"]],
                    }
                ]
            }
        update = context["investigation_update"]
        new_id = active["evidence"][-1]["evidence_id"]
        assert update["new_evidence_ids"] == [new_id]
        assert update["last_query"]["result_evidence_ids"] == [new_id]
        assert update["last_query"]["window"] == "previous"
        assert update["last_query"]["purpose"] == active["decisions"][-1]["rationale"]
        assert (
            update["hypotheses_before_new_evidence"]
            == active["decisions"][-1]["hypotheses"]
        )
        assert {e["evidence_id"] for e in context["observations"]} == {
            e["evidence_id"] for e in active["evidence"]
        }
        # Presentation alone must not force a hypothesis or another query.
        return HTTPDecision.model_validate(
            finish_reply(evidence_ids=[e["evidence_id"] for e in active["evidence"]])
        )

    executor = fixture_executor(CASES[2])
    for index in range(3):
        paused = asyncio.run(
            run(
                directory,
                state if index == 0 else None,
                step=True,
                planner=planner,
                tool_executor=executor,
            )
        )
        assert paused["status"] == "paused"
    report = asyncio.run(run(directory, planner=planner, tool_executor=executor))
    assert report["status"] == "completed"
    assert len(contexts) == 2
    assert report["interpretation"]["hypotheses"] == []
    assert report["answer"]["evidence_ids"] == [
        e["evidence_id"] for e in report["facts"]
    ]


def test_last_query_result_is_matched_by_arguments_not_metric(state):
    asyncio.run(seed(state))
    active = {**state, "deadline": time.time() + 30}
    nodes = HTTPNodes(planners.rule_planner, tool_executor=fixture_executor(CASES[0]))
    decision = asyncio.run(nodes._decide(active))
    state["decisions"] = decision["decisions"]
    _, candidates = context_and_queries(state)
    # A different window of the same metric is new, but isn't this query's result.
    last = state["decisions"][-1]
    other = candidates[(last["endpoint_id"], last["metric"], "second_half")]
    state["evidence"].append(asyncio.run(fixture_executor(CASES[0])(active, other)))
    update = context_and_queries(state)[0]["investigation_update"]
    assert update["new_evidence_ids"] == [state["evidence"][-1]["evidence_id"]]
    assert update["last_query"]["result_evidence_ids"] == []


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


@pytest.mark.parametrize(
    "case,error",
    [
        ("missing", "missing_answer"),
        ("unknown_evidence", "unknown_answer_evidence"),
        ("query_answer", "answer_before_finish"),
    ],
)
def test_answer_contract_rejected_before_tool_io(state, case, error):
    asyncio.run(seed(state))
    reply = finish_reply(evidence_ids=[state["evidence"][0]["evidence_id"]])
    if case == "missing":
        del reply["answer"]
    elif case == "unknown_evidence":
        reply["answer"]["evidence_ids"] = ["invented"]
    else:
        reply = choose(context_and_queries(state)[0]).model_dump() | {
            "answer": reply["answer"]
        }
    before = read_budget(state)
    with pytest.raises(ValueError, match=error):
        validate_choice(reply, state)
    assert read_budget(state) == before


def test_finish_accepts_answer_with_actual_evidence(state):
    asyncio.run(seed(state))
    ids = [e["evidence_id"] for e in state["evidence"]]
    decision, query = validate_choice(finish_reply(evidence_ids=ids), state)
    assert query is None
    assert decision.answer.evidence_ids == ids


def test_rule_finish_does_not_require_model_answer(state):
    asyncio.run(seed(state))
    state["planner_kind"] = "rules"
    decision, query = validate_choice(
        {"action": "finish", "rationale": "규칙 비교를 마쳤다."}, state
    )
    assert query is None and decision.answer is None


def test_whitespace_only_answer_is_rejected(state):
    with pytest.raises(ValueError):
        validate_choice(finish_reply(text=" \n\t "), state)


@pytest.mark.parametrize("with_observations", [True, False])
def test_answer_schema_limits_references_to_visible_observations(
    state, with_observations
):
    if with_observations:
        asyncio.run(seed(state))
    context, _ = context_and_queries(state)
    schema = planners.decision_schema(context)
    expected_ids = [e["evidence_id"] for e in context["observations"]]
    assert "answer" in schema["required"]
    for name in ("HTTPHypothesis", "HTTPAnswer"):
        field = schema["$defs"][name]["properties"]["evidence_ids"]
        if expected_ids:
            assert field["items"]["enum"] == expected_ids
        else:
            assert field["maxItems"] == 0
            assert "enum" not in field["items"]
    if not with_observations:
        assert schema["properties"]["endpoint_id"] == {"type": "null"}


def test_v1_checkpoint_is_rejected_without_more_collection(state):
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from ops_agent.agent.http.graph import build_graph

    directory = Path(state["directory"])
    executor = fixture_executor(CASES[0])
    asyncio.run(run(directory, state, step=True, tool_executor=executor))

    async def restore_old_version():
        async with AsyncSqliteSaver.from_conn_string(
            str(directory / "checkpoints.sqlite")
        ) as saver:
            graph = build_graph(saver, planner=None, tool_executor=executor)
            await graph.aupdate_state(
                {"configurable": {"thread_id": directory.name}},
                {"contract_version": "http-agent-v1"},
            )

    asyncio.run(restore_old_version())
    before = read_budget(state)

    async def must_not_collect(*args):
        pytest.fail("unsupported checkpoints must not collect data")

    with pytest.raises(ValueError, match="지원하지 않는 HTTP 조사 상태"):
        asyncio.run(run(directory, tool_executor=must_not_collect))
    assert read_budget(state) == before
    assert not (directory / "report.json").exists()


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


@pytest.mark.parametrize("exhausted", ["llm", "token"])
def test_budget_exhaustion_marks_unreviewed_observations(state, exhausted):
    state["limits"]["tool_calls"] = 3
    if exhausted == "llm":
        state["limits"]["llm_calls"] = 1
    else:
        state["limits"]["reserved_tokens"] = TOKEN_RESERVATION

    async def planner(active, context):
        reserve(active, "llm")
        return choose(context)

    report = asyncio.run(
        run(
            Path(state["directory"]),
            state,
            planner=planner,
            tool_executor=fixture_executor(CASES[0]),
        )
    )
    assert report["stop_reason"] == f"{exhausted}_budget"
    assert report["status"] == "incomplete"
    assert report["budget"]["tool_calls"] == 3
    assert report["budget"]["llm_calls"] == 1
    assert len(report["decisions"]) == 1
    assert report["interpretation"]["unreviewed_evidence_ids"] == [
        report["facts"][-1]["evidence_id"]
    ]
    assert report["answer"] is None


@pytest.mark.parametrize(
    "resume,repair_finish", [(False, False), (True, False), (False, True)]
)
def test_tool_budget_allows_final_review_without_more_queries(
    state, monkeypatch, resume, repair_finish
):
    state["limits"]["tool_calls"] = 3
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def chat(self, **kwargs):
            context = json.loads(kwargs["messages"][1]["content"])
            calls.append(context)
            if len(calls) == 1:
                assert context["remaining_tool_calls"] == 1
                payload = choose(context).model_dump(mode="json")
            else:
                assert context["remaining_tool_calls"] == 0
                assert context["available_queries"] == []
                assert context["finish_only_reason"] == "tool_budget"
                assert kwargs["format"]["properties"]["action"]["enum"] == ["finish"]
                if repair_finish and len(calls) == 2:
                    # Simulate a model ignoring the finish-only schema.
                    payload = choose(context).model_dump(mode="json")
                else:
                    payload = finish_reply(
                        "직전 구간과 현재 구간의 평균 지연을 비교했습니다.",
                        [e["evidence_id"] for e in context["observations"]],
                    )
                    payload["answer"]["limitations"].append(
                        "추가 조회 한도에 도달했습니다."
                    )
            content = json.dumps(payload, ensure_ascii=False)
            return SimpleNamespace(
                done=True,
                done_reason="stop",
                prompt_eval_count=100,
                eval_count=20,
                message=SimpleNamespace(content=content),
                model_dump=lambda **_: {"message": {"content": content}},
            )

    monkeypatch.setattr(planners, "AsyncClient", Client)
    directory = Path(state["directory"])
    executor = fixture_executor(CASES[0])
    if resume:
        for step in range(3):
            paused = asyncio.run(
                run(
                    directory,
                    state if step == 0 else None,
                    step=True,
                    planner=planners.llm_planner,
                    tool_executor=executor,
                )
            )
            assert paused["status"] == "paused"
        assert paused["evidence_count"] == 3
        assert len(calls) == 1
    report = asyncio.run(
        run(
            directory,
            None if resume else state,
            planner=planners.llm_planner,
            tool_executor=executor,
        )
    )
    assert report["status"] == "incomplete"
    assert report["stop_reason"] == "tool_budget"
    assert report["budget"]["tool_calls"] == 3
    assert report["budget"]["llm_calls"] == len(calls) == (3 if repair_finish else 2)
    assert len(report["decisions"]) == 2
    assert report["decisions"][-1]["action"] == "finish"
    latest_id = report["facts"][-1]["evidence_id"]
    assert calls[-1]["investigation_update"]["new_evidence_ids"] == [latest_id]
    assert latest_id in report["answer"]["evidence_ids"]
    assert report["interpretation"]["unreviewed_evidence_ids"] == []
    assert read_json(directory / "report.json")["answer"] == report["answer"]
    assert asyncio.run(run(directory)) == report


def test_query_rejected_at_tool_limit_before_any_new_io(state):
    asyncio.run(seed(state))
    state["limits"]["tool_calls"] = 2
    context, _ = context_and_queries(state)
    before = read_budget(state)
    with pytest.raises(ValueError, match="query_after_tool_budget"):
        validate_choice(choose(context), state)
    assert read_budget(state) == before


def test_injected_planner_cannot_query_after_tool_limit(state):
    state["limits"]["tool_calls"] = 3
    contexts = []

    async def planner(active, context):
        contexts.append(context)
        return choose(context)

    report = asyncio.run(
        run(
            Path(state["directory"]),
            state,
            planner=planner,
            tool_executor=fixture_executor(CASES[0]),
        )
    )
    assert contexts[-1]["finish_only_reason"] == "tool_budget"
    assert report["status"] == "incomplete"
    assert report["stop_reason"] == "decision_error"
    assert report["budget"]["tool_calls"] == len(report["facts"]) == 3
    assert len(report["decisions"]) == 1
    assert report["answer"] is None
    assert report["interpretation"]["unreviewed_evidence_ids"] == [
        report["facts"][-1]["evidence_id"]
    ]


def test_rule_planner_can_finish_at_tool_limit_without_model_call(state):
    state["planner_kind"] = "rules"
    state["limits"]["tool_calls"] = 3
    report = asyncio.run(
        run(
            Path(state["directory"]),
            state,
            planner=planners.rule_planner,
            tool_executor=fixture_executor(CASES[0]),
        )
    )
    assert report["stop_reason"] == "tool_budget"
    assert report["status"] == "incomplete"
    assert report["budget"]["tool_calls"] == 3
    assert report["budget"]["llm_calls"] == 0
    assert report["decisions"][-1]["action"] == "finish"
    assert report["answer"] is None


def test_time_budget_exhausted_before_final_review_keeps_latest_evidence_unreviewed(
    state,
):
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from ops_agent.agent.http.graph import build_graph

    state["limits"]["tool_calls"] = 3
    directory = Path(state["directory"])
    executor = fixture_executor(CASES[0])

    async def planner(active, context):
        reserve(active, "llm")
        return choose(context)

    async def must_not_plan(*args):
        pytest.fail("no model call is allowed once execution time is exhausted")

    async def investigate():
        for step in range(3):
            paused = await run(
                directory,
                state if step == 0 else None,
                step=True,
                planner=planner,
                tool_executor=executor,
            )
            assert paused["status"] == "paused"
        async with AsyncSqliteSaver.from_conn_string(
            str(directory / "checkpoints.sqlite")
        ) as saver:
            graph = build_graph(saver, planner=planner, tool_executor=executor)
            await graph.aupdate_state(
                {"configurable": {"thread_id": directory.name}},
                {"remaining_seconds": 0},
            )
        return await run(directory, planner=must_not_plan, tool_executor=executor)

    report = asyncio.run(investigate())
    assert report["stop_reason"] == "time_budget"
    assert report["status"] == "incomplete"
    assert report["budget"]["tool_calls"] == 3
    assert report["budget"]["llm_calls"] == 1
    assert report["answer"] is None
    assert report["interpretation"]["unreviewed_evidence_ids"] == [
        report["facts"][-1]["evidence_id"]
    ]


def test_sqlite_resume_excludes_pause_and_preserves_evidence(state, monkeypatch):
    state["planner_kind"] = "rules"
    state["limits"]["llm_seconds"] = 90
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
    assert report["limits"]["llm_seconds"] == 90
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
        ([finish_reply()], 1, None),
        (
            [
                {
                    "action": "query",
                    "endpoint_id": "invented",
                    "metric": "http_request_rate",
                    "window": "previous",
                    "rationale": "test",
                },
                finish_reply(rationale="재검토 후 종료"),
            ],
            2,
            None,
        ),
        (
            [
                {"action": "finish", "rationale": "답변을 누락한 종료"},
                finish_reply(),
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
