import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from mcp import Client

from check_grafana_mcp import PROJECT_DIR, create_grafana_server
from loki_parser import decode_payload, parse_loki_response

DATASOURCE_UID = "grafanacloud-logs"
DEFAULT_LOGQL = '{job="livith-server"}'


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_reference(path: Path | None) -> tuple[Path, dict, dict]:
    if path is None:
        candidates = list((PROJECT_DIR / "artifacts").glob("prometheus-*.json"))
        if not candidates:
            raise ValueError(
                "먼저 query_prometheus.py를 실행하거나 --prometheus-evidence를 지정하세요."
            )
        path = max(candidates, key=lambda item: item.stat().st_mtime)
    path = path.resolve()
    reference = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(reference, dict) or reference.get("tool") != "query_prometheus":
        raise ValueError("Prometheus evidence 파일이 아닙니다.")
    args = reference.get("arguments", {})
    if not isinstance(args, dict):
        raise TypeError("Prometheus 조회 조건이 객체가 아닙니다.")
    if args.get("queryType") != "range":
        raise ValueError("range 조회 evidence가 필요합니다.")
    start, end = args.get("startTime"), args.get("endTime")
    if not isinstance(start, str) or not isinstance(end, str):
        raise TypeError("시작·종료 시각이 없습니다.")
    parsed = [datetime.fromisoformat(value) for value in (start, end)]
    if any(value.utcoffset() is None for value in parsed) or parsed[0] >= parsed[1]:
        raise ValueError("시간대가 포함된 유효한 조회 범위가 필요합니다.")
    return (
        path,
        reference,
        {
            "datasourceUid": DATASOURCE_UID,
            "startRfc3339": start,
            "endRfc3339": end,
        },
    )


async def collect_evidence(
    reference_path: Path | None,
    *,
    logql: str,
    limit: int,
    discover: bool,
    output_dir: Path | None = None,
) -> tuple[Path, dict]:
    path, reference, window = read_reference(reference_path)
    arguments = (
        window
        if discover
        else {
            **window,
            "logql": logql,
            "queryType": "range",
            "direction": "backward",
            "format": "full",
            "limit": limit,
        }
    )
    record = {
        "evidence_id": str(uuid4()),
        "source": "grafana_mcp",
        "tool": "discover_loki_labels" if discover else "query_loki_logs",
        "requested_at": now(),
        "related_prometheus_evidence_id": reference.get("evidence_id"),
        "related_prometheus_file": str(path),
        "arguments": arguments,
        "status": "pending",
        "response": None,
        "discovery_calls": [],
        "summary": None,
        "error_type": None,
        "error_message": None,
    }
    print(f"참조: {path.name}")
    print(f"동일 시간 범위: {window['startRfc3339']} ~ {window['endRfc3339']}")
    try:
        async with asyncio.timeout(90):
            async with Client(create_grafana_server()) as client:
                calls = (
                    [
                        ("list_loki_label_names", window),
                        ("list_loki_label_values", {**window, "labelName": "job"}),
                    ]
                    if discover
                    else [("query_loki_logs", arguments)]
                )
                discovery = {}
                for name, call_args in calls:
                    result = await client.call_tool(name, arguments=call_args)
                    raw = result.model_dump(mode="json", by_alias=True)
                    if discover:
                        record["discovery_calls"].append(
                            {"tool": name, "arguments": call_args, "response": raw}
                        )
                    else:
                        record["response"] = raw
                    if result.is_error:
                        record["status"] = "tool_error"
                        record["error_type"] = "MCPToolError"
                        break
                    try:
                        if discover:
                            values = decode_payload(raw)
                            if not isinstance(values, list) or not all(
                                isinstance(v, str) for v in values
                            ):
                                raise TypeError(
                                    "라벨 조회 결과가 문자열 배열이 아닙니다."
                                )
                            discovery[name] = values
                        else:
                            record["summary"] = parse_loki_response(raw, limit=limit)
                            record["status"] = record["summary"]["status"]
                    except (ValueError, TypeError, KeyError) as exc:
                        record["status"] = "parse_error"
                        record["error_type"] = type(exc).__name__
                        record["error_message"] = str(exc)
                        break
                else:
                    if discover:
                        record["summary"] = discovery
                        record["status"] = "discovery_completed"
    except TimeoutError:
        record["status"] = "timeout"
        record["error_type"] = "TimeoutError"
    # 실패해도 원본·조회 조건을 저장하기 위한 실행 경계.
    except Exception as exc:  # noqa: BLE001
        record["status"] = "connection_or_protocol_error"
        record["error_type"] = type(exc).__name__

    record["completed_at"] = now()
    output_dir = output_dir or PROJECT_DIR / "artifacts"
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = "loki-labels" if discover else "loki"
    output_path = (
        output_dir
        / f"{prefix}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{record['evidence_id'][:8]}.json"
    )
    output_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": record["status"],
                "summary": record["summary"],
                "error_type": record["error_type"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"결과 저장: {output_path}")
    return output_path, record


async def collect(
    reference_path: Path | None, *, logql: str, limit: int, discover: bool
) -> int:
    _, record = await collect_evidence(
        reference_path, logql=logql, limit=limit, discover=discover
    )
    return (
        0
        if record["status"] in {"data_available", "no_data", "discovery_completed"}
        else 1
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prometheus evidence와 동일한 시간 범위의 Loki 로그 조회"
    )
    parser.add_argument(
        "--prometheus-evidence",
        type=Path,
        help="생략하면 가장 최근 저장한 Prometheus evidence",
    )
    parser.add_argument("--logql", default=DEFAULT_LOGQL)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--discover", action="store_true", help="라벨 이름과 job 값만 조회"
    )
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error("--limit은 1~100이어야 합니다.")
    if not args.logql.strip():
        parser.error("--logql은 비어 있을 수 없습니다.")
    try:
        code = asyncio.run(
            collect(
                args.prometheus_evidence,
                logql=args.logql,
                limit=args.limit,
                discover=args.discover,
            )
        )
    except (ValueError, TypeError, KeyError, OSError) as exc:
        parser.error(str(exc))
    else:
        raise SystemExit(code)


if __name__ == "__main__":
    main()
