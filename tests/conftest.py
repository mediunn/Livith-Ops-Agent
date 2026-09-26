import pytest


@pytest.fixture(autouse=True)
def disable_live_tracing(monkeypatch):
    # 개발자의 .env에 키가 있더라도 테스트 입력은 외부로 보내지 않는다.
    monkeypatch.setenv("OPS_LANGFUSE_ENABLED", "false")
