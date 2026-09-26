import asyncio
import json
import shutil
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from dotenv import load_dotenv
from mcp import Client, StdioServerParameters

from check_grafana_mcp import PROJECT_DIR, required_env
from prometheus_parser import parse_prometheus_response

DATASOURCE_UID = "grafanacloud-prom"

PROMQL = "sum by (api) (rate(external_api_request_total[5m]))"


def to_rfc3339(value: datetime) -> str:
    """UTC 시각을 API가 받는 문자열로 변환한다."""
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


async def main() -> None:
    load_dotenv(PROJECT_DIR / ".env")

    uvx_path = shutil.which("uvx")
    if uvx_path is None:
        raise RuntimeError("uvx를 찾을 수 없습니다.")

    server = StdioServerParameters(
        command=uvx_path,
        args=[
            "mcp-grafana",
            "-t",
            "stdio",
            "--disable-write",
            "--enabled-tools",
            "datasource,prometheus,loki",
        ],
        env={
            "GRAFANA_URL": required_env("GRAFANA_URL").rstrip("/"),
            "GRAFANA_SERVICE_ACCOUNT_TOKEN": required_env(
                "GRAFANA_SERVICE_ACCOUNT_TOKEN"
            ),
        },
    )

    # 조회 기준 시각을 한 번만 계산한다.
    # 나중에 Loki에도 같은 시작·종료 시각을 전달한다.
    end_time = datetime.now(UTC).replace(microsecond=0)
    start_time = end_time - timedelta(minutes=30)

    arguments = {
        "datasourceUid": DATASOURCE_UID,
        "expr": PROMQL,
        "queryType": "range",
        "startTime": to_rfc3339(start_time),
        "endTime": to_rfc3339(end_time),
        "stepSeconds": 60,
    }

    record = {
        "evidence_id": str(uuid4()),
        "source": "grafana_mcp",
        "tool": "query_prometheus",
        "requested_at": to_rfc3339(datetime.now(UTC)),
        "arguments": arguments,
        "status": "pending",
        "response": None,
        "summary": None,
        "error_type": None,
        "error_message": None,
    }

    print(f"데이터소스: {DATASOURCE_UID}")
    print(f"조회 구간: {arguments['startTime']} ~ {arguments['endTime']}")
    print(f"쿼리: {PROMQL}")

    try:
        # 서버 시작·연결·조회 전체에 시간 제한을 둔다.
        async with asyncio.timeout(90):
            async with Client(server) as client:
                result = await client.call_tool(
                    "query_prometheus",
                    arguments=arguments,
                )

                # 요약과 별도로 원본 응답을 보존한다.
                record["response"] = result.model_dump(
                    mode="json",
                    by_alias=True,
                )

                if result.is_error:
                    record["status"] = "tool_error"
                    record["error_type"] = "MCPToolError"
                    print("조회 도구가 오류를 반환했습니다. 원본 응답을 확인하세요.")
                else:
                    try:
                        summary = parse_prometheus_response(record["response"])
                    except (ValueError, TypeError, KeyError) as exc:
                        record["status"] = "parse_error"
                        record["error_type"] = type(exc).__name__
                        record["error_message"] = str(exc)
                        print(f"응답 해석 실패: {exc}")
                    else:
                        record["summary"] = summary
                        record["status"] = summary["status"]
                        print(json.dumps(summary, ensure_ascii=False, indent=2))

    except TimeoutError:
        record["status"] = "timeout"
        record["error_type"] = "TimeoutError"
        print("90초 안에 연결 또는 조회를 완료하지 못했습니다.")

    # MCP 실행 경계에서는 예외 종류와 관계없이 실패 기록을 저장한다.
    except Exception as exc:  # noqa: BLE001
        record["status"] = "connection_or_protocol_error"
        record["error_type"] = type(exc).__name__
        print(f"연결 또는 통신 오류: {type(exc).__name__}")

    record["completed_at"] = to_rfc3339(datetime.now(UTC))

    output_dir = PROJECT_DIR / "artifacts"
    output_dir.mkdir(parents=True, exist_ok=True)

    output_path = output_dir / (
        f"prometheus-{end_time.strftime('%Y%m%dT%H%M%SZ')}"
        f"-{record['evidence_id'][:8]}.json"
    )

    output_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"\n조회 상태: {record['status']}")
    print(f"결과 저장: {output_path}")

    if record["status"] not in {"data_available", "no_data"}:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
