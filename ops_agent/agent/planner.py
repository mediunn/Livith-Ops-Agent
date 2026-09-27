"""근거 요약을 읽고 다음 조회 또는 종료를 선택한다."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

from ollama import AsyncClient

from ops_agent.agent.budget import remaining_seconds, reserve
from ops_agent.agent.state import Decision
from ops_agent.config import NUM_CTX, NUM_PREDICT, PROMPT_VERSION
from ops_agent.persistence.artifacts import save_json
from ops_agent.tools.grafana import CATALOG


class ContextTooLarge(ValueError):
    pass


def build_context(state: dict, available: list[str]) -> dict:
    return {
        "symptom": state["symptom"],
        "window": state["window"],
        "scope": {
            "service": "livith-server",
            "environment_filter_applied": False,
            "log_bodies_included": False,
            "service_health_criteria_provided": False,
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


def validate_decision(content: str, state: dict, available: list[str]) -> dict:
    decision = Decision.model_validate_json(content)
    if decision.action != "finish" and decision.action not in available:
        raise ValueError("허용되지 않거나 중복된 도구 선택입니다.")
    allowed_ids = {
        item["evidence_id"]
        for item in state["evidence"]
        if item["status"] == "data_available"
    }
    if any(set(item.evidence_ids) - allowed_ids for item in decision.hypotheses):
        raise ValueError("가설에 유효하지 않은 근거 ID가 있습니다.")
    return decision.model_dump(mode="json")


async def choose_action(state: dict, available: list[str], trace=None) -> dict:
    context = build_context(state, available)
    schema = Decision.model_json_schema()
    # 텍스트 지시뿐 아니라 디코딩 스키마에서도 사용한 도구를 제외한다.
    schema["properties"]["action"]["enum"] = [*available, "finish"]
    messages = [
        {"role": "system", "content": state["prompt"]},
        {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
    ]
    input_bytes = len(
        json.dumps(
            {"messages": messages, "schema": schema},
            ensure_ascii=False,
        ).encode("utf-8")
    )
    # 토크나이저가 아닌 보수적 크기 제한. 조용히 근거를 잘라 넣지 않는다.
    if input_bytes > NUM_CTX - 2048:
        raise ContextTooLarge("입력 근거가 현재 크기 제한을 초과했습니다.")
    reserve(state, "llm")
    options = {"temperature": 0, "num_ctx": NUM_CTX, "num_predict": NUM_PREDICT}
    receipt = {
        "model": state["model"],
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": state["prompt_sha256"],
        "messages": messages,
        "output_schema": schema,
        "options": options,
        "response": None,
        "usage": None,
        "error_type": None,
    }
    if trace:
        trace.record.update(
            input=context,
            messages=messages,
            options=options,
            response=None,
            usage=None,
        )
        trace.start_generation(name="ollama-planner")
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
            raise ValueError("모델 출력이 완료되기 전에 잘렸습니다.")
        return validate_decision(response.message.content or "", state, available)
    except BaseException as exc:
        receipt["error_type"] = type(exc).__name__
        raise
    finally:
        save_json(Path(state["directory"]) / f"decision-{uuid4().hex}.json", receipt)
        if trace:
            trace.record.update(response=receipt["response"], usage=receipt["usage"])
            trace.end_generation(receipt["error_type"])
