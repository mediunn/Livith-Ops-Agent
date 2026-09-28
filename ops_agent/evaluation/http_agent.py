"""HTTP 선택·가설 갱신을 비교하는 작은 개발용 합성 평가. 독립 평가 세트가 아니다."""

from datetime import datetime
from pathlib import Path
from uuid import uuid4

from mcp.types import CallToolResult

from ops_agent.agent.http.graph import initial_state
from ops_agent.agent.http.session import run
from ops_agent.persistence.artifacts import save_json
from ops_agent.tools.http import HTTPQuery, execute_http_query

SCOPE = {
    "metric": "http_request_rate",
    "start_at": "2026-09-26T13:00:00Z",
    "end_at": "2026-09-26T14:00:00Z",
}
CASES = [
    {
        "id": "latency_increase",
        "symptom": "/checkout 평균 지연이 직전 구간보다 높아졌는지 확인해 주세요.",
        "metric": "http_mean_latency",
        "route": "/checkout",
        "current": 800,
        "previous": 100,
        "expected_status": "supported",
    },
    {
        "id": "traffic_drop",
        "symptom": "/orders 요청률이 직전 구간에 비해 0으로 감소했는지 확인해 주세요.",
        "metric": "http_request_rate",
        "route": "/orders",
        "current": 0,
        "previous": 8,
        "expected_status": "supported",
    },
    {
        "id": "contradicted_latency_increase",
        "symptom": "/checkout 평균 지연이 직전 구간보다 높아졌다는 의심을 검증해 주세요.",
        "metric": "http_mean_latency",
        "route": "/checkout",
        "current": 800,
        "previous": 800,
        "expected_status": "rejected",
    },
    {"id": "no_data", "symptom": "HTTP 요청률과 평균 지연의 변화를 확인해 주세요."},
]


def fixture_executor(case: dict):
    """조건별 합성 MCP 응답을 실제 실행기·파서·캐시·예산 경로에 통과시킨다."""

    async def execute(state, query):
        class Client:
            async def call_tool(self, name, arguments):
                series = []
                if case["id"] != "no_data":
                    for route in ("/internal/poll", "/checkout", "/orders"):
                        if query.route and query.route != route:
                            continue
                        if query.method and query.method != "GET":
                            continue
                        value = (
                            (25000 if route == "/internal/poll" else 100)
                            if query.metric == "http_mean_latency"
                            else 5
                        )
                        if route == case["route"] and query.metric == case["metric"]:
                            previous = query.end_at <= datetime.fromisoformat(
                                SCOPE["start_at"]
                            )
                            value = case["previous" if previous else "current"]
                        series.append(
                            {
                                "metric": {"method": "GET", "route": route},
                                "values": [
                                    [query.start_at.timestamp(), str(value)],
                                    [query.end_at.timestamp(), str(value)],
                                ],
                            }
                        )
                return CallToolResult(content=[], structuredContent={"data": series})

        return await execute_http_query(state, query, client=Client())

    return execute


def score(case: dict, report: dict) -> dict:
    facts = report["facts"]
    checks = {
        "completed": report["status"] == "completed",
        "no_duplicate_queries": len({e["query_key"] for e in facts}) == len(facts),
        "within_tool_budget": report["budget"]["tool_calls"] <= 6,
    }
    if case["id"] == "no_data":
        checks["no_unnecessary_followup"] = len(facts) == 2 and not report["decisions"]
        return {
            "checks": checks,
            "hypothesis_status_match": None,
            "semantic_review": "pending",
        }
    target = [
        e
        for e in facts
        if e["action"] == case["metric"]
        and e["measurement"]["route"] == case["route"]
        and datetime.fromisoformat(e["arguments"]["endTime"])
        == datetime.fromisoformat(SCOPE["start_at"])
    ]
    checks["targeted_previous_query"] = bool(target)
    initial = [
        e
        for e in facts
        if e["action"] == case["metric"] and e["measurement"]["route"] is None
    ]
    hypotheses = report["interpretation"]["hypotheses"]
    # 상태·양쪽 구간 인용만 기계 검사한다. 가설 문장의 의미는 별도 검토해야 한다.
    hypothesis_match = any(
        h["status"] == case["expected_status"]
        and any(e["evidence_id"] in h["evidence_ids"] for e in target)
        and any(e["evidence_id"] in h["evidence_ids"] for e in initial)
        for h in hypotheses
    )
    return {
        "checks": checks,
        "hypothesis_status_match": hypothesis_match,
        "semantic_review": "pending",
    }


async def evaluate(root: Path, *, planners=("rules",), model="qwen2.5:3b", cases=CASES):
    results = []
    for case in cases:
        for planner in planners:
            directory = root / uuid4().hex
            state = initial_state(
                directory,
                HTTPQuery(**SCOPE),
                symptom=case["symptom"],
                planner_kind=planner,
                model=model,
            )
            report = await run(directory, state, tool_executor=fixture_executor(case))
            results.append(
                {
                    "case": case["id"],
                    "planner": planner,
                    "model": model if planner == "llm" else None,
                    "report": str(directory / "report.json"),
                    "budget": report["budget"],
                    "stop_reason": report["stop_reason"],
                    **score(case, report),
                }
            )
    summary = {
        "dataset": "http-development-v1",
        "independent": False,
        "results": results,
    }
    save_json(root / "summary.json", summary)
    return summary
