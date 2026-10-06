"""현재 조사에서는 모델 선택만 검증하고, 설명은 관측 정책으로 구성한다."""

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ops_agent.agent.claims import verified_claims
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
from ops_agent.agent.state import Text


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


def decision_schema(state: dict, available: list[str]) -> dict:
    if state.get("version", 3) < 4:
        return legacy_validation.decision_schema(state, available)
    model = SampleModelChoice if state.get("version", 3) >= 5 else ModelChoice
    schema = model.model_json_schema()
    if model is SampleModelChoice:
        candidates = sample_candidates(state)
        if SAMPLE_ACTION in allowed_actions(state, available) and candidates:
            schema["$defs"]["LogSampleRequest"]["properties"]["evidence_id"]["enum"] = [
                c["evidence_id"] for c in candidates
            ]
        else:
            schema["properties"]["log_sample_request"] = {"type": "null"}
    schema["properties"]["action"]["enum"] = allowed_actions(state, available)
    ids = [c["claim_id"] for c in verified_claims(state)]
    if ids:
        schema["properties"]["claim_ids"].update(
            minItems=1, items={"type": "string", "enum": ids}
        )
    else:
        schema["properties"]["claim_ids"]["maxItems"] = 0
    return schema


def validate_decision(content: str, state: dict, available: list[str]) -> dict:
    if state.get("version", 3) < 4:
        return legacy_validation.validate_decision(content, state, available)
    try:
        model = SampleModelChoice if state.get("version", 3) >= 5 else ModelChoice
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
