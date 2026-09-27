from typing import Annotated, Literal, NotRequired, TypedDict

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

READABLE_STATUSES = {"data_available", "no_data", "invalid_data"}
Text = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
]


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1, max_length=500)
    evidence_ids: list[Text] = Field(min_length=1, max_length=4)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "previous_metrics",
        "logs",
        "warning_logs",
        "finish",
    ]
    rationale: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)
    ]
    assessment: Literal[
        "insufficient_evidence",
        "needs_investigation",
    ]
    hypotheses: list[Hypothesis] = Field(max_length=3)
    limitations: list[Text] = Field(min_length=1, max_length=5)
    next_checks: list[Text] = Field(min_length=1, max_length=5)


class AgentState(TypedDict):
    version: int
    thread_id: str
    model: str
    request: dict
    symptom: str
    directory: str
    window: dict
    deadline: float
    limits: dict
    prompt: str
    prompt_version: NotRequired[str]
    prompt_sha256: str
    action: str
    evidence: list[dict]
    decisions: list[dict]
    stop_reason: str
    error_type: str | None
    decision_error: NotRequired[dict | None]
    report: dict | None
