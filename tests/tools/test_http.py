import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from mcp.types import CallToolResult, TextContent
from pydantic import ValidationError

from ops_agent.agent.budget import read_budget
from ops_agent.persistence.artifacts import query_key, read_json
from ops_agent.tools.http import HTTPQuery, execute_http_query, prepare_query


def make_query(**updates):
    return HTTPQuery.model_validate(
        {
            "metric": "http_request_rate",
            "start_at": "2026-09-26T13:00:00Z",
            "end_at": "2026-09-26T14:00:00Z",
            **updates,
        }
    )


class FakeClient:
    def __init__(self, data=None, *, error=False):
        self.calls = []
        self.data = [] if data is None else data
        self.error = error

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return CallToolResult(
            isError=self.error,
            content=[TextContent(type="text", text=json.dumps({"data": self.data}))],
        )


def test_request_scope_and_literal_route_are_preserved():
    route = '/api//concerts/:id"} or vector(1) #'
    arguments, measurement = prepare_query(make_query(route=route, method="GET"))
    expr = arguments["expr"]
    assert 'job="livith-server-production"' in expr
    assert 'method="GET"' in expr
    assert "route=" + json.dumps(route) in expr
    assert "sum by (method, route)" in expr
    assert "http_request_total" in expr
    assert arguments["datasourceUid"] == "grafanacloud-prom"
    assert measurement["unit"] == "requests_per_second"
    assert measurement["route"] == route
    assert measurement["metric_service_filter_applied"] is True


def test_latency_uses_weighted_sum_over_count_and_excludes_zero_denominator():
    arguments, measurement = prepare_query(make_query(metric="http_mean_latency"))
    assert "1000 *" in arguments["expr"]
    assert "rate(http_request_duration_seconds_sum{" in arguments["expr"]
    assert "rate(http_request_duration_seconds_count{" in arguments["expr"]
    assert "> 0" in arguments["expr"]
    assert "or vector(0)" not in arguments["expr"]
    assert measurement["unit"] == "milliseconds"
    assert measurement["aggregation"] == "request_weighted_mean_over_5m"
    assert "route=" not in arguments["expr"]


@pytest.mark.parametrize(
    "updates",
    [
        {"metric": "arbitrary_query"},
        {"expr": "up"},
        {"job": "another-job"},
        {"datasourceUid": "another-datasource"},
        {"method": "GET|POST"},
        {"route": ""},
        {"route": "not-a-route"},
        {"route": "/a\n"},
        {"route": "/" + "x" * 300},
        {"start_at": "2026-09-26T13:00:00"},
        {"end_at": "2026-09-26T13:00:00Z"},
        {"end_at": "2026-09-26T12:00:00Z"},
        {"start_at": "2026-09-25T12:59:59Z"},
        {"end_at": datetime.now(UTC) + timedelta(hours=1)},
    ],
)
def test_invalid_scope_is_rejected(updates):
    with pytest.raises(ValidationError):
        make_query(**updates)


def test_equivalent_timezones_produce_same_query_key():
    first, _ = prepare_query(make_query())
    second, _ = prepare_query(
        make_query(
            start_at="2026-09-26T22:00:00+09:00", end_at="2026-09-26T23:00:00+09:00"
        )
    )
    assert first == second
    assert query_key("query_prometheus", first) == query_key("query_prometheus", second)


def test_different_routes_use_separate_cache_and_same_query_reuses_id(state):
    client = FakeClient()
    first = asyncio.run(
        execute_http_query(state, make_query(route="/one"), client=client)
    )
    second = asyncio.run(
        execute_http_query(state, make_query(route="/two"), client=client)
    )
    cached = asyncio.run(
        execute_http_query(state, make_query(route="/one"), client=client)
    )
    assert cached == first
    assert first["query_key"] != second["query_key"]
    assert first["evidence_id"] != second["evidence_id"]
    assert len(client.calls) == read_budget(state)["tool_calls"] == 2
    assert first["status"] == "no_data"
    assert first["summary"]["series"] == []


def test_latency_evidence_preserves_units_labels_invalid_samples_and_raw(state):
    client = FakeClient(
        [
            {
                "metric": {"method": "GET", "route": "/api//concerts/:id"},
                "values": [[100, "12.5"], [160, "NaN"]],
            }
        ]
    )
    evidence = asyncio.run(
        execute_http_query(state, make_query(metric="http_mean_latency"), client=client)
    )
    assert evidence["measurement"]["unit"] == "milliseconds"
    series = evidence["summary"]["series"][0]
    assert series["labels"]["route"] == "/api//concerts/:id"
    assert series["max_value"] == 12.5
    assert series["latest_value"] is None
    assert series["invalid_sample_count"] == 1
    assert "response" not in evidence
    from pathlib import Path

    assert read_json(Path(evidence["artifact"]))["response"] is not None


def test_validation_cannot_be_bypassed_at_execution_boundary(state):
    query = make_query().model_copy(update={"metric": "injected"})
    client = FakeClient()
    with pytest.raises(ValidationError):
        asyncio.run(execute_http_query(state, query, client=client))
    assert not client.calls
    assert read_budget(state)["tool_calls"] == 0


def test_tool_failure_is_saved_and_charged(state):
    client = FakeClient(error=True)
    result = asyncio.run(execute_http_query(state, make_query(), client=client))
    assert result["status"] == "tool_error"
    assert result["error_type"] == "MCPToolError"
    assert read_budget(state)["tool_calls"] == 1
