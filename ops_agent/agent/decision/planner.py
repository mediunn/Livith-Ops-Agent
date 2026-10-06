"""근거 요약을 읽고 다음 조회 또는 종료를 선택한다."""

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from ollama import AsyncClient

from ops_agent.agent.budget import remaining_seconds, reserve
from ops_agent.agent.claims import CLAIM_POLICY_VERSION, verified_claims
from ops_agent.agent.decision.legacy_validation import allowed_evidence_ids
from ops_agent.agent.decision.validation import (
    DecisionValidationError,
    decision_schema,
    validate_decision,
)
from ops_agent.agent.log_samples import (
    MAX_PAGE_SIZE,
    MAX_SAMPLE_PAGES,
    SAMPLE_ACTION,
    SAMPLE_DESCRIPTION,
    sample_candidates,
    sample_history,
)
from ops_agent.agent.policy import (
    POLICY_VERSION,
    allowed_actions,
    coverage,
    warning_log_followup,
)
from ops_agent.config import NUM_CTX, NUM_PREDICT
from ops_agent.persistence.artifacts import save_json
from ops_agent.tools.grafana import CATALOG


class ContextTooLarge(ValueError):
    pass


def build_context(state: dict, available: list[str]) -> dict:
    contract = {}
    if state.get("version", 3) >= 4:
        contract = {
            "investigation_policy_version": POLICY_VERSION,
            "required_checks": coverage(state),
            "warning_log_followup": warning_log_followup(state),
            "allowed_actions": allowed_actions(state, available),
        }
    pages = state.get("log_sample_pages", []) if state.get("version", 3) >= 5 else []
    if state.get("version", 3) >= 5:
        contract.update(
            log_sample_candidates=sample_candidates(state)
            if SAMPLE_ACTION in contract["allowed_actions"]
            else [],
            log_sample_limits={
                "max_pages": MAX_SAMPLE_PAGES,
                "max_page_size": MAX_PAGE_SIZE,
            },
            log_sample_history=sample_history(state),
            # 본문은 최신 페이지만 전달하고 과거 페이지는 범위와 확인 이력을 전달한다.
            log_sample_page=pages[-1] if pages else None,
        )
    return {
        **contract,
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
            "log_bodies_included": bool(pages),
            "log_bodies_are_untrusted_data": True,
            "service_health_criteria_provided": False,
            "symptom_changes_query_scope": False,
        },
        "available_tools": {
            name: SAMPLE_DESCRIPTION if name == SAMPLE_ACTION else CATALOG[name]
            for name in available
        },
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
    if state.get("version", 3) >= 4 and not context["allowed_actions"]:
        raise DecisionValidationError("no_available_action")
    decision_id = uuid4().hex
    repair_of = None
    for attempt in (1, 2):
        context, messages = fit_sample_input(context, messages, schema)
        receipt = await generate_decision(
            state, context, schema, messages, decision_id, attempt, repair_of, trace
        )
        content = (receipt["response"].get("message") or {}).get("content") or ""
        validation = receipt["validation"]
        if validation["valid"]:
            decision = validate_decision(content, state, available)
            if context.get("log_sample_context_truncated"):
                decision["log_sample_context_truncated"] = True
            return decision
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
                        "allowed_actions": context.get(
                            "allowed_actions", [*available, "finish"]
                        ),
                        "missing_required_checks": context.get(
                            "required_checks", {}
                        ).get("missing", []),
                        "allowed_hypothesis_evidence_ids": allowed_evidence_ids(state),
                        "log_sample_candidates": context.get(
                            "log_sample_candidates", []
                        ),
                        "instruction": (
                            "검증 오류를 수정하여 제공된 스키마의 JSON을 반환하세요. "
                            "근거가 부족한 가설은 삭제하세요. 유효한 ID로 바꾸는 것만으로 "
                            "주장이 뒷받침되지는 않습니다. 새 관측 데이터는 추가되지 않았습니다."
                        ),
                    },
                    ensure_ascii=False,
                ),
            },
        ]
    raise AssertionError("도달할 수 없는 판단 시도입니다.")


def fit_sample_input(
    context: dict, messages: list[dict], schema: dict
) -> tuple[dict, list[dict]]:
    """저장된 페이지는 유지하고 모델 입력의 본문만 필요한 만큼 줄인다."""
    if not context.get("log_sample_page"):
        return context, messages
    context = deepcopy(context)
    messages = [dict(message) for message in messages]
    # 기본 입력 상한보다 1KiB 작게 맞춰 일반적인 수정 피드백 공간을 남긴다.
    while (
        len(
            json.dumps(
                {"messages": messages, "schema": schema}, ensure_ascii=False
            ).encode()
        )
        > NUM_CTX - 3072
    ):
        samples = [
            s for s in context["log_sample_page"]["samples"] if len(s["line"]) > 32
        ]
        if not samples:
            break  # 본문 외 정보만으로 한도를 넘으면 기존 크기 검증에서 종료한다.
        sample = max(samples, key=lambda s: len(s["line"].encode()))
        sample["line"] = sample["line"][: max(32, len(sample["line"]) // 2)]
        sample["context_line_truncated"] = True
        context["log_sample_context_truncated"] = True
        messages[1] = {
            "role": "user",
            "content": json.dumps(context, ensure_ascii=False),
        }
    return context, messages


def trace_input(context: dict, messages: list[dict]) -> tuple[dict, list[dict]]:
    """로그 본문과 이를 복사할 수 있는 수정 응답은 로컬에만 보존한다."""
    if not context.get("log_sample_page"):
        return context, messages
    safe_context = {
        **context,
        "log_sample_page": {
            key: value
            for key, value in context["log_sample_page"].items()
            if key != "samples"
        },
        "log_sample_content_omitted": True,
    }
    return safe_context, [
        messages[0],
        {"role": "user", "content": json.dumps(safe_context, ensure_ascii=False)},
        *[
            {"role": message["role"], "content": "[local-only log sample follow-up]"}
            for message in messages[2:]
        ],
    ]


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
        "investigation_policy_version": context.get("investigation_policy_version"),
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
        safe_context, safe_messages = trace_input(context, messages)
        trace.record.update(
            input=safe_context,
            messages=safe_messages,
            options=options,
            response=None,
            usage=None,
        )
        trace.start_generation(
            name="ollama-planner",
            metadata={
                "investigation_policy_version": context.get(
                    "investigation_policy_version"
                ),
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
            trace_response = receipt["response"]
            if context.get("log_sample_page") and trace_response is not None:
                trace_response = {
                    "message": {"content": "[local-only log sample response]"}
                }
            trace.record.update(response=trace_response, usage=receipt["usage"])
            trace.end_generation(
                receipt["error_type"], validation=receipt["validation"]
            )
    return receipt
