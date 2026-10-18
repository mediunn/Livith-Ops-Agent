"""LLM으로 질문을 지원되는 조사 분야에 연결한다. 관측 도구는 호출하지 않는다."""

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Literal
from uuid import uuid4

from ollama import AsyncClient
from pydantic import Field, field_validator, model_validator

from ops_agent.agent.budget import remaining_seconds, reserve
from ops_agent.agent.contracts import RequestIntent
from ops_agent.config import NUM_CTX, NUM_PREDICT, OLLAMA_HOST, ROUTING_PROMPT_PATH
from ops_agent.persistence.artifacts import save_json
from ops_agent.tools.http import HTTPMethod, HTTPQuery


class RoutedRequest(RequestIntent):
    domain: Literal["http", "external_api"] | None = None
    route: str | None = Field(default=None, min_length=1, max_length=300)
    method: HTTPMethod | None = None
    lookback_minutes: int | None = Field(default=None, ge=1, le=1440, strict=True)
    compare_previous: bool = Field(default=True, strict=True)

    @field_validator("route")
    @classmethod
    def validate_route(cls, value):
        return HTTPQuery.validate_route(value)

    @model_validator(mode="after")
    def validate_domain(self):
        if self.action == "proceed" and self.domain is None:
            raise ValueError("missing_domain")
        if self.domain == "external_api":
            if self.route is not None or self.method is not None:
                raise ValueError("external_api_has_no_http_filter")
            if self.lookback_minutes is not None and self.lookback_minutes > 360:
                raise ValueError("external_api_window_exceeds_six_hours")
        return self


def validate_route_scope(plan: RoutedRequest, question: str, explicit: dict) -> None:
    if plan.action != "proceed":
        return
    if explicit.get("domain") and plan.domain != explicit["domain"]:
        raise ValueError("explicit_domain_mismatch")
    if plan.domain == "external_api" and (
        explicit.get("route") or explicit.get("method")
    ):
        raise ValueError("http_options_for_external_api")
    if plan.domain == "http":
        if explicit.get("compare_previous") is not None:
            raise ValueError("compare_previous_option_is_external_api_only")
        if explicit.get("route") and plan.route not in {None, explicit["route"]}:
            raise ValueError("explicit_route_mismatch")
        if explicit.get("method") and plan.method not in {None, explicit["method"]}:
            raise ValueError("explicit_method_mismatch")
        if (
            plan.route
            and plan.route != explicit.get("route")
            and plan.route not in question
        ):
            raise ValueError("route_must_be_copied_from_input")
        if (
            plan.method
            and plan.method != explicit.get("method")
            and plan.method not in question.upper()
        ):
            raise ValueError("method_must_be_copied_from_input")
        if plan.target_text and not (explicit.get("route") or plan.route):
            raise ValueError("unresolved_http_target")
    if plan.time_text and not (explicit.get("start") or plan.lookback_minutes):
        raise ValueError("unresolved_time_expression")


async def route_request(state: dict, question: str, explicit: dict) -> RoutedRequest:
    prompt = ROUTING_PROMPT_PATH.read_text(encoding="utf-8").strip()
    schema = RoutedRequest.model_json_schema()
    schema["required"] = list(schema["properties"])
    messages = [
        {"role": "system", "content": prompt},
        {
            "role": "user",
            "content": json.dumps(
                {"question": question, "explicit": explicit}, ensure_ascii=False
            ),
        },
    ]
    for attempt in (1, 2):
        if (
            len(
                json.dumps(
                    {"messages": messages, "schema": schema}, ensure_ascii=False
                ).encode()
            )
            > 24000
        ):
            raise ValueError("routing_input_too_large")
        timeout = min(45.0, remaining_seconds(state))
        reserve(state, "llm")
        receipt = {
            "attempt": attempt,
            "model": state["model"],
            "prompt_version": "routing_v1",
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "messages": messages,
            "output_schema": schema,
            "response": None,
            "usage": None,
            "validation_error": None,
        }
        try:
            async with asyncio.timeout(timeout):
                response = await AsyncClient(host=OLLAMA_HOST, timeout=timeout).chat(
                    model=state["model"],
                    messages=messages,
                    format=schema,
                    stream=False,
                    options={
                        "temperature": 0,
                        "num_ctx": NUM_CTX,
                        "num_predict": NUM_PREDICT,
                    },
                )
            receipt["response"] = response.model_dump(mode="json")
            receipt["usage"] = {
                "input_tokens": response.prompt_eval_count,
                "output_tokens": response.eval_count,
            }
            if not response.done or response.done_reason == "length":
                raise RuntimeError("routing_output_truncated")
            content = response.message.content or ""
            try:
                plan = RoutedRequest.model_validate_json(content)
                validate_route_scope(plan, question, explicit)
                return plan
            except ValueError:
                receipt["validation_error"] = "invalid_route_or_scope"
        except BaseException as exc:
            receipt["error_type"] = type(exc).__name__
            raise
        finally:
            save_json(Path(state["directory"]) / f"routing-{uuid4().hex}.json", receipt)
        if attempt == 2:
            raise ValueError("invalid_route_or_scope")
        messages = [
            *messages,
            {"role": "assistant", "content": content},
            {
                "role": "user",
                "content": "스키마와 명시적 옵션에 맞게 수정하세요. 실제 route 라벨을 추측하지 마세요. 대상이나 시간을 확정할 수 없으면 clarify, 지원 밖이면 unsupported를 반환하세요. 새 근거는 없습니다.",
            },
        ]
    raise AssertionError("unreachable")
