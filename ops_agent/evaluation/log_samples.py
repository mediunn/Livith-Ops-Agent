"""로그 샘플 선택·판단 반영을 검사한다. 본문 해석의 정확성은 별도 검토한다."""

from ops_agent.agent.log_samples import MAX_SAMPLE_PAGES, page_reference


def evaluate_log_samples(state: dict, expected: dict) -> dict[str, bool]:
    pages = state.get("log_sample_pages", [])
    sources = {e["evidence_id"]: e["action"] for e in state["evidence"]}
    reviewed = [d.get("log_sample_page_seen") for d in state["decisions"]]
    seen_lines = [
        (page["evidence_id"], sample["index"])
        for page in pages
        for sample in page["samples"]
    ]
    reviewed_lines = {
        (sources.get(page["evidence_id"]), sample["index"])
        for page in pages
        if page_reference(page) in reviewed
        for sample in page["samples"]
    }
    required_lines = {
        (action, index)
        for action, indices in expected["required_indices"].items()
        for index in indices
    }
    checks = {
        "sample_page_count": expected["min_pages"]
        <= len(pages)
        <= expected["max_pages"],
        "sample_page_budget": len(pages) <= MAX_SAMPLE_PAGES,
        "sample_sources": all(
            sources.get(p["evidence_id"]) in expected["source_actions"] for p in pages
        ),
        "no_repeated_sample_lines": len(seen_lines) == len(set(seen_lines)),
        "required_sample_lines_reviewed": required_lines <= reviewed_lines,
        "all_sample_pages_reviewed": all(page_reference(p) in reviewed for p in pages),
    }
    if expected.get("all_returned_lines_redacted"):
        checks["sample_lines_redacted"] = bool(seen_lines) and all(
            sample["redacted"] is True
            and sample["line"] == "[REDACTED: sensitive log line]"
            for page in pages
            for sample in page["samples"]
        )
    return checks
