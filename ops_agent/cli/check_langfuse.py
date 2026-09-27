"""프로젝트 인증과 합성 trace 전송을 확인한다. 실제 운영 데이터는 사용하지 않는다."""

from datetime import UTC, datetime, timedelta
from time import sleep

from ops_agent.telemetry.langfuse import configured_client


def confirm_receipt(client, trace_id: str, started_at: datetime) -> bool:
    # 인덱싱 지연을 허용하되 끝없이 기다리지 않는다. trace ID로 이번 실행만 조회.
    for delay in (0, 1, 2, 4, 8, 10):
        if delay:
            sleep(delay)
        response = client.api.observations.get_many(
            trace_id=trace_id,
            from_start_time=started_at - timedelta(minutes=1),
            to_start_time=datetime.now(UTC) + timedelta(minutes=1),
            fields="core",
            limit=10,
            request_options={"max_retries": 0, "timeout_in_seconds": 5},
        )
        if any(item.trace_id == trace_id for item in response.data):
            return True
    return False


def main() -> int:
    try:
        client = configured_client()
        if client is None:
            print(".env에 OPS_LANGFUSE_ENABLED=true와 Langfuse 설정 3개를 입력하세요.")
            return 1
        if not client.auth_check():
            print("인증 실패: 프로젝트 키와 해당 프로젝트의 서버 주소를 확인하세요.")
            return 1
        print("프로젝트 인증 성공")
        started_at = datetime.now(UTC)
        span = client.start_observation(
            name="ops-agent-connection-check", input={"synthetic": True}
        )
        span.update(output={"connection_check": "ok"})
        span.end()
        client.flush()
        print(f"Trace ID: {span.trace_id}")
        print(f"Trace URL: {client.get_trace_url(trace_id=span.trace_id)}")
        if confirm_receipt(client, span.trace_id, started_at):
            print("서버 수신 확인 성공: 이번 trace를 Langfuse API에서 조회했습니다.")
            return 0
        print("서버 수신 미확인: 전송 실패 또는 인덱싱 지연일 수 있습니다.")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"연결 확인 실패: {type(exc).__name__}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
