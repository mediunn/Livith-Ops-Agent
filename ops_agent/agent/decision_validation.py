"""모델 출력의 형식·도구·근거 참조를 검사한다. 가설의 진실성은 별도다."""

from pydantic import ValidationError

from ops_agent.agent.state import Decision

ERROR_MESSAGES = {
    "schema_error": "응답이 Decision JSON 형식과 필드 제약을 충족하지 않습니다.",
    "invalid_action": "허용되지 않거나 이미 사용한 도구를 선택했습니다.",
    "unknown_evidence": "제공되지 않은 근거 ID를 가설에 인용했습니다.",
    "unavailable_evidence": "data_available이 아닌 근거를 가설에 인용했습니다.",
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
    ids = allowed_evidence_ids(state)
    if ids:
        schema["$defs"]["Hypothesis"]["properties"]["evidence_ids"]["items"]["enum"] = (
            ids
        )
    else:
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
    return decision.model_dump(mode="json")
