from types import SimpleNamespace

import pytest

from ops_agent.cli import check_langfuse as check


@pytest.mark.parametrize("arrival,exit_code", [(1, 0), (3, 0), (None, 1)])
def test_check_requires_server_receipt(monkeypatch, arrival, exit_code):
    calls = []

    def fetch(**kwargs):
        calls.append(kwargs)
        records = (
            [SimpleNamespace(trace_id="trace-test")] if len(calls) == arrival else []
        )
        return SimpleNamespace(data=records)

    span = SimpleNamespace(
        trace_id="trace-test", update=lambda **kwargs: None, end=lambda: None
    )
    client = SimpleNamespace(
        auth_check=lambda: True,
        start_observation=lambda **kwargs: span,
        flush=lambda: None,  # flush 자체는 실패를 알려주지 않을 수 있다.
        get_trace_url=lambda **kwargs: "https://example.test/trace/trace-test",
        api=SimpleNamespace(observations=SimpleNamespace(get_many=fetch)),
    )
    monkeypatch.setattr(check, "configured_client", lambda: client)
    monkeypatch.setattr(check, "sleep", lambda seconds: None)
    assert check.main() == exit_code
    assert len(calls) == (arrival or 6)
    assert all(call["trace_id"] == "trace-test" for call in calls)
