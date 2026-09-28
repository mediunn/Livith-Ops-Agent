import operator
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from ops_agent.tools.http import HTTPMetric

CONTRACT_VERSION = "http-agent-v1"
Window = Literal["previous", "first_half", "second_half"]
Text = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)
]


class HTTPHypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Literal["h1", "h2", "h3"]
    statement: Text
    status: Literal["unverified", "supported", "rejected", "insufficient_evidence"]
    evidence_ids: list[str] = Field(min_length=1, max_length=6)


class HTTPDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["query", "finish"]
    endpoint_id: str | None = None
    metric: HTTPMetric | None = None
    window: Window | None = None
    rationale: Text
    hypotheses: list[HTTPHypothesis] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def check_fields(self):
        values = (self.endpoint_id, self.metric, self.window)
        if self.action == "query" and any(value is None for value in values):
            raise ValueError("query requires endpoint_id, metric and window")
        if self.action == "finish" and any(value is not None for value in values):
            raise ValueError("finish requires null query fields")
        if len({h.id for h in self.hypotheses}) != len(self.hypotheses):
            raise ValueError("duplicate hypothesis ID")
        return self


class HTTPAgentState(TypedDict):
    contract_version: str
    thread_id: str
    directory: str
    request: dict
    symptom: str
    planner_kind: str
    model: str
    prompt: str
    prompt_sha256: str
    limits: dict
    remaining_seconds: float
    pending_query: dict | None
    evidence: Annotated[list[dict], operator.add]
    decisions: Annotated[list[dict], operator.add]
    stop_reason: str
    error_type: str | None
    report: dict | None
