"""이전 v3 응답의 회귀 검사용 검사. v4 CLI는 이 계약으로 재개하지 않는다."""

from pydantic import ValidationError

from ops_agent.agent.claims import verified_claims
from ops_agent.agent.decision_errors import DecisionValidationError
from ops_agent.agent.state import Decision


def allowed_evidence_ids(state: dict) -> list[str]:
    return sorted(
        {
            item["evidence_id"]
            for item in state["evidence"]
            if item["status"] == "data_available"
        }
    )


def decision_schema(state: dict, available: list[str]) -> dict:
    schema = Decision.model_json_schema()
    schema["properties"]["action"]["enum"] = [*available, "finish"]
    schema["properties"]["rationale"].update(
        pattern=r"^[^\r\n]+[.!?。]$",
        description="행동 이유 하나만 짧은 완결 문장으로 작성. 40~80자 권장, 문장부호로 종료.",
    )
    ids = allowed_evidence_ids(state)
    if ids:
        schema["$defs"]["Hypothesis"]["properties"]["evidence_ids"]["items"]["enum"] = (
            ids
        )
    else:
        schema["properties"]["hypotheses"]["maxItems"] = 0
    claims = verified_claims(state)
    schema["required"] = [*schema["required"], "claim_ids"]
    if claims:
        schema["properties"]["claim_ids"]["minItems"] = 1
        schema["properties"]["claim_ids"]["items"]["enum"] = [
            c["claim_id"] for c in claims
        ]
    else:
        schema["properties"]["claim_ids"]["maxItems"] = 0
        schema["properties"]["hypotheses"]["maxItems"] = 0
    return schema


def validate_decision(content: str, state: dict, available: list[str]) -> dict:
    try:
        decision = Decision.model_validate_json(content)
    except ValidationError as exc:
        raise DecisionValidationError("schema_error") from exc
    if decision.action != "finish" and decision.action not in available:
        raise DecisionValidationError("invalid_action")
    cited = {
        identifier for item in decision.hypotheses for identifier in item.evidence_ids
    }
    known = {item["evidence_id"] for item in state["evidence"]}
    if cited - known:
        raise DecisionValidationError("unknown_evidence")
    if cited - set(allowed_evidence_ids(state)):
        raise DecisionValidationError("unavailable_evidence")
    claims = {c["claim_id"]: c for c in verified_claims(state)}
    if claims and not decision.claim_ids:
        raise DecisionValidationError("missing_claim_selection")
    if set(decision.claim_ids) - claims.keys():
        raise DecisionValidationError("unknown_claim")
    supported = {
        eid for cid in decision.claim_ids for eid in claims[cid]["evidence_ids"]
    }
    if decision.hypotheses and (not supported or cited - supported):
        raise DecisionValidationError("unsupported_hypothesis")
    if (
        "\n" in decision.rationale
        or "\r" in decision.rationale
        or decision.rationale[-1] not in ".!?。"
    ):
        raise DecisionValidationError("rationale_format")
    return decision.model_dump(mode="json")
