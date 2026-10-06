"""저장된 로그 페이지의 선택 범위와 읽기 이력을 관리한다."""

from ops_agent.tools.grafana import query_spec

SAMPLE_ACTION = "get_log_samples"
SAMPLE_DESCRIPTION = (
    "저장된 로그 근거의 다음 페이지를 마스킹해 읽기. 새 Grafana 조회 없음"
)
MAX_SAMPLE_PAGES = 2
MAX_PAGE_SIZE = 5


def sample_candidates(state: dict) -> list[dict]:
    pages = state.get("log_sample_pages", [])
    if state.get("version", 3) < 5 or len(pages) >= MAX_SAMPLE_PAGES:
        return []
    candidates = []
    for evidence in state["evidence"]:
        if evidence["action"] not in {"logs", "warning_logs"}:
            continue
        tool, arguments = query_spec(state, evidence["action"])
        count = (evidence.get("summary") or {}).get("log_count")
        if (
            evidence["tool"] != tool
            or evidence["arguments"] != arguments
            or evidence["status"] != "data_available"
            or type(count) is not int
            or count <= 0
        ):
            continue
        previous = [p for p in pages if p["evidence_id"] == evidence["evidence_id"]]
        cursor = previous[-1]["next_cursor"] if previous else 0
        if cursor is not None and cursor < count:
            candidates.append(
                {
                    "evidence_id": evidence["evidence_id"],
                    "cursor": cursor,
                    "max_limit": MAX_PAGE_SIZE,
                }
            )
    return candidates


def valid_sample_request(state: dict, request: dict | None) -> bool:
    if not isinstance(request, dict) or set(request) != {
        "evidence_id",
        "cursor",
        "limit",
    }:
        return False
    return (
        type(request["cursor"]) is int
        and type(request["limit"]) is int
        and 1 <= request["limit"] <= MAX_PAGE_SIZE
        and any(
            request["evidence_id"] == c["evidence_id"]
            and request["cursor"] == c["cursor"]
            for c in sample_candidates(state)
        )
    )


def page_reference(page: dict) -> dict:
    return {key: page[key] for key in ("evidence_id", "cursor")}


def sample_history(state: dict) -> list[dict]:
    """보고서와 추적에는 본문 대신 범위와 모델 판단 반영 여부를 남긴다."""
    reviewed = [d.get("log_sample_page_seen") for d in state["decisions"]]
    return [
        {
            **{key: value for key, value in page.items() if key != "samples"},
            "reviewed": page_reference(page) in reviewed,
        }
        for page in state.get("log_sample_pages", [])
    ]
