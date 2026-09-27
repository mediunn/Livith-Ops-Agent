import pytest

from ops_agent.persistence.artifacts import query_artifact_path, query_key


def test_argument_order_does_not_change_query_identity(tmp_path):
    first = {"expr": "up", "filters": {"job": "api", "region": "kr"}}
    second = {"filters": {"region": "kr", "job": "api"}, "expr": "up"}
    assert query_key("query_prometheus", first) == query_key("query_prometheus", second)
    assert query_artifact_path(
        tmp_path, "query_prometheus", first
    ) == query_artifact_path(tmp_path, "query_prometheus", second)


@pytest.mark.parametrize(
    "field,value",
    [
        ("expr", 'up{job="another-service"}'),
        ("datasourceUid", "another-datasource"),
        ("startTime", "2025-01-01T01:00:00+00:00"),
        ("endTime", "2025-01-01T03:00:00+00:00"),
        ("stepSeconds", 120),
    ],
)
def test_query_scope_changes_identity(field, value):
    arguments = {
        "expr": "up",
        "datasourceUid": "metrics",
        "startTime": "2025-01-01T00:00:00+00:00",
        "endTime": "2025-01-01T02:00:00+00:00",
        "stepSeconds": 60,
    }
    assert query_key("query_prometheus", arguments) != query_key(
        "query_prometheus", {**arguments, field: value}
    )


def test_tool_and_thread_directory_separate_cache(tmp_path):
    assert query_key("tool-a", {}) != query_key("tool-b", {})
    assert query_artifact_path(tmp_path / "a", "tool", {}) != query_artifact_path(
        tmp_path / "b", "tool", {}
    )


def test_nonfinite_arguments_are_rejected():
    with pytest.raises(ValueError):
        query_key("tool", {"value": float("nan")})
