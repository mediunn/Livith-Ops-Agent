import json

import pytest

from ops_agent.persistence.artifacts import query_artifact_path, query_key, save_json
from ops_agent.tools.log_samples import get_log_samples


@pytest.fixture
def stored_logs(tmp_path):
    def create(lines, *, text_response=False, source_limit=100, truncated=False):
        arguments = {"limit": source_limit}
        evidence = {
            "evidence_id": "logs-1",
            "tool": "query_loki_logs",
            "arguments": arguments,
            "status": "data_available" if lines else "no_data",
        }
        payload = {
            "data": [
                {
                    "timestamp": str(1790425505095804000 + index),
                    "line": line,
                    "labels": {"job": "test", "token": "label-secret"},
                    "structuredMetadata": {"detected_level": "info"},
                }
                for index, line in enumerate(lines)
            ],
            "metadata": {"resultsTruncated": truncated},
        }
        response = (
            {"content": [{"type": "text", "text": json.dumps(payload)}]}
            if text_response
            else {"structuredContent": payload}
        )
        record = {
            **evidence,
            "thread_id": "thread-1",
            "query_key": query_key(evidence["tool"], arguments),
            "completed_at": 1,
            "response": response,
        }
        path = query_artifact_path(tmp_path, evidence["tool"], arguments)
        save_json(path, record)
        state = {
            "directory": str(tmp_path),
            "thread_id": "thread-1",
            "evidence": [evidence],
        }
        return state, record, path

    return create


@pytest.mark.parametrize("text_response", [False, True])
def test_pages_preserve_order_and_raw_artifact(stored_logs, text_response):
    state, _, path = stored_logs(
        ["first", "second", "third"], text_response=text_response
    )
    original = path.read_bytes()
    first = get_log_samples(state, "logs-1", limit=2)
    second = get_log_samples(state, "logs-1", cursor=first["next_cursor"], limit=2)

    assert [sample["line"] for sample in first["samples"]] == ["first", "second"]
    assert first["returned_count"] == 2
    assert first["total_stored_logs"] == 3
    assert first["has_more"] is True
    assert second["samples"][0]["index"] == 2
    assert second["samples"][0]["timestamp_ns"] == "1790425505095804002"
    assert second["samples"][0]["line"] == "third"
    assert second["next_cursor"] is None
    assert second["has_more"] is False
    assert "label-secret" not in json.dumps(first)
    assert path.read_bytes() == original


def test_masks_before_clipping_and_marks_changes(stored_logs):
    state, _, _ = stored_logs(
        ["x" * 600 + " password=secret", "user=a@example.com", "가" * 501]
    )
    samples = get_log_samples(state, "logs-1")["samples"]
    assert samples[0]["line"] == "[REDACTED: sensitive log line]"
    assert samples[0]["redacted"] is True
    assert samples[0]["line_truncated"] is False
    assert samples[1]["line"] == "user=[EMAIL]"
    assert samples[1]["redacted"] is True
    assert samples[2]["line"] == "가" * 500
    assert samples[2]["redacted"] is False
    assert samples[2]["line_truncated"] is True


@pytest.mark.parametrize("source_limit,truncated", [(1, False), (100, True)])
def test_last_page_does_not_imply_complete_source(stored_logs, source_limit, truncated):
    state, _, _ = stored_logs(["log"], source_limit=source_limit, truncated=truncated)
    result = get_log_samples(state, "logs-1")
    assert result["has_more"] is False
    assert result["scope"] == "stored_response_only"
    assert result["source_summary"]["possibly_truncated"] is True


@pytest.mark.parametrize("lines,cursor", [([], 0), (["log"], 1)])
def test_empty_page_at_end(stored_logs, lines, cursor):
    state, _, _ = stored_logs(lines)
    result = get_log_samples(state, "logs-1", cursor=cursor)
    assert result["samples"] == []
    assert result["next_cursor"] is None
    assert result["status"] == ("data_available" if lines else "no_data")


@pytest.mark.parametrize(
    "options",
    [
        {"cursor": -1},
        {"cursor": True},
        {"cursor": 2},
        {"limit": 0},
        {"limit": 11},
        {"limit": True},
    ],
)
def test_rejects_invalid_page(stored_logs, options):
    state, _, _ = stored_logs(["log"])
    with pytest.raises(ValueError):
        get_log_samples(state, "logs-1", **options)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "wrong_tool", "failed"])
def test_rejects_unreadable_evidence(stored_logs, mutation):
    state, _, _ = stored_logs(["log"])
    if mutation == "missing":
        state["evidence"] = []
    elif mutation == "duplicate":
        state["evidence"] *= 2
    elif mutation == "wrong_tool":
        state["evidence"][0]["tool"] = "query_prometheus"
    else:
        state["evidence"][0]["status"] = "collection_error"
    with pytest.raises(ValueError):
        get_log_samples(state, "logs-1")


@pytest.mark.parametrize(
    "field", ["thread_id", "evidence_id", "query_key", "arguments"]
)
def test_rejects_mismatched_record(stored_logs, field):
    state, record, path = stored_logs(["log"])
    record[field] = "different"
    save_json(path, record)
    with pytest.raises(ValueError, match="evidence_record_mismatch"):
        get_log_samples(state, "logs-1")


@pytest.mark.parametrize("mutation", ["pending", "error", "status", "malformed"])
def test_rejects_incomplete_or_invalid_response(stored_logs, mutation):
    state, record, path = stored_logs(["log"])
    if mutation == "pending":
        record["completed_at"] = None
    elif mutation == "error":
        record["response"]["isError"] = True
    elif mutation == "status":
        record["response"]["structuredContent"]["data"] = []
    else:
        record["response"]["structuredContent"]["data"][0]["line"] = None
    save_json(path, record)
    with pytest.raises((TypeError, ValueError)):
        get_log_samples(state, "logs-1")


def test_rejects_artifact_symlink_outside_investigation(stored_logs, tmp_path):
    state, _, path = stored_logs(["log"])
    outside = tmp_path / "outside.json"
    path.rename(outside)
    investigation = tmp_path / "investigation"
    linked = investigation / "queries" / path.name
    linked.parent.mkdir(parents=True)
    linked.symlink_to(outside)
    state["directory"] = str(investigation)
    with pytest.raises(ValueError, match="artifact_outside_investigation"):
        get_log_samples(state, "logs-1")
