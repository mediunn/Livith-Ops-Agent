"""버전 간 공통 판단 오류 코드. 입력값을 진단 문자열에 복사하지 않는다."""

ERROR_MESSAGES = {
    "invalid_log_sample_request": "log_sample_candidates에 있는 근거 ID와 다음 cursor, 1~5의 limit을 선택하세요.",
    "unexpected_log_sample_request": "get_log_samples 이외의 행동에는 log_sample_request를 생략하거나 null로 지정하세요.",
    "required_checks_missing": "필수 조회가 남아 있어 종료할 수 없습니다. missing_required_checks를 확인하세요.",
    "no_available_action": "필수 조회를 완료할 수 있는 행동이 없습니다.",
    "schema_error": "응답이 제공된 JSON 형식과 필드 제약을 충족하지 않습니다.",
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
