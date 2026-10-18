import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops_agent.agent.contracts import AgentAnswer
from ops_agent.agent.routing import RoutedRequest
from ops_agent.cli import ask as workflow
from ops_agent.cli.presentation import print_result
from ops_agent.persistence.artifacts import read_json, save_json


@pytest.fixture
def roots(tmp_path, monkeypatch):
    for name, folder in (
        ("REQUEST_ROOT", "requests"),
        ("HTTP_AGENT_ROOT", "http"),
        ("AGENT_ROOT", "external"),
    ):
        monkeypatch.setattr(workflow, name, tmp_path / folder)
    return tmp_path


def args(**updates):
    return SimpleNamespace(
        **{
            "question": "관측 확인",
            "domain": None,
            "route": None,
            "method": None,
            "start": None,
            "end": None,
            "timezone": "Asia/Seoul",
            "compare_previous": None,
            "model": "test-local",
            "seconds": 180,
            "output": None,
            "step": False,
            **updates,
        }
    )


def result(question="관측 확인"):
    return {
        "status": "completed",
        "stop_reason": "planner_finished",
        "symptom": question,
        "answer": AgentAnswer(
            text="반환된 요청률을 확인했습니다.",
            evidence_ids=["e1"],
            limitations=["조회 범위에 한정한 관측입니다."],
        ).model_dump(mode="json"),
        "facts": [
            {
                "evidence_id": "e1",
                "action": "http_request_rate",
                "status": "data_available",
            }
        ],
    }


@pytest.mark.parametrize("domain", ["http", "external_api"])
@pytest.mark.parametrize("output", ["answer", "json", "report"])
def test_common_entrypoint_dispatches_only_selected_domain(
    roots, monkeypatch, capsys, domain, output
):
    calls = []
    question = "/checkout 요청률 확인" if domain == "http" else "LastFM 요청률 확인"

    async def route(state, question, explicit):
        return RoutedRequest(
            action="proceed",
            domain=domain,
            purpose="lookup",
            route="/checkout" if domain == "http" else None,
            lookback_minutes=15,
            compare_previous=False,
        )

    async def http(directory, state, *, step):
        assert domain == "http"
        calls.append("http")
        assert state["request"]["route"] == "/checkout"
        start = datetime.fromisoformat(state["request"]["start_at"])
        end = datetime.fromisoformat(state["request"]["end_at"])
        assert (end - start).total_seconds() == 900
        return result(question)

    async def external(options):
        assert domain == "external_api"
        calls.append("external_api")
        assert options.request.compare_previous is False
        assert options.output == output
        target = workflow.AGENT_ROOT / options.thread_id
        save_json(target / "report.json", result(question))
        print_result(result(question), output=options.output, directory=target)
        return 0

    monkeypatch.setattr(workflow, "route_request", route)
    monkeypatch.setattr(workflow, "run_http", http)
    monkeypatch.setattr(workflow, "run_external", external)
    assert asyncio.run(workflow.ask(args(question=question, output=output))) == 0
    assert calls == [domain]
    captured = capsys.readouterr()
    if output == "json":
        assert json.loads(captured.out)["answer"] == result()["answer"]
    else:
        assert result()["answer"]["text"] in captured.out
    record = read_json(next((roots / "requests").glob("*/request.json")))
    assert record["status"] == "completed" and record["route"]["domain"] == domain
    assert record["routing_budget"]["tool_calls"] == 0
    if output == "report":
        assert (Path(record["investigation_directory"]) / "report.md").exists()


@pytest.mark.parametrize("action", ["clarify", "unsupported"])
def test_unclear_or_unsupported_request_does_not_start_investigation(
    roots, monkeypatch, capsys, action
):
    async def route(*_):
        return RoutedRequest(
            action=action,
            question="어느 분야인가요?" if action == "clarify" else None,
            unsupported_reason="p95 조회는 지원하지 않습니다."
            if action == "unsupported"
            else None,
        )

    async def forbidden(*_, **__):
        pytest.fail("must not start either investigation")

    monkeypatch.setattr(workflow, "route_request", route)
    monkeypatch.setattr(workflow, "run_http", forbidden)
    monkeypatch.setattr(workflow, "run_external", forbidden)
    assert asyncio.run(workflow.ask(args(output="json"))) == (
        0 if action == "clarify" else 1
    )
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["status"] == ("needs_input" if action == "clarify" else "unsupported")
    assert not (roots / "http").exists() and not (roots / "external").exists()


@pytest.mark.parametrize(
    "updates",
    [
        {"question": " "},
        {"start": "2026-01-01"},
        {"route": "checkout"},
        {"seconds": 0},
        {"model": "test-cloud"},
    ],
)
def test_invalid_input_is_rejected_before_creating_request(roots, updates):
    with pytest.raises(ValueError):
        asyncio.run(workflow.ask(args(**updates)))
    assert not (roots / "requests").exists()


def test_explicit_scope_is_preserved_over_model_defaults(roots, monkeypatch):
    async def route(*_):
        return RoutedRequest(
            action="proceed", domain="http", purpose="compare", lookback_minutes=15
        )

    async def http(directory, state, *, step):
        assert state["request"]["route"] == "/api//concerts"
        assert state["request"]["method"] == "GET"
        assert state["request"]["start_at"] == "2026-01-01T13:00:00Z"
        assert state["request"]["end_at"] == "2026-01-01T14:00:00Z"
        return result()

    monkeypatch.setattr(workflow, "route_request", route)
    monkeypatch.setattr(workflow, "run_http", http)
    assert (
        asyncio.run(
            workflow.ask(
                args(
                    route="/api//concerts",
                    method="GET",
                    start="2026-01-01T22:00:00",
                    end="2026-01-01T23:00:00",
                )
            )
        )
        == 0
    )


def test_router_failure_is_saved_and_never_falls_back(roots, monkeypatch):
    async def route(*_):
        raise TimeoutError()

    async def forbidden(*_, **__):
        pytest.fail("must not fall back to another domain")

    monkeypatch.setattr(workflow, "route_request", route)
    monkeypatch.setattr(workflow, "run_http", forbidden)
    monkeypatch.setattr(workflow, "run_external", forbidden)
    with pytest.raises(TimeoutError):
        asyncio.run(workflow.ask(args()))
    record = read_json(next((roots / "requests").glob("*/request.json")))
    assert record["status"] == "interrupted_or_failed"
    assert record["investigation_id"] is None


@pytest.mark.parametrize("domain", ["http", "external_api"])
def test_routed_request_runs_real_graph_and_exports_saved_answer_without_requery(
    roots, monkeypatch, capsys, domain
):
    from ollama import ChatResponse

    from ops_agent.agent import nodes
    from ops_agent.agent.decision import planner as external_planner
    from ops_agent.agent.http import planner as http_planner
    from ops_agent.agent.http import session as http_session
    from ops_agent.evaluation.agent import CASES_PATH, synthetic_collector
    from ops_agent.evaluation.http_agent import CASES, fixture_executor
    from ops_agent.persistence import session as external_session

    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def chat(self, **kwargs):
            context = json.loads(kwargs["messages"][1]["content"])
            calls.append(context)
            if domain == "http":
                payload = {
                    "action": "finish",
                    "endpoint_id": None,
                    "metric": None,
                    "window": None,
                    "rationale": "요청 구간의 관측을 확인했다.",
                    "hypotheses": [],
                    "answer": AgentAnswer(
                        text="요청 구간의 HTTP 요청률과 평균 지연을 확인했습니다.",
                        evidence_ids=[
                            e["evidence_id"] for e in context["observations"]
                        ],
                        limitations=["원인은 확인하지 않았습니다."],
                    ).model_dump(mode="json"),
                }
            else:
                missing = context["required_checks"]["missing"]
                payload = {
                    "action": missing[0] if missing else "finish",
                    "claim_ids": [],
                    "answer": None,
                }
                if not missing:
                    payload["answer"] = AgentAnswer(
                        text="반환된 외부 API 요청률과 서비스 로그를 확인했습니다.",
                        evidence_ids=[e["evidence_id"] for e in context["evidence"]],
                        limitations=["반환 범위 밖의 로그는 확인하지 않았습니다."],
                    ).model_dump(mode="json")
            return ChatResponse(
                model="test-local",
                done=True,
                done_reason="stop",
                message={"role": "assistant", "content": json.dumps(payload)},
                prompt_eval_count=100,
                eval_count=30,
            )

    async def route(*_):
        return RoutedRequest(
            action="proceed",
            domain=domain,
            purpose="lookup",
            route="/checkout" if domain == "http" else None,
        )

    async def http(directory, state, *, step):
        return await http_session.run(
            directory, state, step=step, tool_executor=fixture_executor(CASES[0])
        )

    case = read_json(CASES_PATH)["cases"][0] | {
        "current_rate": 1,
        "previous_rate": 1,
        "log_levels": {"info": 2},
        "truncated": False,
    }
    monkeypatch.setattr(workflow, "route_request", route)
    monkeypatch.setattr(workflow, "run_http", http)
    monkeypatch.setattr(external_session, "AGENT_ROOT", roots / "external")
    monkeypatch.setattr(
        external_session, "CHECKPOINT_DB", roots / "external" / "checkpoints.sqlite"
    )
    monkeypatch.setattr(nodes, "execute_tool", synthetic_collector(case))
    monkeypatch.setattr(external_planner, "AsyncClient", Client)
    monkeypatch.setattr(http_planner, "AsyncClient", Client)
    question = "/checkout 요청률 확인" if domain == "http" else "외부 API 요청률 확인"
    assert asyncio.run(workflow.ask(args(question=question, output="json"))) == 0
    original = json.loads(capsys.readouterr().out)
    assert original["answer"]["evidence_ids"]
    assert original["status"] == "completed"
    record = read_json(next((roots / "requests").glob("*/request.json")))
    directory = Path(record["investigation_directory"])
    assert read_json(directory / "report.json")["answer"] == original["answer"]
    before = len(calls)
    if domain == "http":
        resumed = asyncio.run(http_session.run(directory))
        print_result(resumed, output="report", directory=directory)
    else:
        assert (
            asyncio.run(
                external_session.run(
                    SimpleNamespace(
                        resume=record["investigation_id"],
                        status=None,
                        step=False,
                        output="report",
                    )
                )
            )
            == 0
        )
    assert len(calls) == before
    assert original["answer"]["text"] in (directory / "report.md").read_text()
