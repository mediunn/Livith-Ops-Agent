from pathlib import Path

from ops_agent.agent.budget import BudgetExceeded, read_budget, remaining_seconds
from ops_agent.agent.decision.planner import ContextTooLarge, choose_action
from ops_agent.agent.decision.validation import DecisionValidationError
from ops_agent.agent.log_samples import (
    SAMPLE_ACTION,
    sample_candidates,
    valid_sample_request,
)
from ops_agent.agent.policy import allowed_actions, coverage, warning_log_followup
from ops_agent.agent.state import READABLE_STATUSES, AgentState
from ops_agent.persistence.artifacts import query_key, save_json
from ops_agent.reporting.agent_report import build_report
from ops_agent.tools.grafana import CATALOG, execute_tool, query_already_collected
from ops_agent.tools.log_samples import get_log_samples


class AgentNodes:
    def __init__(self, trace=None, *, tool_executor=None):
        self.trace = trace
        self.tool_executor = tool_executor

    async def decide(self, state: AgentState) -> dict:
        try:
            remaining_seconds(state)
        except BudgetExceeded as exc:
            return {"stop_reason": str(exc)}
        if any(item["status"] not in READABLE_STATUSES for item in state["evidence"]):
            return {"stop_reason": "collection_error"}
        if not query_already_collected(state, "current_metrics"):
            return {"action": "current_metrics"}
        available = [
            name
            for name in CATALOG
            if name != "current_metrics" and not query_already_collected(state, name)
        ]
        if read_budget(state)["tool_calls"] >= state["limits"]["tool_calls"]:
            if state.get("version", 3) >= 4 and coverage(state)["missing"]:
                return {"stop_reason": "tool_budget"}
            available = []
        if sample_candidates(state):
            available.append(SAMPLE_ACTION)
        if state.get("version", 3) >= 4:
            available = [a for a in allowed_actions(state, available) if a != "finish"]
            if coverage(state)["missing"] and not available:
                return {"stop_reason": "required_checks_unavailable"}
        try:
            decision = await choose_action(state, available, trace=self.trace)
        except BudgetExceeded as exc:
            return {"stop_reason": str(exc)}
        except ContextTooLarge:
            return {"stop_reason": "context_limit"}
        except TimeoutError:
            return {"stop_reason": "model_timeout", "error_type": "TimeoutError"}
        except DecisionValidationError as exc:
            return {
                "stop_reason": "decision_error",
                "error_type": type(exc).__name__,
                "decision_error": exc.details(),
            }
        except Exception as exc:  # noqa: BLE001
            return {"stop_reason": "decision_error", "error_type": type(exc).__name__}
        if (
            state.get("version", 3) >= 4
            and decision["action"] == "finish"
            and coverage(state)["missing"]
        ):
            error = DecisionValidationError("required_checks_missing")
            return {
                "stop_reason": "decision_error",
                "error_type": type(error).__name__,
                "decision_error": error.details(),
            }
        return {
            "decision_error": None,
            "action": decision["action"],
            "log_sample_request": decision.get("log_sample_request"),
            "decisions": [*state["decisions"], decision],
            "stop_reason": "model_finished" if decision["action"] == "finish" else "",
        }

    async def query(self, state: AgentState) -> dict:
        action = state["action"]
        if action == SAMPLE_ACTION:
            return self.read_log_samples(state)
        if action not in CATALOG:
            return {"stop_reason": "duplicate_or_invalid_tool"}
        if (
            state.get("version", 3) >= 4
            and action == "warning_logs"
            and not warning_log_followup(state)["eligible"]
        ):
            return {"stop_reason": "optional_query_blocked"}
        try:
            if query_already_collected(state, action):
                return {"stop_reason": "duplicate_or_invalid_tool"}
            evidence = await (self.tool_executor or execute_tool)(
                state, trace=self.trace
            )
        except BudgetExceeded as exc:
            return {"stop_reason": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"stop_reason": "collection_error", "error_type": type(exc).__name__}
        return {"evidence": [*state["evidence"], evidence]}

    def read_log_samples(self, state: AgentState) -> dict:
        request = state.get("log_sample_request")
        # 재개 직후에도 모델 출력과 같은 제약을 적용한다.
        if coverage(state)["missing"] or not valid_sample_request(state, request):
            return {"stop_reason": "log_sample_request_blocked"}
        try:
            remaining_seconds(state)
            page = get_log_samples(state, **request)
            remaining_seconds(state)
            path = (
                Path(state["directory"])
                / "log-samples"
                / f"{query_key(SAMPLE_ACTION, request)}.json"
            )
            save_json(
                path,
                {"thread_id": state["thread_id"], "request": request, "page": page},
            )
        except BudgetExceeded as exc:
            return {"stop_reason": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"stop_reason": "log_sample_error", "error_type": type(exc).__name__}
        return {
            "log_sample_pages": [*state.get("log_sample_pages", []), page],
            "log_sample_request": None,
        }

    def report(self, state: AgentState) -> dict:
        return {"report": build_report(state)}
