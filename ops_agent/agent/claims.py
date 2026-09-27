"""파싱한 관측에서 좁은 범위의 사실만 계산한다. 원인은 검증하지 않는다."""

import hashlib
import json
import math
from datetime import datetime

from ops_agent.tools.grafana import measurement_for, query_spec

CLAIM_POLICY_VERSION = "observation-claims-v1"


def _count(value) -> bool:
    return type(value) is int and value >= 0


def _number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _claim(kind: str, evidence_ids: list[str], statement: str, **values) -> dict:
    claim = dict(kind=kind, evidence_ids=evidence_ids, statement=statement, **values)
    digest = hashlib.sha256(
        json.dumps(claim, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:24]
    return {"claim_id": digest, **claim}


def _usable(state: dict, item: dict) -> bool:
    """현재 요청의 고정 쿼리·단위와 일치하는 성공 관측만 사용한다."""
    if item.get("status") != "data_available":
        return False
    try:
        tool, arguments = query_spec(state, item["action"])
    except (KeyError, TypeError, ValueError):
        return False
    return (
        item.get("tool") == tool
        and item.get("arguments") == arguments
        and all(
            item.get("measurement", {}).get(key) == value
            for key, value in measurement_for(tool).items()
        )
        and isinstance(item.get("summary"), dict)
        and item["summary"].get("status") == "data_available"
    )


def _warning_claim(item: dict) -> dict | None:
    summary = item["summary"]
    count = summary.get("log_count")
    levels = summary.get("level_counts")
    if (
        summary.get("scope") != "returned_logs_only"
        or not _count(count)
        or count == 0
        or not isinstance(levels, dict)
        or not all(isinstance(k, str) and _count(v) for k, v in levels.items())
        or sum(levels.values()) != count
    ):
        return None
    warnings = sum(
        v for k, v in levels.items() if k.lower() in {"warn", "warning", "error"}
    )
    if warnings == 0:
        return None
    return _claim(
        "warning_log_observed",
        [item["evidence_id"]],
        f"이 조회가 반환한 로그 요약에 warn/warning/error 레벨 {warnings}건이 기록됐다.",
        warning_level_count=warnings,
        returned_log_count=count,
        scope="returned_logs_only",
    )


def _series(item: dict) -> dict:
    """중복 라벨·잘못된 값·오래된 마지막 샘플은 비교에서 제외한다."""
    end = datetime.fromisoformat(item["arguments"]["endTime"]).timestamp()
    result = {}
    seen = set()
    series = item["summary"].get("series")
    if not isinstance(series, list):
        return result
    for row in series:
        if not isinstance(row, dict):
            continue
        labels = row.get("labels")
        if (
            not isinstance(labels, dict)
            or not labels.get("api")
            or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in labels.items()
            )
        ):
            continue
        key = tuple(sorted(labels.items()))
        if key in seen:
            result.pop(key, None)
            continue
        seen.add(key)
        valid = row.get("valid_sample_count")
        if (
            _count(valid)
            and valid > 0
            and type(row.get("sample_count")) is int
            and row["sample_count"] == valid
            and type(row.get("invalid_sample_count")) is int
            and row["invalid_sample_count"] == 0
            and _number(row.get("latest_value"))
            and _number(row.get("last_timestamp"))
            and row["last_timestamp"] == end
        ):
            result[key] = row
    return result


def verified_claims(state: dict) -> list[dict]:
    evidence = [item for item in state["evidence"] if _usable(state, item)]
    claims = [
        claim
        for item in evidence
        if item["tool"] == "query_loki_logs"
        if (claim := _warning_claim(item)) is not None
    ]
    current = [item for item in evidence if item["action"] == "current_metrics"]
    previous = [item for item in evidence if item["action"] == "previous_metrics"]
    # 같은 action의 관측이 여럿이면 비교 대상을 확정할 수 없다.
    if len(current) == len(previous) == 1:
        now, before = current[0], previous[0]
        latest, prior = _series(now), _series(before)
        for labels in sorted(latest.keys() & prior.keys()):
            value, old = latest[labels]["latest_value"], prior[labels]["latest_value"]
            if value > old:
                claims.append(
                    _claim(
                        "latest_request_rate_increased",
                        [before["evidence_id"], now["evidence_id"]],
                        f"API {dict(labels)['api']}의 구간 마지막 요청률 평가값: "
                        f"직전 {old} req/s → 현재 {value} req/s.",
                        labels=dict(labels),
                        previous_value=old,
                        current_value=value,
                        previous_timestamp=prior[labels]["last_timestamp"],
                        current_timestamp=latest[labels]["last_timestamp"],
                        scope="query_window_endpoints_only",
                    )
                )
    return sorted(
        {c["claim_id"]: c for c in claims}.values(), key=lambda c: c["claim_id"]
    )
