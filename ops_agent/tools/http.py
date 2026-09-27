"""검증된 HTTP 조회 조건을 읽기 전용 PromQL 템플릿으로 변환한다."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from ops_agent.collectors.prometheus_parser import parse_prometheus_response
from ops_agent.config import HTTP_DATASOURCE_UID, HTTP_JOB
from ops_agent.tools.grafana import execute_query

HTTPMetric = Literal["http_request_rate", "http_mean_latency"]
HTTPMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


class HTTPQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    metric: HTTPMetric
    start_at: AwareDatetime
    end_at: AwareDatetime
    route: str | None = Field(default=None, strict=True, min_length=1, max_length=300)
    method: HTTPMethod | None = None

    @field_validator("route")
    @classmethod
    def validate_route(cls, value: str | None) -> str | None:
        if value is not None and (
            not value.startswith("/")
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError("route는 /로 시작하는 제어문자 없는 라벨 값이어야 합니다.")
        return value  # // 및 :id를 포함한 실제 route 라벨을 그대로 보존한다.

    @field_validator("start_at", "end_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_window(self):
        if not timedelta(0) < self.end_at - self.start_at <= timedelta(hours=24):
            raise ValueError("HTTP 조회 범위는 0초 초과, 최대 24시간입니다.")
        if self.end_at > datetime.now(UTC):
            raise ValueError("미래 시각까지 조회할 수 없습니다.")
        return self


def _request_rate(selector: str) -> str:
    return f"sum by (method, route) (rate(http_request_total{{{selector}}}[5m]))"


def _mean_latency(selector: str) -> str:
    total = (
        "sum by (method, route) "
        f"(rate(http_request_duration_seconds_sum{{{selector}}}[5m]))"
    )
    count = (
        "sum by (method, route) "
        f"(rate(http_request_duration_seconds_count{{{selector}}}[5m]))"
    )
    return f"1000 * ({total}) / ({count} > 0)"


@dataclass(frozen=True)
class HTTPTool:
    description: str
    unit: str
    expression: Callable[[str], str]


HTTP_TOOLS = {
    "http_request_rate": HTTPTool(
        "HTTP 메서드·엔드포인트별 초당 요청률", "requests_per_second", _request_rate
    ),
    "http_mean_latency": HTTPTool(
        "HTTP 메서드·엔드포인트별 최근 5분 요청의 평균 응답 시간",
        "milliseconds",
        _mean_latency,
    ),
}


def prepare_query(query: HTTPQuery) -> tuple[dict, dict]:
    # model_construct 등으로 만든 객체도 실행 경계에서 다시 검사한다.
    query = HTTPQuery.model_validate(query.model_dump())
    definition = HTTP_TOOLS[query.metric]
    labels = {"job": HTTP_JOB}
    if query.method is not None:
        labels["method"] = query.method
    if query.route is not None:
        labels["route"] = query.route
    selector = ",".join(
        f"{name}={json.dumps(value, ensure_ascii=False)}"
        for name, value in labels.items()
    )
    arguments = {
        "datasourceUid": HTTP_DATASOURCE_UID,
        "expr": definition.expression(selector),
        "queryType": "range",
        "startTime": query.start_at.isoformat(),
        "endTime": query.end_at.isoformat(),
        "stepSeconds": 60,
    }
    measurement = {
        "unit": definition.unit,
        "rate_window_seconds": 300,
        "query_step_seconds": 60,
        "sample_count_meaning": "평가 시점 수이며 요청 건수가 아님",
        "scope": "HTTP job 필터 범위의 method·route별 관측",
        "job": HTTP_JOB,
        "method": query.method,
        "route": query.route,
        "metric_service_filter_applied": True,
        "aggregation": "request_rate"
        if query.metric == "http_request_rate"
        else "request_weighted_mean_over_5m",
        "empty_result_meaning": "관측 없음. 요청 없음·계측 누락 등을 구분하지 않으며 0으로 대체하지 않음",
    }
    return arguments, measurement


async def execute_http_query(
    state: dict, query: HTTPQuery, *, client=None, trace=None
) -> dict:
    arguments, measurement = prepare_query(query)
    return await execute_query(
        state,
        action=query.metric,
        tool="query_prometheus",
        arguments=arguments,
        measurement=measurement,
        parse_response=parse_prometheus_response,
        client=client,
        trace=trace,
    )
