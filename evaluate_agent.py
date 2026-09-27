"""합성 수집 결과와 실제 planner로 전체 조사 루프를 평가한다. Grafana 호출은 없다."""

import argparse
import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from ops_agent.agent.budget import read_budget, remaining_seconds, reserve
from ops_agent.agent.graph import build_graph
from ops_agent.collectors.loki_parser import parse_loki_response
from ops_agent.collectors.prometheus_parser import parse_prometheus_response
from ops_agent.config import PROJECT_DIR
from ops_agent.persistence.artifacts import save_json
from ops_agent.persistence.session import initial_state
from ops_agent.telemetry.langfuse import ReportTrace, record_evaluation
from ops_agent.tools.grafana import measurement_for, query_spec

CASES_PATH = PROJECT_DIR / "evals/agent-cases.json"


def synthetic_observation(state: dict, case: dict, action: str) -> dict:
    tool, arguments = query_spec(state, action)
    if tool == "query_prometheus":
        rate = case["previous_rate" if action == "previous_metrics" else "current_rate"]
        raw = {
            "structuredContent": {
                "data": [
                    {
                        "metric": {"api": "synthetic-api"},
                        "values": [
                            [
                                datetime.fromisoformat(arguments[key]).timestamp(),
                                str(rate),
                            ]
                            for key in ("startTime", "endTime")
                        ],
                    }
                ]
            }
        }
        summary = parse_prometheus_response(raw)
    else:
        levels = case[
            "warning_query_levels" if action == "warning_logs" else "log_levels"
        ]
        timestamp = int(
            datetime.fromisoformat(arguments["startRfc3339"]).timestamp() * 1e9
        )
        entries = [
            {
                "timestamp": str(timestamp + index),
                "line": (
                    "Synthetic warn/error marker."
                    if level in case["warning_query_levels"]
                    else "Synthetic fixture."
                ),
                "labels": {"job": "livith-server"},
                "structuredMetadata": {"detected_level": level},
            }
            for level, count in levels.items()
            for index in range(count)
        ]
        raw = {
            "structuredContent": {
                "data": entries,
                "metadata": {"resultsTruncated": case["truncated"]},
            }
        }
        summary = parse_loki_response(raw, limit=100)
    return {
        "evidence_id": uuid4().hex,
        "action": action,
        "tool": tool,
        "arguments": arguments,
        "measurement": measurement_for(tool),
        "status": summary["status"],
        "summary": summary,
        "error_type": None,
        "source": "synthetic_fixture",
    }


def synthetic_collector(case: dict):
    async def collect(state, trace=None):
        remaining_seconds(state)
        reserve(state, "tool")
        item = synthetic_observation(state, case, state["action"])
        if trace:
            span = trace.start_step(state["action"], item["arguments"])
            trace.end_step(span, item)
        save_json(Path(state["directory"]) / f"{state['action']}.json", item)
        return item

    return collect


def evaluate_state(state: dict, expected: dict) -> dict:
    report = state.get("report") or {}
    actions = [item["action"] for item in state["evidence"]]
    decisions = state["decisions"]
    completed = report.get("status") == "completed"
    selected_ids = report.get("selected_claim_ids")
    selected_kinds = {
        c["kind"]
        for c in report.get("verified_claims", [])
        if selected_ids is None or c["claim_id"] in selected_ids
    }
    checks = {
        "completed": completed,
        "requested_comparison_and_logs": (
            {"current_metrics", "previous_metrics"}.issubset(actions)
            and bool({"logs", "warning_logs"} & set(actions))
        ),
        "selected_expected_claims": completed
        and selected_kinds == set(expected["claim_kinds"]),
        "hypotheses_policy": completed
        and (not expected["empty_hypotheses"] or not report["hypotheses"]),
        # 문장 경계 형식 검사다. 문법·의미의 완결성은 별도 검토한다.
        "rationale_boundary": bool(decisions)
        and all(
            d["rationale"][-1] in ".!?。"
            and "\n" not in d["rationale"]
            and "\r" not in d["rationale"]
            for d in decisions
        ),
        "no_repeated_tools": len(actions) == len(set(actions)),
    }
    if state.get("version", 3) >= 4:
        checks["required_checks_complete"] = completed and not report.get(
            "required_checks", {}
        ).get("missing", ["unknown"])
        checks["code_generated_narrative"] = (
            report.get("narrative_source") == "code_generated"
            and report.get("health_assessment") == "not_evaluated"
            and report.get("model_assessment") is None
        )
    return {
        "checks": checks,
        "automatic_pass": all(checks.values()),
        "semantic_review": "pending",
    }


async def run_case(
    case: dict, model: str, seconds: int, suite_id: str, repeat: int, dry_run=False
) -> dict:
    state = initial_state(
        SimpleNamespace(
            model=model,
            seconds=seconds,
            symptom=case["symptom"],
            start="2026-01-01T00:00:00+00:00",
            end="2026-01-01T00:02:00+00:00",
        ),
        f"eval-{uuid4().hex}",
    )
    directory = Path(state["directory"])
    if dry_run:
        for action in ("current_metrics", "previous_metrics", "logs", "warning_logs"):
            save_json(
                directory / f"{action}.json", synthetic_observation(state, case, action)
            )
        return {"case_id": case["id"], "directory": str(directory), "status": "dry_run"}
    record = {
        "run_id": state["thread_id"],
        "model": model,
        "prompt_version": state["prompt_version"],
        "prompt_sha256": state["prompt_sha256"],
        "context_version": f"agent-state-v{state['version']}",
        "status": "running",
        "report": None,
        "error": None,
    }
    trace = ReportTrace(
        record,
        {
            "evaluation_run_id": suite_id,
            "case_id": case["id"],
            "repeat": repeat,
            "synthetic": True,
            "grafana_requeried": False,
            "model_reexecuted": True,
            "evaluation_scope": "full graph with synthetic collector and real planner",
        },
        name="ops-agent-loop-eval",
    )
    started = time.monotonic()
    try:
        async with AsyncSqliteSaver.from_conn_string(
            str(directory / "evaluation.sqlite")
        ) as saver:
            graph = build_graph(
                saver, trace=trace, tool_executor=synthetic_collector(case)
            )
            state = await graph.ainvoke(
                state,
                {
                    "configurable": {"thread_id": state["thread_id"]},
                    "recursion_limit": 40,
                },
            )
        record.update(status=state["report"]["status"], report=state["report"])
        if state.get("error_type"):
            record["error"] = {"type": state["error_type"]}
        record["validation"] = evaluate_state(state, case["expected"])
        save_json(directory / "report.json", state["report"])
    except Exception as exc:  # noqa: BLE001
        record.update(
            status="failed",
            error={"type": type(exc).__name__},
            validation={
                "checks": {"completed": False},
                "automatic_pass": False,
                "semantic_review": "pending",
            },
        )
    finally:
        record["elapsed_seconds"] = round(time.monotonic() - started, 2)
        trace.finish()
    record_evaluation(record, record["validation"])
    save_json(directory / "evaluation.json", record)
    receipts = [json.loads(p.read_text()) for p in directory.glob("decision-*.json")]
    return {
        "case_id": case["id"],
        "repeat": repeat,
        "directory": str(directory),
        "status": record["status"],
        "evaluation": record["validation"],
        "actions": [item["action"] for item in state["evidence"]],
        "budget": read_budget(state),
        "elapsed_seconds": record["elapsed_seconds"],
        "usage": {
            key: sum((r.get("usage") or {}).get(key) or 0 for r in receipts)
            for key in ("input_tokens", "output_tokens")
        },
        "trace_url": record["telemetry"].get("trace_url"),
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case")
    parser.add_argument("--repeat", type=int, choices=range(1, 6), default=1)
    parser.add_argument("--seconds", type=int, default=180)
    parser.add_argument("--model", default="qwen2.5:3b")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if (
        not args.model.strip()
        or args.model.endswith("-cloud")
        or not 1 <= args.seconds <= 3600
    ):
        parser.error("로컬 모델 이름과 1~3600초 실행 예산이 필요합니다.")
    dataset = json.loads(CASES_PATH.read_text())
    cases = [c for c in dataset["cases"] if args.case in (None, c["id"])]
    if not cases:
        parser.error("해당 사례 ID가 없습니다.")
    suite_id = f"agent-{uuid4().hex}"
    directory = PROJECT_DIR / "artifacts/evaluations" / suite_id
    directory.mkdir(parents=True)
    save_json(directory / "dataset.json", dataset)
    result = {"dataset_version": dataset["version"], "synthetic": True, "results": []}
    print(f"평가 폴더: {directory}", flush=True)
    for repeat in range(1, args.repeat + 1):
        for case in cases:
            row = await run_case(
                case, args.model, args.seconds, suite_id, repeat, args.dry_run
            )
            result["results"].append(row)
            save_json(directory / "summary.json", result)
            print(
                json.dumps(
                    {
                        k: row[k]
                        for k in ("case_id", "status", "evaluation")
                        if k in row
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    return (
        0
        if args.dry_run
        or all(r["evaluation"]["automatic_pass"] for r in result["results"])
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
