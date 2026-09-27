from types import SimpleNamespace

import pytest

from ops_agent.persistence import session


@pytest.fixture(autouse=True)
def disable_live_tracing(monkeypatch):
    # 개발자의 .env에 키가 있더라도 테스트 입력은 외부로 보내지 않는다.
    monkeypatch.setenv("OPS_LANGFUSE_ENABLED", "false")


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "AGENT_ROOT", tmp_path)
    return session.initial_state(
        SimpleNamespace(
            model="qwen2.5:3b",
            symptom="test",
            seconds=600,
        ),
        "test-thread",
    )
