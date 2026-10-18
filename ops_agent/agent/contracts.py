"""조사 분야에 공통으로 사용하는 요청 해석과 사용자 답변 계약."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

MessageText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)
]


class AgentAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
    ]
    evidence_ids: list[MessageText] = Field(default_factory=list, max_length=12)
    limitations: list[MessageText] = Field(default_factory=list, max_length=5)


def validate_answer_references(answer: AgentAnswer, evidence_ids: set[str]) -> None:
    """참조 존재 여부만 검사한다. 주장을 뒷받침하는지는 별도로 평가한다."""
    if set(answer.evidence_ids) - evidence_ids:
        raise ValueError("unknown_answer_evidence")


class RequestIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["proceed", "clarify", "unsupported"]
    purpose: Literal["lookup", "compare", "investigate"] | None = None
    target_text: MessageText | None = None
    time_text: MessageText | None = None
    output_format: Literal["answer", "report"] = "answer"
    question: MessageText | None = None
    unsupported_reason: MessageText | None = None

    @model_validator(mode="after")
    def validate_action(self):
        if self.action == "proceed":
            if self.purpose is None:
                raise ValueError("missing_purpose")
            if self.question is not None or self.unsupported_reason is not None:
                raise ValueError("unexpected_message_for_proceed")
        elif self.action == "clarify":
            if self.question is None or self.unsupported_reason is not None:
                raise ValueError("invalid_clarification")
        elif self.unsupported_reason is None or self.question is not None:
            raise ValueError("invalid_unsupported_request")
        return self
