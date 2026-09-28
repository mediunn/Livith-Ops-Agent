"""응답 실패와 조사 품질 검사를 분리하고, 미수신 사용량을 보존한다."""

from collections import Counter
from pathlib import Path
from statistics import median

from ops_agent.persistence.artifacts import read_json


def call_metrics(directory: Path) -> dict:
    calls = []
    for path in sorted(directory.glob("decision-*.json")):
        receipt = read_json(path)
        response = receipt.get("response") or {}
        calls.append(
            {
                "receipt": str(path),
                "attempt": receipt["attempt"],
                "started_at": receipt.get("started_at"),
                "elapsed_seconds": receipt.get("elapsed_seconds"),
                "timeout_seconds": receipt.get("timeout_seconds"),
                "error_type": receipt.get("error_type"),
                "validation_error": receipt.get("validation_error"),
                "done_reason": response.get("done_reason"),
                "input_tokens": (receipt.get("usage") or {}).get("input_tokens"),
                "output_tokens": (receipt.get("usage") or {}).get("output_tokens"),
                "server_seconds": {
                    name: response.get(name) / 1e9
                    if response.get(name) is not None
                    else None
                    for name in (
                        "total_duration",
                        "load_duration",
                        "prompt_eval_duration",
                        "eval_duration",
                    )
                },
            }
        )
    calls.sort(key=lambda c: c["started_at"] or 0)
    return {
        "calls": calls,
        "known_input_tokens": sum(c["input_tokens"] or 0 for c in calls),
        "known_output_tokens": sum(c["output_tokens"] or 0 for c in calls),
        "calls_with_unknown_usage": sum(
            c["input_tokens"] is None or c["output_tokens"] is None for c in calls
        ),
        "repair_calls": sum(c["attempt"] == 2 for c in calls),
    }


def classify(report: dict, scored: dict, metrics: dict) -> str:
    reason, error = report["stop_reason"], report.get("error_type")
    if reason == "time_budget" or error in {
        "TimeoutError",
        "ReadTimeout",
        "ConnectTimeout",
    }:
        return "timeout"
    if reason.endswith("_budget"):
        return "budget_exhausted"
    if reason == "collection_error":
        return "collection_error"
    if reason == "decision_error":
        last = metrics["calls"][-1] if metrics["calls"] else {}
        if last.get("done_reason") == "length":
            return "truncated_output"
        if last.get("validation_error"):
            return "invalid_decision"
        return "decision_error"
    if "no_unnecessary_followup" in scored["checks"]:
        return (
            "no_data_handled" if all(scored["checks"].values()) else "no_data_failure"
        )
    if not scored["checks"]["targeted_previous_query"]:
        return "missing_comparison"
    if not all(scored["checks"].values()):
        return "check_failed"
    if not scored["hypothesis_status_match"]:
        return "hypothesis_check_failed"
    return "automatic_checks_passed"  # 가설 문장의 의미 정확성은 별도 검토.


def aggregate(results: list[dict]) -> list[dict]:
    groups = {}
    for row in results:
        groups.setdefault((row["planner"], row["model"]), []).append(row)
    output = []
    for (planner, model), rows in groups.items():
        comparable = [r for r in rows if "targeted_previous_query" in r["checks"]]
        output.append(
            {
                "planner": planner,
                "model": model,
                "runs": len(rows),
                "comparison_runs": len(comparable),
                "completed_comparisons": sum(
                    r["checks"]["completed"] for r in comparable
                ),
                "targeted_previous_queries": sum(
                    r["checks"]["targeted_previous_query"] for r in comparable
                ),
                "hypothesis_status_matches": sum(
                    bool(r["hypothesis_status_match"]) for r in comparable
                ),
                "outcomes": dict(Counter(r["outcome"] for r in rows)),
                "comparison_wall_seconds": {
                    "min": min(r["wall_seconds"] for r in comparable),
                    "median": median(r["wall_seconds"] for r in comparable),
                    "max": max(r["wall_seconds"] for r in comparable),
                }
                if comparable
                else None,
                "tool_calls": sum(r["budget"]["tool_calls"] for r in rows),
                "llm_calls": sum(r["budget"]["llm_calls"] for r in rows),
                "known_input_tokens": sum(
                    r["metrics"]["known_input_tokens"] for r in rows
                ),
                "known_output_tokens": sum(
                    r["metrics"]["known_output_tokens"] for r in rows
                ),
                "calls_with_unknown_usage": sum(
                    r["metrics"]["calls_with_unknown_usage"] for r in rows
                ),
                "semantic_review": "pending",
            }
        )
    return output
