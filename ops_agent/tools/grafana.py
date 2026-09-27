"""허용된 고정 쿼리만 실행한다. 원본 응답은 로컬에만 저장한다."""

import asyncio
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from mcp import Client

from ops_agent.agent.budget import BudgetExceeded, remaining_seconds, reserve
from ops_agent.collectors.grafana import create_grafana_server
from ops_agent.collectors.loki_parser import parse_loki_response
from ops_agent.collectors.prometheus import PROMQL
from ops_agent.collectors.prometheus_parser import parse_prometheus_response
from ops_agent.persistence.artifacts import (
    cached_query,
    query_artifact_path,
    query_key,
    read_json,
    save_json,
)

CATALOG = {
    "current_metrics": "요청한 조회 구간의 외부 API별 초당 요청률",
    "previous_metrics": "요청 구간과 길이가 같은 직전 구간의 요청률",
    "logs": "요청 구간의 서비스 로그 최대 100건 요약",
    "warning_logs": "요청 구간의 warn/error 문자열 매칭 로그 최대 100건. 실제 로그 레벨 필터가 아님",
}


def query_spec(state: dict, action: str) -> tuple[str, dict]:
    if action not in CATALOG:
        raise ValueError("허용되지 않은 도구입니다.")
    start = datetime.fromisoformat(state["window"]["start"])
    end = datetime.fromisoformat(state["window"]["end"])
    if start.utcoffset() is None or end.utcoffset() is None or start >= end:
        raise ValueError("시간대를 포함한 유효한 조회 구간이 필요합니다.")
    if action == "previous_metrics":
        duration = end - start
        end, start = start, start - duration
    if action in {"current_metrics", "previous_metrics"}:
        return "query_prometheus", {
            "datasourceUid": "grafanacloud-prom",
            "expr": PROMQL,
            "queryType": "range",
            "startTime": start.isoformat(),
            "endTime": end.isoformat(),
            "stepSeconds": 60,
        }
    logql = '{job="livith-server"}'
    if action == "warning_logs":
        logql += ' |~ "(?i)(warn|error)"'
    return "query_loki_logs", {
        "datasourceUid": "grafanacloud-logs",
        "logql": logql,
        "queryType": "range",
        "startRfc3339": start.isoformat(),
        "endRfc3339": end.isoformat(),
        "direction": "backward",
        "format": "full",
        "limit": 100,
    }


def query_already_collected(state: dict, action: str) -> bool:
    tool, arguments = query_spec(state, action)
    key = query_key(tool, arguments)
    return any(
        query_key(item["tool"], item["arguments"]) == key for item in state["evidence"]
    )


def measurement_for(tool: str) -> dict:
    if tool == "query_prometheus":
        return {
            "unit": "requests_per_second",
            "rate_window_seconds": 300,
            "query_step_seconds": 60,
            "sample_count_meaning": "평가 시점 수이며 요청 건수가 아님",
            "scope": "외부 API 요청률이며 서비스 전체 요청률이 아님",
        }
    return {"scope": "returned_logs_only", "log_bodies_included": False}


def without_raw_response(record: dict) -> dict:
    return {key: value for key, value in record.items() if key != "response"}


async def execute_tool(state: dict, trace=None) -> dict:
    action = state["action"]
    tool, arguments = query_spec(state, action)
    return await execute_query(
        state,
        action=action,
        tool=tool,
        arguments=arguments,
        measurement=measurement_for(tool),
        parse_response=parse_prometheus_response
        if tool == "query_prometheus"
        else lambda raw: parse_loki_response(raw, limit=100),
        legacy_cache=True,
        trace=trace,
    )


async def execute_query(
    state: dict,
    *,
    action: str,
    tool: str,
    arguments: dict,
    measurement: dict,
    parse_response: Callable[[dict], dict],
    legacy_cache: bool = False,
    client=None,
    trace=None,
) -> dict:
    """검증된 도구 빌더의 조회를 공통 예산·캐시·원본 보존 경로로 실행한다."""
    path = query_artifact_path(state["directory"], tool, arguments)
    cached = cached_query(
        path,
        thread_id=state["thread_id"],
        tool=tool,
        arguments=arguments,
    )

    if cached is None and legacy_cache:
        # 기존 실행은 조건이 일치하는 완료 기록만 재사용한다.
        legacy_path = Path(state["directory"]) / f"{action}.json"
        if legacy_path.exists():
            legacy = read_json(legacy_path)
            if legacy.get("tool") == tool and legacy.get("arguments") == arguments:
                cached = cached_query(
                    legacy_path,
                    thread_id=state["thread_id"],
                    tool=tool,
                    arguments=arguments,
                )

    if cached is not None:
        if cached.get("status") == "pending" or not cached.get("completed_at"):
            raise ValueError("완료되지 않은 근거 파일입니다.")

        if not path.exists():
            cached = {
                **cached,
                "query_key": query_key(tool, arguments),
                "artifact": str(path),
            }
            save_json(path, cached)

        return {**without_raw_response(cached), "action": action}

    remaining_seconds(state)
    reserve(state, "tool")

    record = {
        "thread_id": state["thread_id"],
        "action": action,
        "evidence_id": str(uuid4()),
        "query_key": query_key(tool, arguments),
        "source": "grafana_mcp",
        "tool": tool,
        "arguments": arguments,
        "measurement": measurement,
        "status": "pending",
        "summary": None,
        "response": None,
        "error_type": None,
        "started_at": time.time(),
        "artifact": str(path),
    }
    span = trace.start_step(action, arguments) if trace else None
    try:
        async with asyncio.timeout(min(20.0, remaining_seconds(state))):
            if client is not None:
                result = await client.call_tool(tool, arguments=arguments)
            else:
                async with Client(create_grafana_server()) as connection:
                    result = await connection.call_tool(tool, arguments=arguments)
        raw = result.model_dump(mode="json", by_alias=True)
        record["response"] = raw
        if result.is_error:
            record.update(status="tool_error", error_type="MCPToolError")
        else:
            summary = parse_response(raw)
            record.update(summary=summary, status=summary["status"])
    except BudgetExceeded:
        record.update(status="time_budget", error_type="BudgetExceeded")
        raise
    except TimeoutError:
        record.update(status="timeout", error_type="TimeoutError")
    except asyncio.CancelledError:
        record.update(status="interrupted", error_type="CancelledError")
        raise
    except Exception as exc:  # noqa: BLE001
        record.update(status="collection_error", error_type=type(exc).__name__)
    finally:
        # 취소 중에는 완료된 근거로 저장하지 않는다. 재개 시 읽기 호출을 재시도한다.
        if trace:
            trace.end_step(span, record)
    record["completed_at"] = time.time()
    save_json(path, record)
    return without_raw_response(record)
