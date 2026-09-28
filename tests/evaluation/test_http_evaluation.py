import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops_agent.agent.http import planner
from ops_agent.agent.http.graph import initial_state
from ops_agent.cli import evaluate_http_agent
from ops_agent.evaluation.http_agent import CASES, SCOPE, evaluate
from ops_agent.evaluation.http_metrics import call_metrics, classify
from ops_agent.persistence.artifacts import read_json, save_json
from ops_agent.tools.http import HTTPQuery


def test_repeated_models_rotate_and_rule_baseline_is_not_duplicated(tmp_path):
    root = tmp_path / "evaluation"
    summary = asyncio.run(
        evaluate(
            root,
            models=["first", "second"],
            planners=("rules", "llm"),
            repeats=2,
            seconds=300,
            llm_seconds=90,
            cases=[CASES[-1]],
        )
    )
    assert [(r["repeat"], r["planner"], r["model"]) for r in summary["results"]] == [
        (1, "rules", None),
        (1, "llm", "first"),
        (1, "llm", "second"),
        (2, "rules", None),
        (2, "llm", "second"),
        (2, "llm", "first"),
    ]
    assert len({r["report"] for r in summary["results"]}) == 6
    assert summary["status"] == "completed"
    assert read_json(root / "summary.json") == summary
    metadata = read_json(root / "metadata.json")
    assert metadata["limits"]["llm_seconds"] == 90
    assert metadata["source_hashes"]["prompts/agent/http_agent_v1.txt"]
    for row in summary["results"]:
        assert read_json(Path(row["report"]))["limits"]["llm_seconds"] == 90
        assert row["outcome"] == "no_data_handled"
        assert row["budget"]["llm_calls"] == 0
    assert all(g["comparison_runs"] == 0 for g in summary["aggregate"])


def test_interruption_preserves_completed_case_and_marks_partial_summary(tmp_path):
    root = tmp_path / "interrupted"

    def stop_after_result(event):
        if event["event"] == "result":
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            evaluate(root, cases=[CASES[-1], CASES[0]], on_progress=stop_after_result)
        )
    saved = read_json(root / "summary.json")
    assert saved["status"] == "interrupted_or_failed"
    assert saved["error_type"] == "CancelledError"
    assert len(saved["results"]) == 1
    assert Path(saved["results"][0]["report"]).is_file()


@pytest.mark.parametrize(
    "updates",
    [
        {"repeats": 0},
        {"repeats": 11},
        {"seconds": 0},
        {"llm_seconds": 0},
        {"llm_seconds": 181},
        {"models": []},
        {"models": ["duplicate", "duplicate"]},
        {"cases": []},
        {"planners": ["unknown"]},
    ],
)
def test_invalid_evaluation_does_not_create_artifacts(tmp_path, updates):
    root = tmp_path / "absent"
    with pytest.raises(ValueError):
        asyncio.run(evaluate(root, **updates))
    assert not root.exists()


def test_unknown_usage_is_not_reported_as_zero_consumption(tmp_path):
    save_json(
        tmp_path / "decision-a.json",
        {
            "attempt": 1,
            "started_at": 1,
            "elapsed_seconds": 90,
            "timeout_seconds": 90,
            "error_type": "TimeoutError",
            "response": None,
            "usage": None,
        },
    )
    metrics = call_metrics(tmp_path)
    assert metrics["calls_with_unknown_usage"] == 1
    assert metrics["calls"][0]["input_tokens"] is None
    assert metrics["calls"][0]["server_seconds"]["eval_duration"] is None
    assert (
        classify(
            {"stop_reason": "decision_error", "error_type": "TimeoutError"}, {}, metrics
        )
        == "timeout"
    )


def test_metrics_keep_server_timing_and_separate_validation_failure(tmp_path):
    save_json(
        tmp_path / "decision-b.json",
        {
            "attempt": 2,
            "started_at": 2,
            "elapsed_seconds": 1,
            "validation_error": "invalid",
            "usage": {"input_tokens": 10, "output_tokens": 3},
            "response": {"load_duration": 500_000_000, "eval_duration": 250_000_000},
        },
    )
    metrics = call_metrics(tmp_path)
    assert metrics["repair_calls"] == 1
    assert metrics["calls"][0]["server_seconds"]["load_duration"] == 0.5
    assert metrics["calls_with_unknown_usage"] == 0
    assert (
        classify(
            {"stop_reason": "decision_error", "error_type": "ValueError"}, {}, metrics
        )
        == "invalid_decision"
    )
    scored = {
        "checks": {"completed": True, "targeted_previous_query": False},
        "hypothesis_status_match": False,
    }
    assert (
        classify({"stop_reason": "planner_finished"}, scored, metrics)
        == "missing_comparison"
    )
    scored["checks"]["targeted_previous_query"] = True
    assert (
        classify({"stop_reason": "planner_finished"}, scored, metrics)
        == "hypothesis_check_failed"
    )


@pytest.mark.parametrize(
    "configured,remaining,expected", [(90, 120, 90), (90, 5, 5), (None, 120, 45)]
)
def test_planner_respects_saved_limit_and_remaining_budget(
    tmp_path, monkeypatch, configured, remaining, expected
):
    state = initial_state(tmp_path / "run", HTTPQuery(**SCOPE), symptom="test")
    if configured is None:
        del state["limits"][
            "llm_seconds"
        ]  # Older HTTP checkpoints retain the original limit.
    else:
        state["limits"]["llm_seconds"] = configured
    state["deadline"] = time.time() + remaining
    monkeypatch.setattr(planner, "remaining_seconds", lambda _: remaining)
    seen = []

    class Client:
        def __init__(self, *, host, timeout):
            seen.append(timeout)

        async def chat(self, **kwargs):
            content = json.dumps({"action": "finish", "rationale": "No more evidence"})
            return SimpleNamespace(
                done=True,
                done_reason="stop",
                prompt_eval_count=2,
                eval_count=3,
                message=SimpleNamespace(content=content),
                model_dump=lambda **_: {},
            )

    monkeypatch.setattr(planner, "AsyncClient", Client)
    result = asyncio.run(
        planner.llm_planner(state, {"endpoints": [], "observations": []})
    )
    assert result.action == "finish" and seen == [expected]
    receipt = read_json(next(Path(state["directory"]).glob("decision-*.json")))
    assert receipt["timeout_seconds"] == expected
    assert receipt["elapsed_seconds"] >= 0


def test_cli_forwards_comparison_settings_and_returns_failure(monkeypatch, capsys):
    async def fake_evaluate(root, **kwargs):
        assert kwargs["models"] == ["first", "second"]
        assert (
            kwargs["repeats"] == 2
            and kwargs["llm_seconds"] == 90
            and kwargs["seconds"] == 300
        )
        assert kwargs["warmup"] is True and kwargs["cases"] == [CASES[2]]
        return {"aggregate": [], "results": [{"checks": {"completed": False}}]}

    monkeypatch.setattr(evaluate_http_agent, "evaluate", fake_evaluate)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluate_http_agent",
            "--planner",
            "llm",
            "--models",
            "first",
            "second",
            "--repeat",
            "2",
            "--seconds",
            "300",
            "--llm-seconds",
            "90",
            "--warmup",
            "--case",
            "contradicted_latency_increase",
        ],
    )
    assert evaluate_http_agent.main() == 1
    assert "summary.json" in capsys.readouterr().out
