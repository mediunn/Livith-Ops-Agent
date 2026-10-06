"""필수 조회 이행과 관측 범위를 코드로 설명한다. 서비스 건강·원인은 판정하지 않는다."""

from ops_agent.agent.claims import verified_claims
from ops_agent.agent.log_samples import SAMPLE_ACTION, sample_candidates
from ops_agent.agent.state import READABLE_STATUSES
from ops_agent.tools.grafana import CATALOG, query_spec

POLICY_VERSION = "investigation-policy-v3"
RATIONALES = {
    "previous_metrics": "같은 길이의 직전 구간을 조회해 외부 API 요청률 평가값을 비교한다.",
    "logs": "요청 구간의 서비스 로그를 최대 100건 조회해 반환 범위의 레벨을 확인한다.",
    "warning_logs": "warn/error 문자열에 매칭되는 로그를 최대 100건 조회해 추가 관측을 확인한다.",
    SAMPLE_ACTION: "저장된 로그 근거에서 제한된 샘플을 읽어 다음 행동 선택에 활용한다.",
    "finish": "요청한 필수 조회를 마쳤으며, 관측값과 확인 한계를 정리해 종료한다.",
}


def coverage(state: dict) -> dict:
    required = ["current_metrics", "logs"]
    if state["request"]["compare_previous"]:
        required.insert(1, "previous_metrics")
    statuses = {}
    for item in state["evidence"]:
        action = item["action"]
        if action not in required or item["status"] not in READABLE_STATUSES:
            continue
        tool, arguments = query_spec(state, action)
        if item["tool"] == tool and item["arguments"] == arguments:
            statuses[action] = item["status"]
    return {
        "required": required,
        "completed": [name for name in required if name in statuses],
        "missing": [name for name in required if name not in statuses],
        "statuses": statuses,
    }


def warning_log_followup(state: dict) -> dict:
    """Only suppress the subset query when matching base-result completeness is explicit."""
    result = {
        "eligible": False,
        "reason": "required_checks_pending",
        "basis_evidence_ids": [],
        "queried": any(e["action"] == "warning_logs" for e in state["evidence"]),
    }
    if coverage(state)["missing"]:
        return result
    tool, arguments = query_spec(state, "logs")
    base = next(
        (
            e
            for e in reversed(state["evidence"])
            if e["action"] == "logs"
            and e["tool"] == tool
            and e["arguments"] == arguments
        ),
        None,
    )
    result.update(eligible=True, reason="base_completeness_unknown")
    if base is None:
        return result
    result["basis_evidence_ids"] = [base["evidence_id"]]
    summary = base.get("summary") or {}
    count = summary.get("log_count")
    limit = summary.get("requested_limit")
    if any(
        summary.get(key) is True
        for key in ("server_results_truncated", "limit_reached", "possibly_truncated")
    ) or (type(count) is int and count >= arguments["limit"]):
        result["reason"] = "base_result_truncated"
    elif (
        base["status"] in {"data_available", "no_data"}
        and summary.get("status") == base["status"]
        and summary.get("scope") == "returned_logs_only"
        and type(count) is int
        and 0 <= count < arguments["limit"]
        and (base["status"] == "no_data") == (count == 0)
        and type(limit) is int
        and limit == arguments["limit"]
        and all(
            summary.get(key) is False
            for key in (
                "server_results_truncated",
                "limit_reached",
                "possibly_truncated",
            )
        )
    ):
        result.update(eligible=False, reason="base_result_complete")
    return result


def allowed_actions(state: dict, available: list[str]) -> list[str]:
    actions = [
        a
        for a in available
        if (a != "previous_metrics" or state["request"]["compare_previous"])
        and (a != "warning_logs" or warning_log_followup(state)["eligible"])
        and (
            a != SAMPLE_ACTION
            or (not coverage(state)["missing"] and bool(sample_candidates(state)))
        )
    ]
    return actions if coverage(state)["missing"] else [*actions, "finish"]


def narrative(state: dict) -> dict:
    checks = coverage(state)
    claims = verified_claims(state)
    kinds = {c["kind"] for c in claims}
    limitations = [
        "서비스 전체 정상·장애 판정 기준과 SLO가 없어 건강 상태를 판정하지 않았다.",
        "지표는 외부 API 요청률이며 서비스 전체 요청량·오류율·지연을 나타내지 않는다.",
        "환경 필터와 지표의 서비스 라벨 필터를 적용하지 않았다.",
        (
            "마스킹·길이 제한된 로그 샘플을 읽었으며 전체 로그 확인이나 원인 판정은 하지 않았다."
            if state.get("log_sample_pages")
            else "로그 본문은 모델에 제공하지 않았고 원인 가설을 생성하지 않았다."
        ),
        "자연어 증상에서 필수 조사를 추출하지 않으며 명시된 요청 설정을 따른다.",
    ]
    if warning_log_followup(state)["reason"] == "base_result_complete":
        limitations.append(
            "동일 범위의 서비스 로그가 한도 미만이며 잘리지 않았다고 보고되어 "
            "경고 문자열 부분집합 조회를 추가하지 않는다. 이후 지연 수집된 로그는 포함하지 않는다."
        )
    if checks["missing"]:
        limitations.append("필수 조회 미완료: " + ", ".join(checks["missing"]))
    else:
        limitations.append(
            "필수 조회 완료는 데이터 확보나 서비스 정상 확인을 뜻하지 않는다."
        )
    for item in state["evidence"]:
        if item["status"] == "no_data":
            limitations.append(
                f"{item['action']} 조회에 반환 데이터가 없어 해당 범위를 판단할 수 없다."
            )
        elif item["status"] == "invalid_data":
            limitations.append(
                f"{item['action']} 조회에 유효한 수치가 없어 해당 값을 해석하지 않았다."
            )
        if item["tool"] == "query_loki_logs" and item["status"] in READABLE_STATUSES:
            limitations.append(
                "로그 건수·레벨은 반환된 범위만 의미하며 조회끼리 중복될 수 있다."
            )
            if (item.get("summary") or {}).get("possibly_truncated"):
                limitations.append(
                    "로그 결과가 잘렸을 수 있어 전체 로그의 존재·부재를 판단할 수 없다."
                )
    if "latest_request_rate_increased" in kinds:
        limitations.append(
            "요청률 증가는 두 구간 마지막 평가값의 비교이며 전체 추세·오류·원인 판정이 아니다."
        )
    next_checks = [
        f"미완료 조회: {CATALOG[name]}를 다시 확인한다." for name in checks["missing"]
    ]
    if not next_checks:
        next_checks = [
            "별도 확인: 서비스 HTTP 요청량·오류율·지연을 수집하고 정상 기준 또는 SLO와 비교한다."
        ]
    if "warning_log_observed" in kinds:
        next_checks.append(
            "별도 확인: 반환 로그의 원문·요청 ID를 대조해 외부 API 호출과 관련됐는지 확인한다."
        )
    if "latest_request_rate_increased" in kinds:
        next_checks.append(
            "별도 확인: API별 전체 시계열과 배포·트래픽 변경 기록을 대조한다."
        )
    if "no_data" in checks["statuses"].values():
        next_checks.append(
            "별도 확인: 데이터가 없는 조회의 라벨·수집 경로·보존 기간을 점검한다."
        )
    return {
        "assessment": "needs_investigation"
        if claims and not checks["missing"]
        else "insufficient_evidence",
        "hypotheses": [],
        "limitations": list(dict.fromkeys(limitations)),
        "next_checks": next_checks,
        "narrative_source": "code_generated",
        "health_assessment": "not_evaluated",
    }


def render_choice(state: dict, action: str, claim_ids: list[str]) -> dict:
    return {
        "action": action,
        "claim_ids": claim_ids,
        "rationale": RATIONALES[action],
        **narrative(state),
    }
