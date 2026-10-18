import asyncio
import json
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from ops_agent.agent.contracts import AgentAnswer
from ops_agent.agent.decision.planner import choose_action
from ops_agent.agent.decision.validation import (
    DecisionValidationError,
    decision_schema,
    validate_decision,
)
from ops_agent.agent.http.request import HTTPRequestIntent
from ops_agent.agent.http.state import HTTPAnswer
from ops_agent.agent.log_samples import SAMPLE_ACTION
from ops_agent.agent.nodes import AgentNodes
from ops_agent.evaluation.agent import CASES_PATH, synthetic_collector
from ops_agent.persistence.artifacts import read_json
from ops_agent.reporting.agent_report import build_report
from tests.helpers.agent import fake_model


@pytest.fixture
def answer_state(current_state):
    state = current_state
    state["version"] = 6
    case = deepcopy(read_json(CASES_PATH)["cases"][0])
    case.update(
        current_rate=1, previous_rate=1, log_levels={"info": 2}, truncated=False
    )
    collect = synthetic_collector(case)
    for action in ("current_metrics", "previous_metrics", "logs"):
        state["action"] = action
        state["evidence"].append(asyncio.run(collect(state)))
    return state


def reply(state):
    return {
        "action": "finish",
        "claim_ids": [],
        "answer": AgentAnswer(
            text="조회한 외부 API의 두 구간 마지막 요청률은 같습니다.",
            evidence_ids=[e["evidence_id"] for e in state["evidence"]],
            limitations=["오류율·지연과 원인은 확인하지 않았습니다."],
        ).model_dump(mode="json"),
    }


def test_http_and_external_api_share_answer_contract():
    assert HTTPAnswer is AgentAnswer
    intent = HTTPRequestIntent(
        action="proceed", purpose="lookup", metrics=("http_request_rate",)
    )
    assert intent.output_format == "answer"
    with pytest.raises(ValueError):
        HTTPRequestIntent(action="proceed", purpose="lookup", metrics=("http_p95",))
    with pytest.raises(ValueError):
        HTTPRequestIntent(action="proceed", purpose="lookup")


@pytest.mark.parametrize("sample_available", [False, True])
def test_external_v6_schema_handles_answers_and_samples(answer_state, sample_available):
    state = answer_state
    schema = decision_schema(state, [SAMPLE_ACTION] if sample_available else [])
    validator = Draft202012Validator(schema)
    validator.check_schema(schema)
    validator.validate(reply(state))
    missing = reply(state)
    del missing["answer"]
    assert not validator.is_valid(missing)
    if sample_available:
        sample = {
            "action": SAMPLE_ACTION,
            "claim_ids": [],
            "answer": None,
            "log_sample_request": {
                "evidence_id": state["evidence"][-1]["evidence_id"],
                "cursor": 0,
                "limit": 2,
            },
        }
        validator.validate(sample)
        sample["answer"] = reply(state)["answer"]
        assert not validator.is_valid(sample)


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("missing", "missing_answer"),
        ("unknown", "unknown_answer_evidence"),
        ("empty_refs", "missing_answer_evidence"),
        ("sample_answer", "answer_before_finish"),
    ],
)
def test_external_answer_contract_rejects_invalid_choices(answer_state, mutation, code):
    value = reply(answer_state)
    available = []
    if mutation == "missing":
        del value["answer"]
    elif mutation == "unknown":
        value["answer"]["evidence_ids"] = ["invented"]
    elif mutation == "empty_refs":
        value["answer"]["evidence_ids"] = []
    else:
        available = [SAMPLE_ACTION]
        value.update(
            action=SAMPLE_ACTION,
            log_sample_request={
                "evidence_id": answer_state["evidence"][-1]["evidence_id"],
                "cursor": 0,
                "limit": 1,
            },
        )
    with pytest.raises(DecisionValidationError) as error:
        validate_decision(json.dumps(value), answer_state, available)
    assert error.value.code == code


def test_external_finish_keeps_model_answer_and_does_not_reuse_it_on_failure(
    answer_state, monkeypatch
):
    state = answer_state
    calls = fake_model(monkeypatch, state, [reply(state)])
    update = asyncio.run(AgentNodes().decide(state))
    state.update(update)
    report = build_report(state)
    assert len(calls) == 1
    assert report["report_version"] == "agent-report-v6"
    assert report["answer"] == reply(state)["answer"]
    assert report["answer_source"] == "model"
    assert report["unreviewed_evidence_ids"] == []
    state["stop_reason"] = "llm_budget"
    assert build_report(state)["answer"] is None


def test_external_answer_missing_is_repaired_with_feedback(answer_state, monkeypatch):
    missing = reply(answer_state)
    del missing["answer"]
    calls = fake_model(monkeypatch, answer_state, [missing, reply(answer_state)])
    result = asyncio.run(choose_action(answer_state, []))
    assert result["answer"] == reply(answer_state)["answer"]
    feedback = json.loads(calls[1]["messages"][3]["content"])
    assert feedback["validation_feedback"]["code"] == "missing_answer"
