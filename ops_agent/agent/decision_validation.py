"""모델 출력의 형식·도구·관측 선택·근거 참조를 검사한다. 원인 검증은 별도다."""

from pydantic import ValidationError

from ops_agent.agent.claims import verified_claims
from ops_agent.agent.state import Decision

ERROR_MESSAGES = {
    "schema_error": "응답이 Decision JSON 형식과 필드 제약을 충족하지 않습니다.",
    "invalid_action": "허용되지 않거나 이미 사용한 도구를 선택했습니다.",
    "unknown_evidence": "제공되지 않은 근거 ID를 가설에 인용했습니다.",
    "unavailable_evidence": "data_available이 아닌 근거를 가설에 인용했습니다.",
    "missing_claim_selection": "검증 관측이 있으므로 claim_ids에 관련 관측 ID를 하나 이상 선택하세요. 관측 문장을 원인 가설로 옮기지 마세요.",
    "unknown_claim": "제공된 검증 관측 목록에 없는 claim_id를 선택했습니다.",
    "unsupported_hypothesis": "선택한 검증 관측이 없거나 가설이 그 관측 밖의 근거를 인용했습니다. 가설을 삭제하세요.",
    "rationale_format": "rationale는 줄바꿈 없이 마침표로 끝나는 짧은 완결 문장으로 다시 작성하세요.",
    "output_truncated": "모델 출력이 완료되기 전에 잘렸습니다.",
}


class DecisionValidationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(ERROR_MESSAGES[code])

    def details(self) -> dict:
        # 예외 원문의 입력값·경로를 추적 metadata로 복사하지 않는다.
        return {"valid": False, "code": self.code, "message": ERROR_MESSAGES[self.code]}


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
