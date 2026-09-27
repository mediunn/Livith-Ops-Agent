from ops_agent.agent.budget import BudgetExceeded, read_budget, remaining_seconds
from ops_agent.agent.decision_validation import DecisionValidationError
from ops_agent.agent.planner import ContextTooLarge, choose_action
from ops_agent.agent.state import READABLE_STATUSES, AgentState
from ops_agent.reporting.agent_report import build_report
from ops_agent.tools.grafana import CATALOG, execute_tool


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
        if not state["evidence"]:
            return {"action": "current_metrics"}
        used = {item["action"] for item in state["evidence"]}
        available = [
            name for name in CATALOG if name not in used and name != "current_metrics"
        ]
        if read_budget(state)["tool_calls"] >= state["limits"]["tool_calls"]:
            available = []
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
        return {
            "decision_error": None,
            "action": decision["action"],
            "decisions": [*state["decisions"], decision],
            "stop_reason": "model_finished" if decision["action"] == "finish" else "",
        }

    async def query(self, state: AgentState) -> dict:
        action = state["action"]
        if action not in CATALOG or action in {
            item["action"] for item in state["evidence"]
        }:
            return {"stop_reason": "duplicate_or_invalid_tool"}
        try:
            evidence = await (self.tool_executor or execute_tool)(
                state, trace=self.trace
            )
        except BudgetExceeded as exc:
            return {"stop_reason": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"stop_reason": "collection_error", "error_type": type(exc).__name__}
        return {"evidence": [*state["evidence"], evidence]}

    def report(self, state: AgentState) -> dict:
        return {"report": build_report(state)}
