"""HTTP 도구 직접 실행. 모델 판단 없이 검증된 조회와 원본·요약을 저장한다."""

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp import Client

from ops_agent.agent.budget import initialize_budget, read_budget, remaining_seconds
from ops_agent.agent.request import DEFAULT_TIMEZONE, parse_time
from ops_agent.collectors.grafana import create_grafana_server
from ops_agent.config import HTTP_ROOT
from ops_agent.persistence.artifacts import save_json
from ops_agent.tools.http import HTTP_TOOLS, HTTPQuery, execute_http_query


def queries_from_args(args) -> list[HTTPQuery]:
    ZoneInfo(args.timezone)
    if (args.start is None) != (args.end is None):
        raise ValueError("--start와 --end를 함께 입력하세요.")
    if args.start is None:
        end = datetime.now(UTC).replace(microsecond=0)
        start = end - timedelta(minutes=30)
    else:
        start = parse_time(args.start, args.timezone)
        end = parse_time(args.end, args.timezone)
    metrics = list(HTTP_TOOLS) if args.metric == "all" else [args.metric]
    return [
        HTTPQuery(
            metric=metric,
            start_at=start,
            end_at=end,
            route=args.route,
            method=args.method,
        )
        for metric in metrics
    ]


async def run_queries(queries: list[HTTPQuery], *, seconds: int = 90) -> dict:
    if not queries or len(queries) > 6:
        raise ValueError("한 실행에는 1~6개 조회를 지정하세요.")
    if type(seconds) is not int or not 1 <= seconds <= 3600:
        raise ValueError("실행 시간 예산은 1~3600초입니다.")
    queries = [HTTPQuery.model_validate(q.model_dump()) for q in queries]
    run_id = uuid4().hex
    directory = HTTP_ROOT / run_id
    directory.mkdir(parents=True, exist_ok=False)
    initialize_budget(directory)
    state = {
        "thread_id": run_id,
        "directory": str(directory),
        "deadline": time.time() + seconds,
        "limits": {"tool_calls": len(queries), "llm_calls": 0, "reserved_tokens": 0},
    }
    result = {
        "run_id": run_id,
        "status": "incomplete",
        "requests": [q.model_dump(mode="json") for q in queries],
        "facts": [],
        "error_type": None,
    }
    save_json(directory / "requests.json", {"requests": result["requests"]})
    try:
        async with asyncio.timeout(remaining_seconds(state)):
            # 이번 실행의 모든 조회가 같은 읽기 전용 MCP 세션을 사용한다.
            async with Client(create_grafana_server()) as client:
                for query in queries:
                    evidence = await execute_http_query(state, query, client=client)
                    result["facts"].append(evidence)
                    if evidence["status"] not in {"data_available", "no_data"}:
                        break
                else:
                    result["status"] = "completed"
    except asyncio.CancelledError:
        result.update(status="interrupted", error_type="CancelledError")
        raise
    except Exception as exc:  # noqa: BLE001
        result.update(status="incomplete", error_type=type(exc).__name__)
    finally:
        result.update(budget=read_budget(state), completed_at=time.time())
        save_json(directory / "result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="HTTP 요청률·평균 지연 읽기 전용 조회")
    parser.add_argument("--metric", choices=["all", *HTTP_TOOLS], default="all")
    parser.add_argument("--route", help="실제 route 라벨의 정확한 값. 생략하면 전체")
    parser.add_argument(
        "--method", choices=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    )
    parser.add_argument("--start", help="ISO 8601 시작 시각. --end와 함께 지정")
    parser.add_argument("--end", help="ISO 8601 종료 시각")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--seconds", type=int, default=90)
    args = parser.parse_args()
    try:
        if not 1 <= args.seconds <= 3600:
            raise ValueError("--seconds는 1~3600이어야 합니다.")
        queries = queries_from_args(args)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        parser.error(str(exc))
    try:
        result = asyncio.run(run_queries(queries, seconds=args.seconds))
    except KeyboardInterrupt:
        return 130
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
