"""현재 조사에서는 모델 선택만 검증하고, 설명은 관측 정책으로 구성한다."""

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ops_agent.agent.claims import verified_claims
from ops_agent.agent.decision import legacy_validation
from ops_agent.agent.decision.errors import DecisionValidationError
from ops_agent.agent.policy import allowed_actions, coverage, render_choice
from ops_agent.agent.state import Text


class ModelChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Text
    claim_ids: list[Text] = Field(max_length=3)


def decision_schema(state: dict, available: list[str]) -> dict:
    if state.get("version", 3) < 4:
        return legacy_validation.decision_schema(state, available)
    schema = ModelChoice.model_json_schema()
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
        choice = ModelChoice.model_validate_json(content)
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
    return render_choice(state, choice.action, list(dict.fromkeys(choice.claim_ids)))
