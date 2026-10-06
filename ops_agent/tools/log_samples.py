"""저장된 Loki 근거에서 제한된 로그 샘플을 읽는다."""

import re
from pathlib import Path

from ops_agent.collectors.loki_parser import decode_payload, parse_loki_response
from ops_agent.persistence.artifacts import query_artifact_path, query_key, read_json

MAX_SAMPLES = 10
MAX_LINE_CHARS = 500
SENSITIVE = re.compile(
    r"""(?ix)
    \b(?:authorization|cookie|set-cookie|password|passwd|
        token|access_token|refresh_token|api[_-]?key|secret)
    ["']?\s*[:=]
    | \bbearer\s+\S+
    | \beyJ[\w-]+\.[\w-]+\.[\w-]+\b
    """
)
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def mask_line(line: str) -> str:
    """정의된 민감정보 패턴을 가린다. 모든 개인정보 탐지를 보장하지는 않는다."""
    if SENSITIVE.search(line):
        return "[REDACTED: sensitive log line]"
    return EMAIL.sub("[EMAIL]", line)


def get_log_samples(
    state: dict,
    evidence_id: str,
    *,
    cursor: int = 0,
    limit: int = 5,
) -> dict:
    """cursor는 저장된 응답 순서의 인덱스다. 외부 조회, 모델 호출은 하지 않는다."""
    if type(cursor) is not int or cursor < 0:
        raise ValueError("cursor must be a non-negative integer")
    if type(limit) is not int or not 1 <= limit <= MAX_SAMPLES:
        raise ValueError(f"limit must be between 1 and {MAX_SAMPLES}")

    matches = [e for e in state["evidence"] if e["evidence_id"] == evidence_id]
    if len(matches) != 1:
        raise ValueError("unknown_or_duplicate_evidence")
    evidence = matches[0]
    if evidence["tool"] != "query_loki_logs":
        raise ValueError("not_log_evidence")
    if evidence["status"] not in {"data_available", "no_data"}:
        raise ValueError("unreadable_evidence")

    arguments = evidence["arguments"]
    directory = Path(state["directory"]).resolve()
    path = query_artifact_path(directory, evidence["tool"], arguments).resolve()
    if not path.is_relative_to(directory):
        raise ValueError("artifact_outside_investigation")

    record = read_json(path)
    expected = {
        "thread_id": state["thread_id"],
        "evidence_id": evidence_id,
        "tool": evidence["tool"],
        "arguments": arguments,
        "status": evidence["status"],
        "query_key": query_key(evidence["tool"], arguments),
    }

    if any(record.get(key) != value for key, value in expected.items()):
        raise ValueError("evidence_record_mismatch")
    if not record.get("completed_at"):
        raise ValueError("incomplete_evidence")

    response = record["response"]
    if not isinstance(response, dict) or response.get("isError"):
        raise ValueError("invalid_log_response")
    source_limit = arguments["limit"]
    if type(source_limit) is not int or source_limit < 1:
        raise ValueError("invalid_source_limit")
    summary = parse_loki_response(response, limit=source_limit)
    if summary["status"] != evidence["status"]:
        raise ValueError("evidence_status_mismatch")
    entries = decode_payload(response)["data"]
    if cursor > len(entries):
        raise ValueError("cursor_out_of_range")

    end = min(cursor + limit, len(entries))
    samples = []

    for index in range(cursor, end):
        entry = entries[index]

        # 잘라내기 전에 전체 줄에서 민감정보 패턴을 검사한다.
        masked = mask_line(entry["line"])
        samples.append(
            {
                "index": index,
                "timestamp_ns": entry["timestamp"],
                "line": masked[:MAX_LINE_CHARS],
                "redacted": masked != entry["line"],
                "line_truncated": len(masked) > MAX_LINE_CHARS,
            }
        )

    return {
        "evidence_id": evidence_id,
        "status": summary["status"],
        "scope": "stored_response_only",
        "cursor": cursor,
        "next_cursor": end if end < len(entries) else None,
        "has_more": end < len(entries),
        "total_stored_logs": len(entries),
        "returned_count": len(samples),
        "source_summary": summary,
        "samples": samples,
    }
