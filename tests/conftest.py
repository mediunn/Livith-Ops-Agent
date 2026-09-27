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
    result = session.initial_state(
        SimpleNamespace(
            model="qwen2.5:3b",
            symptom="test",
            seconds=600,
        ),
        "test-thread",
    )

    # v3 자유 문장·참조 정책의 역사적 회귀 검사를 유지한다.
    result["version"] = 3
    result["request"]["compare_previous"] = False
    return result


@pytest.fixture
def current_state(state):
    state["version"] = 4
    state["request"]["compare_previous"] = True
    return state
