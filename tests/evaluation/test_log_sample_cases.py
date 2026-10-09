import asyncio
import json
from collections import Counter
from copy import deepcopy
from pathlib import Path

import pytest

from ops_agent.agent.claims import verified_claims
from ops_agent.evaluation.agent import (
    LOG_SAMPLE_CASES_PATH,
    run_case,
    save_synthetic_observation,
    synthetic_observation,
)
from ops_agent.evaluation.log_samples import evaluate_log_samples
from ops_agent.persistence.artifacts import query_artifact_path, read_json
from ops_agent.tools.log_samples import get_log_samples
from tests.helpers.agent import fake_model

CASES = read_json(LOG_SAMPLE_CASES_PATH)["cases"]
PLANS = {
    "error_body_required": [(0, 5)],
    "first_page_sufficient": [(0, 5)],
    "next_page_needed": [(0, 5), (5, 5)],
    "no_logs": [],
    "redacted_only": [(0, 5)],
}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_explicit_bodies_use_real_parsers_and_stored_order(state, case):
    item = synthetic_observation(state, case, "logs", include_response=True)
    data = item["response"]["structuredContent"]["data"]
    assert [entry["line"] for entry in data] == [
        entry["line"] for entry in case["log_entries"]
    ]
    assert item["summary"]["log_count"] == len(case["log_entries"])
    assert item["summary"]["level_counts"] == dict(
        Counter(e["level"] for e in case["log_entries"])
    )
    assert len(data) <= item["arguments"]["limit"]
    timestamps = [int(e["timestamp"]) for e in data]
    assert timestamps == sorted(set(timestamps), reverse=True)
    state["evidence"] = [item]
    assert {c["kind"] for c in verified_claims(state)} == set(
        case["expected"]["claim_kinds"]
    )


def test_paired_cases_have_identical_summary_but_different_target_position(state):
    first = next(c for c in CASES if c["id"] == "first_page_sufficient")
    second = next(c for c in CASES if c["id"] == "next_page_needed")
    assert first["symptom"] == second["symptom"]
    assert (
        synthetic_observation(state, first, "logs")["summary"]
        == synthetic_observation(state, second, "logs")["summary"]
    )
    target = "event=external_api_finished request_id=req_demo_42"
    assert target in first["log_entries"][0]["line"]
    assert all(target not in row["line"] for row in second["log_entries"][:5])
    assert target in second["log_entries"][5]["line"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_reference_actions_pass_full_graph_without_leaking_expectations(
    state, case, monkeypatch
):
    def choice(action, selection=None):
        context = json.loads(calls[-1]["messages"][1]["content"])
        result = {
            "action": action,
            "claim_ids": [c["claim_id"] for c in context["verified_claims"]],
        }
        if selection is not None:
            source = next(e for e in context["evidence"] if e["action"] == "logs")
            result["log_sample_request"] = {
                "evidence_id": source["evidence_id"],
                "cursor": selection[0],
                "limit": selection[1],
            }
        return result

    outputs = [lambda: choice("previous_metrics"), lambda: choice("logs")]
    outputs.extend(
        lambda selection=selection: choice("get_log_samples", selection)
        for selection in PLANS[case["id"]]
    )
    outputs.append(lambda: choice("finish"))
    calls = fake_model(monkeypatch, state, outputs)
    row = asyncio.run(run_case(case, state["model"], 60, "log-sample-test", 1))
    assert row["evaluation"]["automatic_pass"], row["evaluation"]
    assert row["evaluation"]["semantic_review"] == "pending"
    assert row["budget"]["tool_calls"] == 3
    assert row["log_sample_reads"]["pages_read"] == len(PLANS[case["id"]])
    contexts = [json.loads(c["messages"][1]["content"]) for c in calls]
    for context in contexts:
        assert "expected" not in context
        assert "description" not in context
        assert "semantic_review_notes" not in json.dumps(context)
        assert "log_entries" not in context
    assert all(context["log_sample_page"] is None for context in contexts[:3])
    if case["id"] == "redacted_only":
        assert "synthetic-not-a-real" not in json.dumps(calls)
        assert "[REDACTED: sensitive log line]" in json.dumps(calls)
    if case["id"] == "next_page_needed":
        assert "event=external_api_finished" not in json.dumps(contexts[3])
        assert "event=external_api_finished" in json.dumps(contexts[4])


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_dry_run_saves_sampleable_raw_responses_without_model_or_trace(
    state, case, monkeypatch
):
    from ops_agent.evaluation import agent

    def forbidden(*args, **kwargs):
        pytest.fail("A fixture-only run attempted live work")

    monkeypatch.setattr(agent, "ReportTrace", forbidden)
    monkeypatch.setattr(agent, "build_graph", forbidden)
    row = asyncio.run(
        run_case(case, state["model"], 60, "fixture-only", 1, dry_run=True)
    )
    directory = Path(row["directory"])
    evidence = read_json(directory / "logs.json")
    raw = read_json(
        query_artifact_path(directory, evidence["tool"], evidence["arguments"])
    )
    assert "response" not in evidence
    fixture_state = {
        "directory": str(directory),
        "thread_id": raw["thread_id"],
        "evidence": [evidence],
    }
    page = get_log_samples(fixture_state, evidence["evidence_id"])
    assert page["returned_count"] == min(5, len(case["log_entries"]))
    assert read_json(directory / "budget.json") == {
        "tool_calls": 0,
        "llm_calls": 0,
        "reserved_tokens": 0,
    }


@pytest.mark.parametrize(
    "mutation", ["skipped", "unreviewed", "duplicate", "wrong_source", "extra_page"]
)
def test_checks_reject_missing_review_wrong_source_and_overreading(state, mutation):
    case = next(c for c in CASES if c["id"] == "first_page_sufficient")
    item = save_synthetic_observation(state, case, "logs")
    state["evidence"] = [item]
    page = get_log_samples(state, item["evidence_id"])
    reference = {"evidence_id": page["evidence_id"], "cursor": 0}
    state["log_sample_pages"] = [page]
    state["decisions"] = [{"log_sample_page_seen": reference}]
    assert all(evaluate_log_samples(state, case["expected"]["log_samples"]).values())
    if mutation == "skipped":
        state["log_sample_pages"] = []
    elif mutation == "unreviewed":
        state["decisions"] = []
    elif mutation == "duplicate":
        state["log_sample_pages"].append(deepcopy(page))
    elif mutation == "wrong_source":
        item["action"] = "warning_logs"
    else:
        state["log_sample_pages"].append(
            get_log_samples(state, item["evidence_id"], cursor=5)
        )
        state["decisions"].append({"log_sample_page_seen": {**reference, "cursor": 5}})
    checks = evaluate_log_samples(state, case["expected"]["log_samples"])
    assert not all(checks.values())
    if mutation in {"skipped", "unreviewed", "wrong_source"}:
        assert not checks["required_sample_lines_reviewed"]
    elif mutation == "duplicate":
        assert not checks["no_repeated_sample_lines"]
    else:
        assert not checks["sample_page_count"]


def test_cli_dry_run_selects_new_suite(tmp_path, monkeypatch):
    from ops_agent.evaluation import agent
    from ops_agent.persistence import session

    monkeypatch.setattr(agent, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(session, "AGENT_ROOT", tmp_path / "agent")
    monkeypatch.setattr(
        "sys.argv", ["evaluate_agent", "--suite", "log-samples", "--dry-run"]
    )
    assert asyncio.run(agent.main()) == 0
    result = read_json(
        next((tmp_path / "artifacts/evaluations").glob("*/summary.json"))
    )
    assert result["dataset_version"] == "log-samples-v1"
    assert {row["case_id"] for row in result["results"]} == set(PLANS)
    assert all(row["status"] == "dry_run" for row in result["results"])
