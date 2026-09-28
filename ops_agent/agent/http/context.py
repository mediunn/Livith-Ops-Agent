"""관측에서 후속 조회 후보를 만들고 모델 선택을 검증된 쿼리로 변환한다."""

from collections import defaultdict
from datetime import timedelta

from ops_agent.agent.budget import read_budget
from ops_agent.agent.http.state import HTTPDecision
from ops_agent.persistence.artifacts import query_key
from ops_agent.tools.http import HTTP_TOOLS, HTTPQuery, prepare_query

MAX_ENDPOINTS = 12


def initial_queries(state: dict) -> list[HTTPQuery]:
    return [
        HTTPQuery.model_validate({**state["request"], "metric": name})
        for name in HTTP_TOOLS
    ]


def identity(query: HTTPQuery) -> str:
    arguments, _ = prepare_query(query)
    return query_key("query_prometheus", arguments)


def context_and_queries(state: dict) -> tuple[dict, dict]:
    scope = HTTPQuery.model_validate(state["request"])
    rows = defaultdict(dict)
    keys = {identity(q) for q in initial_queries(state)}
    for item in state["evidence"]:
        if item["query_key"] not in keys:
            continue
        for row in (item.get("summary") or {}).get("series", []):
            labels = row["labels"]
            route, method = labels.get("route"), labels.get("method")
            if not route or not method:
                continue
            if (scope.route is not None and route != scope.route) or (
                scope.method is not None and method != scope.method
            ):
                continue
            try:
                HTTPQuery.model_validate(
                    {**state["request"], "route": route, "method": method}
                )
            except ValueError:
                continue
            rows[(method, route)][item["action"]] = row
    ranked = sorted(
        rows,
        key=lambda key: (
            -(rows[key].get("http_mean_latency", {}).get("max_value") or 0),
            key,
        ),
    )
    endpoints = [
        {
            "id": f"e{index}",
            "method": key[0],
            "route": key[1],
            "observations": rows[key],
        }
        for index, key in enumerate(ranked[:MAX_ENDPOINTS])
    ]
    duration = scope.end_at - scope.start_at
    middle = scope.start_at + duration / 2
    windows = {
        "previous": (scope.start_at - duration, scope.start_at),
        "first_half": (scope.start_at, middle),
        "second_half": (middle, scope.end_at),
    }
    # 5분 rate보다 짧은 구간으로 좁히는 후보는 제공하지 않는다.
    if duration < timedelta(minutes=10):
        windows = {"previous": windows["previous"]}
    used = {item["query_key"] for item in state["evidence"]}
    candidates = {}
    available = []
    for endpoint in endpoints:
        for metric in HTTP_TOOLS:
            remaining = []
            for window, (start, end) in windows.items():
                query = HTTPQuery(
                    metric=metric,
                    start_at=start,
                    end_at=end,
                    method=endpoint["method"],
                    route=endpoint["route"],
                )
                if identity(query) not in used:
                    candidates[(endpoint["id"], metric, window)] = query
                    remaining.append(window)
            if remaining:
                available.append(
                    {
                        "endpoint_id": endpoint["id"],
                        "metric": metric,
                        "windows": remaining,
                    }
                )
    visible = {(e["method"], e["route"]) for e in endpoints}
    observations = []
    for item in state["evidence"]:
        summary = item.get("summary") or {}
        series = summary.get("series", [])
        selected = [
            s
            for s in series
            if (s["labels"].get("method"), s["labels"].get("route")) in visible
        ]
        observations.append(
            {
                "evidence_id": item["evidence_id"],
                "metric": item["action"],
                "status": item["status"],
                "unit": item["measurement"]["unit"],
                "start": item["arguments"]["startTime"],
                "end": item["arguments"]["endTime"],
                "series": selected,
                "omitted_series": len(series) - len(selected),
            }
        )
    context = {
        "symptom": state["symptom"],
        "investigation_update": investigation_update(state),
        "scope": state["request"],
        "endpoints": [
            {k: v for k, v in e.items() if k != "observations"} for e in endpoints
        ],
        "omitted_endpoints": max(0, len(ranked) - MAX_ENDPOINTS),
        "observations": observations,
        "windows": {
            name: {"start": start.isoformat(), "end": end.isoformat()}
            for name, (start, end) in windows.items()
        },
        "available_queries": available,
        "previous_decisions": [
            {
                k: v
                for k, v in d.items()
                if k not in {"query", "source", "evidence_considered"}
            }
            for d in state["decisions"]
        ],
        "remaining_tool_calls": state["limits"]["tool_calls"]
        - read_budget(state)["tool_calls"],
        "sample_count_meaning": "평가 시점 수이며 요청 건수가 아님. 5분 rate 구간은 서로 겹친다.",
    }
    return context, candidates


def investigation_update(state: dict) -> dict:
    """직전 판단 이후의 근거를 표시한다. 읽었거나 가설을 검증했다는 뜻은 아니다."""
    last = state["decisions"][-1] if state["decisions"] else None
    considered = set(last["evidence_considered"]) if last else set()
    last_query = None
    if last and last["query"]:
        key = identity(HTTPQuery.model_validate(last["query"]))
        last_query = {
            "endpoint_id": last["endpoint_id"],
            "metric": last["metric"],
            "window": last["window"],
            "purpose": last["rationale"],
            "result_evidence_ids": [
                e["evidence_id"] for e in state["evidence"] if e["query_key"] == key
            ],
        }
    return {
        "new_evidence_ids": [
            e["evidence_id"]
            for e in state["evidence"]
            if e["evidence_id"] not in considered
        ],
        "last_query": last_query,
        "hypotheses_before_new_evidence": last["hypotheses"] if last else [],
    }


def validate_choice(value, state: dict) -> tuple[HTTPDecision, HTTPQuery | None]:
    decision = HTTPDecision.model_validate(
        value.model_dump() if isinstance(value, HTTPDecision) else value
    )
    _, candidates = context_and_queries(state)
    ids = {e["evidence_id"] for e in state["evidence"]}
    for hypothesis in decision.hypotheses:
        if set(hypothesis.evidence_ids) - ids:
            raise ValueError("unknown_evidence_id")
    query = None
    if decision.action == "query":
        query = candidates.get((decision.endpoint_id, decision.metric, decision.window))
        if query is None:
            raise ValueError("unavailable_query")
    return decision, query
