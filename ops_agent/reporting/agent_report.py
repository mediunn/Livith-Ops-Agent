"""관측값은 파싱 결과에서 가져오고 모델 해석과 분리한다."""

import time

from ops_agent.agent.budget import read_budget
from ops_agent.agent.state import READABLE_STATUSES


def build_report(state: dict) -> dict:
    last = state["decisions"][-1] if state["decisions"] else None
    model_finished = state["stop_reason"] == "model_finished"
    final_decision = last if model_finished else None
    current = next(
        (item for item in state["evidence"] if item["action"] == "current_metrics"),
        None,
    )
    has_log_query = any(
        item["tool"] == "query_loki_logs" and item["status"] in READABLE_STATUSES
        for item in state["evidence"]
    )
    has_minimum_evidence = (
        current is not None and current["status"] == "data_available" and has_log_query
    )
    assessment = "insufficient_evidence"
    if model_finished and has_minimum_evidence and last:
        assessment = last["assessment"]
    limitations = [
        "외부 API 요청률과 제한된 로그 요약만 조사했다.",
        "서비스 전체 정상·장애 판정 기준과 SLO는 제공되지 않았다.",
        "환경 라벨 필터를 적용하지 않았다.",
        "외부 API 지표 쿼리에 서비스 라벨 필터를 적용하지 않았다.",
        "증상 문장에서 조사 대상·시간·유형을 자동 추출하지 않았다.",
        "로그 본문은 모델에 제공하지 않았다.",
        "가설과 자유 서술의 사실성은 자동 검증하지 않았다.",
    ]
    if final_decision:
        limitations.extend(final_decision["limitations"])
    if not has_minimum_evidence:
        limitations.append("최소 지표·로그 관측 범위가 부족하다.")
    if not model_finished:
        limitations.append(f"제한 또는 오류로 조사 종료: {state['stop_reason']}")
    return {
        "report_version": "agent-report-v2",
        "thread_id": state["thread_id"],
        "request": state["request"],
        "symptom": state["symptom"],
        "window": state["window"],
        "status": "completed" if model_finished else "incomplete",
        "stop_reason": state["stop_reason"],
        "assessment": assessment,
        "model_assessment": final_decision["assessment"] if final_decision else None,
        "facts_source": "parsed_observations",
        "facts": [
            {
                key: item[key]
                for key in (
                    "evidence_id",
                    "action",
                    "tool",
                    "arguments",
                    "measurement",
                    "status",
                    "summary",
                )
            }
            for item in state["evidence"]
            if item["status"] in READABLE_STATUSES
        ],
        "hypotheses": last["hypotheses"] if model_finished and last else [],
        "limitations": list(dict.fromkeys(limitations)),
        "next_checks": final_decision["next_checks"]
        if final_decision
        else ["실패 또는 예산 종료 원인을 확인한다."],
        "decisions": state["decisions"],
        # 보고서는 Langfuse로도 전송될 수 있으므로 로컬 경로를 넣지 않는다.
        "tool_history": [
            {
                key: item[key]
                for key in ("action", "evidence_id", "status", "error_type")
            }
            for item in state["evidence"]
        ],
        "budget": read_budget(state),
        "semantic_review": "pending",
        "completed_at": time.time(),
    }
