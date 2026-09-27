"""근거 요약을 읽고 다음 조회 또는 종료를 선택한다."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

from ollama import AsyncClient

from ops_agent.agent.budget import remaining_seconds, reserve
from ops_agent.agent.claims import CLAIM_POLICY_VERSION, verified_claims
from ops_agent.agent.decision_validation import (
    DecisionValidationError,
    allowed_evidence_ids,
    decision_schema,
    validate_decision,
)
from ops_agent.config import NUM_CTX, NUM_PREDICT
from ops_agent.persistence.artifacts import save_json
from ops_agent.tools.grafana import CATALOG


class ContextTooLarge(ValueError):
    pass


def build_context(state: dict, available: list[str]) -> dict:
    return {
        "claim_policy_version": CLAIM_POLICY_VERSION,
        "verified_claims": verified_claims(state),
        "symptom": state["symptom"],
        "window": state["window"],
        "request": state["request"],
        "allowed_hypothesis_evidence_ids": allowed_evidence_ids(state),
        "scope": {
            "service": state["request"]["service"],
            "environment": state["request"]["environment"],
            "investigation_type": state["request"]["investigation_type"],
            "environment_filter_applied": False,
            "metric_service_filter_applied": False,
            "log_bodies_included": False,
            "service_health_criteria_provided": False,
            "symptom_changes_query_scope": False,
        },
        "available_tools": {name: CATALOG[name] for name in available},
        "evidence": [
            {
                key: item[key]
                for key in (
                    "action",
                    "evidence_id",
                    "tool",
                    "arguments",
                    "measurement",
                    "status",
                    "summary",
                )
            }
            for item in state["evidence"]
        ],
        "previous_choices": [
            {"action": item["action"], "rationale": item["rationale"]}
            for item in state["decisions"]
        ],
        "remaining_seconds": round(remaining_seconds(state), 2),
    }


async def choose_action(state: dict, available: list[str], trace=None) -> dict:
    context = build_context(state, available)
    schema = decision_schema(state, available)
    messages = [
        {"role": "system", "content": state["prompt"]},
        {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
    ]
    decision_id = uuid4().hex
    repair_of = None
    for attempt in (1, 2):
        receipt = await generate_decision(
            state, context, schema, messages, decision_id, attempt, repair_of, trace
        )
        content = (receipt["response"].get("message") or {}).get("content") or ""
        validation = receipt["validation"]
        if validation["valid"]:
            return validate_decision(content, state, available)
        if attempt == 2 or validation["code"] == "output_truncated":
            raise DecisionValidationError(validation["code"])
        repair_of = receipt["attempt_id"]
        messages = [
            *messages,
            {"role": "assistant", "content": content},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "validation_feedback": validation,
                        "verified_claims": context["verified_claims"],
                        "allowed_actions": [*available, "finish"],
                        "allowed_hypothesis_evidence_ids": allowed_evidence_ids(state),
                        "instruction": (
                            "이전 응답의 검증 오류를 수정하여 완전한 Decision JSON을 반환하세요. "
                            "근거가 부족한 가설은 삭제하세요. 유효한 ID로 바꾸는 것만으로 "
                            "주장이 뒷받침되지는 않습니다. 새 관측 데이터는 추가되지 않았습니다."
                        ),
                    },
                    ensure_ascii=False,
                ),
            },
        ]
    raise AssertionError("도달할 수 없는 판단 시도입니다.")


async def generate_decision(
    state: dict,
    context: dict,
    schema: dict,
    messages: list[dict],
    decision_id: str,
    attempt: int,
    repair_of: str | None,
    trace=None,
) -> dict:
    input_bytes = len(
        json.dumps(
            {"messages": messages, "schema": schema},
            ensure_ascii=False,
        ).encode("utf-8")
    )
    # 수정 요청에도 같은 입력 크기·시간·호출·토큰 예약 한도를 적용한다.
    if input_bytes > NUM_CTX - 2048:
        raise ContextTooLarge("입력 근거가 현재 크기 제한을 초과했습니다.")
    remaining_seconds(state)
    reserve(state, "llm")
    options = {"temperature": 0, "num_ctx": NUM_CTX, "num_predict": NUM_PREDICT}
    receipt = {
        "claim_policy_version": CLAIM_POLICY_VERSION,
        "decision_id": decision_id,
        "attempt_id": uuid4().hex,
        "attempt": attempt,
        "repair_of": repair_of,
        "model": state["model"],
        "prompt_version": state.get("prompt_version", "ops_agent_v2"),
        "prompt_sha256": state["prompt_sha256"],
        "messages": messages,
        "output_schema": schema,
        "options": options,
        "response": None,
        "usage": None,
        "error_type": None,
        "validation": None,
    }
    if trace:
        trace.record.update(
            input=context,
            messages=messages,
            options=options,
            response=None,
            usage=None,
        )
        trace.start_generation(
            name="ollama-planner",
            metadata={
                "claim_policy_version": CLAIM_POLICY_VERSION,
                "decision_id": decision_id,
                "attempt_id": receipt["attempt_id"],
                "attempt": attempt,
                "repair_of": repair_of,
            },
        )
    try:
        timeout = min(45.0, remaining_seconds(state))
        async with asyncio.timeout(timeout):
            response = await AsyncClient(
                host="http://127.0.0.1:11434",
                timeout=timeout,
            ).chat(
                model=state["model"],
                messages=messages,
                format=schema,
                stream=False,
                options=options,
            )
        receipt["response"] = response.model_dump(mode="json")
        receipt["usage"] = {
            "input_tokens": response.prompt_eval_count,
            "output_tokens": response.eval_count,
        }
        if not response.done or response.done_reason == "length":
            raise DecisionValidationError("output_truncated")
        validate_decision(
            response.message.content or "", state, list(context["available_tools"])
        )
        receipt["validation"] = {"valid": True, "code": "ok"}
    except DecisionValidationError as exc:
        receipt["error_type"] = type(exc).__name__
        receipt["validation"] = exc.details()
    except BaseException as exc:
        receipt["error_type"] = type(exc).__name__
        raise
    finally:
        save_json(
            Path(state["directory"]) / f"decision-{receipt['attempt_id']}.json", receipt
        )
        if trace:
            trace.record.update(response=receipt["response"], usage=receipt["usage"])
            trace.end_generation(
                receipt["error_type"], validation=receipt["validation"]
            )
    return receipt
