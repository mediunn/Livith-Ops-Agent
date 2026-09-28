"""HTTP 선택·가설 갱신을 비교하는 작은 개발용 합성 평가. 독립 평가 세트가 아니다."""

import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from mcp.types import CallToolResult
from ollama import AsyncClient

from ops_agent.agent.http.graph import initial_state
from ops_agent.agent.http.session import run
from ops_agent.config import NUM_CTX, NUM_PREDICT, OLLAMA_HOST, PROJECT_DIR
from ops_agent.evaluation.http_metrics import aggregate, call_metrics, classify
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
        "within_tool_budget": report["budget"]["tool_calls"]
        <= report.get("limits", {}).get("tool_calls", 6),
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


async def warm_model(model: str) -> dict:
    started = time.monotonic()
    result = {"model": model, "outside_investigation_budget": True, "status": "running"}
    try:
        client = AsyncClient(host=OLLAMA_HOST, timeout=180)
        models = await client.list()
        result["installed_model"] = [
            m.model_dump(mode="json") for m in models.models if m.model == model
        ]
        response = await client.chat(
            model=model, messages=[], stream=False, options={"num_ctx": NUM_CTX}
        )
        result.update(status="completed", response=response.model_dump(mode="json"))
    except Exception as exc:  # noqa: BLE001
        result.update(status="failed", error_type=type(exc).__name__)
    result["elapsed_seconds"] = time.monotonic() - started
    return result


async def evaluate(
    root: Path,
    *,
    planners=("rules",),
    model="qwen2.5:3b",
    models=None,
    cases=CASES,
    repeats=1,
    seconds=180,
    llm_seconds=45,
    warmup=False,
    on_progress=None,
):
    models = list(models) if models is not None else [model]
    if (
        not models
        or len(set(models)) != len(models)
        or any(not m.strip() or m.endswith("-cloud") for m in models)
    ):
        raise ValueError("중복 없는 로컬 모델 목록이 필요합니다.")
    if (
        not planners
        or len(set(planners)) != len(planners)
        or set(planners) - {"rules", "llm"}
    ):
        raise ValueError("유효한 planner 목록이 필요합니다.")
    if (
        not 1 <= repeats <= 10
        or not 1 <= seconds <= 3600
        or not 1 <= llm_seconds <= 180
    ):
        raise ValueError(
            "반복은 1~10회, 전체 시간은 1~3600초, 호출 시간은 1~180초여야 합니다."
        )
    if not cases or len({c["id"] for c in cases}) != len(cases):
        raise ValueError("중복 없는 평가 사례가 필요합니다.")
    root.mkdir(parents=True, exist_ok=False)
    sources = [
        *sorted((PROJECT_DIR / "ops_agent/agent/http").glob("*.py")),
        PROJECT_DIR / "ops_agent/tools/http.py",
        Path(__file__),
        PROJECT_DIR / "ops_agent/evaluation/http_metrics.py",
        PROJECT_DIR / "ops_agent/config.py",
        PROJECT_DIR / "prompts/agent/http_agent_v1.txt",
    ]
    metadata = {
        "created_at": datetime.now(UTC).isoformat(),
        "models": models,
        "planners": list(planners),
        "repeats": repeats,
        "scope": SCOPE,
        "cases": cases,
        "warmup": warmup,
        "limits": {
            "seconds": seconds,
            "llm_seconds": llm_seconds,
            "tool_calls": 6,
            "llm_calls": 8,
        },
        "options": {"temperature": 0, "num_ctx": NUM_CTX, "num_predict": NUM_PREDICT},
        "order": "rotate model order each repetition; run sequentially",
        "evidence_ids": "random UUID per investigation; observations are fixed",
        "source_hashes": {
            str(p.relative_to(PROJECT_DIR)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sources
        },
        "dataset_sha256": hashlib.sha256(
            json.dumps(cases, sort_keys=True).encode()
        ).hexdigest(),
    }
    save_json(root / "metadata.json", metadata)
    summary = {
        "dataset": "http-development-v1",
        "independent": False,
        "status": "running",
        "results": [],
        "aggregate": [],
    }

    def persist():
        summary["aggregate"] = aggregate(summary["results"])
        save_json(root / "summary.json", summary)

    persist()
    try:
        for repeat in range(1, repeats + 1):
            offset = (repeat - 1) % len(models)
            ordered = models[offset:] + models[:offset]
            batches = [
                (p, m)
                for p in planners
                for m in (ordered if p == "llm" else [models[0]])
            ]
            for planner, selected_model in batches:
                if warmup and planner == "llm":
                    if on_progress:
                        on_progress(
                            {
                                "event": "warmup",
                                "repeat": repeat,
                                "model": selected_model,
                            }
                        )
                    warmed = await warm_model(selected_model)
                    save_json(
                        root / f"warmup-{repeat}-{models.index(selected_model)}.json",
                        warmed,
                    )
                for case in cases:
                    started = time.monotonic()
                    directory = root / uuid4().hex
                    state = initial_state(
                        directory,
                        HTTPQuery(**SCOPE),
                        symptom=case["symptom"],
                        planner_kind=planner,
                        model=selected_model,
                        seconds=seconds,
                        llm_seconds=llm_seconds,
                    )
                    report = await run(
                        directory, state, tool_executor=fixture_executor(case)
                    )
                    scored, metrics = score(case, report), call_metrics(directory)
                    row = {
                        "case": case["id"],
                        "repeat": repeat,
                        "planner": planner,
                        "model": selected_model if planner == "llm" else None,
                        "report": str(directory / "report.json"),
                        "budget": report["budget"],
                        "stop_reason": report["stop_reason"],
                        "error_type": report.get("error_type"),
                        "wall_seconds": round(time.monotonic() - started, 3),
                        "metrics": metrics,
                        "outcome": classify(report, scored, metrics),
                        **scored,
                    }
                    summary["results"].append(row)
                    persist()
                    if on_progress:
                        on_progress(
                            {
                                "event": "result",
                                **{k: v for k, v in row.items() if k != "metrics"},
                            }
                        )
        summary["status"] = "completed"
    except BaseException as exc:
        summary.update(status="interrupted_or_failed", error_type=type(exc).__name__)
        raise
    finally:
        persist()
    return summary
