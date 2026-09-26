import json
from collections import Counter


def decode_payload(response: dict):
    """MCP JSON 응답을 읽는다. 조회 오류는 호출자가 먼저 처리한다."""
    payload = response.get("structuredContent")
    if payload is not None:
        return payload
    blocks = response.get("content")
    if not isinstance(blocks, list) or not all(isinstance(x, dict) for x in blocks):
        raise TypeError("MCP content가 블록 배열이 아닙니다.")
    texts = [x.get("text") for x in blocks if x.get("type") == "text"]
    if len(texts) != 1 or not isinstance(texts[0], str):
        raise ValueError("JSON 텍스트 응답이 정확히 하나여야 합니다.")
    return json.loads(texts[0])


def parse_loki_response(response: dict, *, limit: int) -> dict:
    """query_loki_logs의 full 로그 응답을 요약한다. 로그 본문은 원본에 보존."""
    payload = decode_payload(response)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise TypeError("Loki 응답의 data가 배열이 아닙니다.")
    entries = payload["data"]
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, dict):
        raise TypeError("Loki metadata가 객체가 아닙니다.")
    returned = metadata.get("linesReturned")
    if returned is not None and (type(returned) is not int or returned != len(entries)):
        raise ValueError("Loki linesReturned와 실제 로그 수가 다릅니다.")
    truncated = metadata.get("resultsTruncated")
    if truncated is not None and not isinstance(truncated, bool):
        raise TypeError("resultsTruncated는 boolean이어야 합니다.")

    timestamps = []
    streams = set()
    levels = Counter()
    for entry in entries:
        if not isinstance(entry, dict):
            raise TypeError("Loki 로그 항목이 객체가 아닙니다.")
        timestamp = entry.get("timestamp")
        if (
            not isinstance(timestamp, str)
            or not timestamp.isascii()
            or not timestamp.isdigit()
        ):
            raise ValueError("Loki timestamp는 나노초 정수 문자열이어야 합니다.")
        if not isinstance(entry.get("line"), str):
            raise TypeError("Loki line이 문자열이 아닙니다.")
        labels = entry.get("labels")
        if not isinstance(labels, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in labels.items()
        ):
            raise TypeError("Loki labels가 문자열 매핑이 아닙니다.")
        structured = entry.get("structuredMetadata", {})
        if not isinstance(structured, dict):
            raise TypeError("structuredMetadata가 객체가 아닙니다.")
        level = structured.get("detected_level", labels.get("level", "unknown"))
        if not isinstance(level, str):
            level = "unknown"
        levels[level] += 1
        timestamps.append(int(timestamp))
        streams.add(tuple(sorted(labels.items())))

    return {
        "status": "data_available" if entries else "no_data",
        "log_count": len(entries),
        "stream_count": len(streams),
        "level_counts": dict(levels),
        "first_timestamp_ns": str(min(timestamps)) if timestamps else None,
        "last_timestamp_ns": str(max(timestamps)) if timestamps else None,
        "requested_limit": limit,
        "server_results_truncated": truncated,
        "limit_reached": len(entries) >= limit,
        "possibly_truncated": truncated is True or len(entries) >= limit,
        "scope": "returned_logs_only",
    }
