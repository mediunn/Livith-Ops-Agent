"""조사 버전에 따라 모델 선택과 사용자 답변의 참조를 검증한다."""

from copy import deepcopy

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ops_agent.agent.claims import verified_claims
from ops_agent.agent.contracts import AgentAnswer, validate_answer_references
from ops_agent.agent.decision import legacy_validation
from ops_agent.agent.decision.errors import DecisionValidationError
from ops_agent.agent.log_samples import (
    MAX_PAGE_SIZE,
    SAMPLE_ACTION,
    page_reference,
    sample_candidates,
    valid_sample_request,
)
from ops_agent.agent.policy import allowed_actions, coverage, render_choice
from ops_agent.agent.state import READABLE_STATUSES, Text


class ModelChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Text
    claim_ids: list[Text] = Field(max_length=3)


class LogSampleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    evidence_id: Text
    cursor: int = Field(ge=0)
    limit: int = Field(ge=1, le=MAX_PAGE_SIZE)


class SampleModelChoice(ModelChoice):
    log_sample_request: LogSampleRequest | None = None


class AnswerModelChoice(SampleModelChoice):
    answer: AgentAnswer | None = None


def choice_model(state: dict):
    if state.get("version", 3) >= 6:
        return AnswerModelChoice
    return SampleModelChoice if state.get("version", 3) >= 5 else ModelChoice


def answer_evidence_ids(state: dict) -> set[str]:
    return {
        e["evidence_id"] for e in state["evidence"] if e["status"] in READABLE_STATUSES
    }


def validate_model_answer(action: str, answer, state: dict) -> dict | None:
    if action != "finish":
        if answer is not None:
            raise DecisionValidationError("answer_before_finish")
        return None
    if answer is None:
        raise DecisionValidationError("missing_answer")
    try:
        parsed = AgentAnswer.model_validate(answer)
    except ValidationError as exc:
        raise DecisionValidationError("schema_error") from exc
    ids = answer_evidence_ids(state)
    if ids and not parsed.evidence_ids:
        raise DecisionValidationError("missing_answer_evidence")
    try:
        validate_answer_references(parsed, ids)
    except ValueError as exc:
        raise DecisionValidationError("unknown_answer_evidence") from exc
    return parsed.model_dump(mode="json")


def decision_schema(state: dict, available: list[str]) -> dict:
    if state.get("version", 3) < 4:
        return legacy_validation.decision_schema(state, available)
    model = choice_model(state)
    schema = model.model_json_schema()
    actions = allowed_actions(state, available)
    schema["properties"]["action"]["enum"] = actions
    ids = [c["claim_id"] for c in verified_claims(state)]
    if ids:
        schema["properties"]["claim_ids"].update(
            minItems=1, items={"type": "string", "enum": ids}
        )
    else:
        schema["properties"]["claim_ids"]["maxItems"] = 0
    if model is AnswerModelChoice:
        schema["required"].append("answer")
        refs = schema["$defs"]["AgentAnswer"]["properties"]["evidence_ids"]
        evidence_ids = sorted(answer_evidence_ids(state))
        if evidence_ids:
            refs.update(minItems=1, items={"type": "string", "enum": evidence_ids})
        else:
            refs["maxItems"] = 0
    if issubclass(model, SampleModelChoice):
        if SAMPLE_ACTION in actions:
            schema = sample_choice_schema(schema, sample_candidates(state))
        else:
            schema["properties"]["log_sample_request"] = {"type": "null"}
    if model is AnswerModelChoice:
        return answer_choice_schema(schema)
    return schema


def answer_choice_schema(schema: dict) -> dict:
    """행동별로 종료 답변 객체 또는 null만 생성하도록 제한한다."""
    definitions = schema.pop("$defs")
    branches = schema.pop("anyOf", None) or [schema]
    choices = []
    for branch in branches:
        actions = branch["properties"]["action"]["enum"]
        if "finish" in actions:
            finish = deepcopy(branch)
            finish["properties"]["action"]["enum"] = ["finish"]
            finish["properties"]["answer"] = {"$ref": "#/$defs/AgentAnswer"}
            choices.append(finish)
        remaining = [a for a in actions if a != "finish"]
        if remaining:
            query = deepcopy(branch)
            query["properties"]["action"]["enum"] = remaining
            query["properties"]["answer"] = {"type": "null"}
            choices.append(query)
    return {"$defs": definitions, "anyOf": choices}


def sample_choice_schema(schema: dict, candidates: list[dict]) -> dict:
    """행동과 인자를 함께 제한해 null 인자와 다른 근거의 cursor 조합을 막는다."""
    definitions = schema.pop("$defs")
    request_schema = definitions["LogSampleRequest"]
    requests = []
    for candidate in candidates:
        request = deepcopy(request_schema)
        request["properties"]["evidence_id"]["enum"] = [candidate["evidence_id"]]
        request["properties"]["cursor"]["enum"] = [candidate["cursor"]]
        requests.append(request)
    definitions["LogSampleRequest"] = {"anyOf": requests}
    sample = deepcopy(schema)
    sample["properties"]["action"]["enum"] = [SAMPLE_ACTION]
    sample["properties"]["log_sample_request"] = {"$ref": "#/$defs/LogSampleRequest"}
    sample["required"].append("log_sample_request")
    schema["properties"]["action"]["enum"].remove(SAMPLE_ACTION)
    schema["properties"]["log_sample_request"] = {"type": "null"}
    return {"$defs": definitions, "anyOf": [sample, schema]}


def validate_decision(content: str, state: dict, available: list[str]) -> dict:
    if state.get("version", 3) < 4:
        return legacy_validation.validate_decision(content, state, available)
    try:
        model = choice_model(state)
        choice = model.model_validate_json(content)
    except ValidationError as exc:
        raise DecisionValidationError("schema_error") from exc
    if choice.action == "finish" and coverage(state)["missing"]:
        raise DecisionValidationError("required_checks_missing")
    if choice.action not in allowed_actions(state, available):
        raise DecisionValidationError("invalid_action")
    ids = {c["claim_id"] for c in verified_claims(state)}
    if ids and not choice.claim_ids:
        raise DecisionValidationError("missing_claim_selection")
    if set(choice.claim_ids) - ids:
        raise DecisionValidationError("unknown_claim")
    result = render_choice(state, choice.action, list(dict.fromkeys(choice.claim_ids)))
    if isinstance(choice, AnswerModelChoice):
        result["answer"] = validate_model_answer(choice.action, choice.answer, state)
        result["evidence_considered"] = [e["evidence_id"] for e in state["evidence"]]
    if isinstance(choice, SampleModelChoice):
        request = (
            choice.log_sample_request.model_dump()
            if choice.log_sample_request is not None
            else None
        )
        if choice.action == SAMPLE_ACTION:
            if not valid_sample_request(state, request):
                raise DecisionValidationError("invalid_log_sample_request")
            result["log_sample_request"] = request
        elif request is not None:
            raise DecisionValidationError("unexpected_log_sample_request")
        if state.get("log_sample_pages"):
            result["log_sample_page_seen"] = page_reference(
                state["log_sample_pages"][-1]
            )
    return result
