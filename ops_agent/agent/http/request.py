"""공통 요청 해석에 HTTP에서 지원하는 지표를 추가한다."""

from pydantic import Field, field_validator, model_validator

from ops_agent.agent.contracts import RequestIntent
from ops_agent.tools.http import HTTPMetric


class HTTPRequestIntent(RequestIntent):
    metrics: tuple[HTTPMetric, ...] = Field(default_factory=tuple, max_length=2)

    @field_validator("metrics")
    @classmethod
    def validate_metrics(cls, value: tuple[HTTPMetric, ...]) -> tuple[HTTPMetric, ...]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate_metrics")
        return value

    @model_validator(mode="after")
    def validate_required_metrics(self):
        if self.action == "proceed" and not self.metrics:
            raise ValueError("missing_metrics")
        return self
