from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import run_agent
from ops_agent.agent.request import InvestigationRequest, parse_time, request_from_args


def explicit_args(**updates):
    return SimpleNamespace(
        **{
            "service": "livith-server",
            "environment": "unspecified",
            "investigation_type": "external_api",
            "timezone": "Asia/Seoul",
            "compare_previous": True,
            "start": "2025-01-01T22:00:00",
            "end": "2025-01-01T23:00:00",
            "symptom": "호출량 변화 확인",
            **updates,
        }
    )


def test_kst_input_and_explicit_offsets_describe_same_window():
    local = request_from_args(explicit_args())
    explicit = request_from_args(
        explicit_args(start="2025-01-01T13:00:00Z", end="2025-01-01T14:00:00Z")
    )
    assert (
        local.as_window()
        == explicit.as_window()
        == {
            "start": "2025-01-01T13:00:00+00:00",
            "end": "2025-01-01T14:00:00+00:00",
        }
    )
    assert local.defaults_applied == ()
    assert InvestigationRequest.model_validate_json(local.model_dump_json()) == local


def test_defaults_are_recorded_and_window_is_thirty_minutes():
    before = datetime.now(UTC).replace(microsecond=0)
    request = request_from_args(SimpleNamespace())
    assert before <= request.end_at <= datetime.now(UTC)
    assert request.end_at - request.start_at == timedelta(minutes=30)
    assert set(request.defaults_applied) == {
        "timezone",
        "time_window",
        "service",
        "environment",
        "investigation_type",
        "symptom",
        "compare_previous",
    }


@pytest.mark.parametrize(
    "updates",
    [
        {"start": None},
        {"end": None},
        {"start": "2025-01-01T23:00:00"},
        {"start": "2025-01-02T00:00:00"},
        {"start": "2025-01-01T16:59:59"},
        {"start": "2999-01-01T22:00:00", "end": "2999-01-01T23:00:00"},
        {"start": "not-a-date"},
        {"timezone": "Missing/Timezone"},
        {"service": "other-service"},
        {"environment": "production"},
        {"investigation_type": "http_latency"},
        {"symptom": "   "},
        {"symptom": "x" * 1001},
    ],
)
def test_invalid_requests_are_rejected(updates):
    with pytest.raises(ValueError):
        request_from_args(explicit_args(**updates))


def test_six_hour_boundary_is_allowed():
    request = request_from_args(explicit_args(start="2025-01-01T17:00:00"))
    assert request.end_at - request.start_at == timedelta(hours=6)


@pytest.mark.parametrize("value", ["2025-03-09T02:30:00", "2025-11-02T01:30:00"])
def test_dst_missing_and_ambiguous_local_times_require_offset(value):
    with pytest.raises(ValueError, match="UTC 오프셋"):
        parse_time(value, "America/New_York")


def test_explicit_offsets_disambiguate_dst_overlap():
    early = parse_time("2025-11-02T01:30:00-04:00", "America/New_York")
    late = parse_time("2025-11-02T01:30:00-05:00", "America/New_York")
    assert late - early == timedelta(hours=1)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--start", "2025-01-01T22:00:00"],
        ["--timezone", "Missing/Timezone"],
        ["--environment", "production"],
        ["--service", "other-service"],
        ["--investigation-type", "http_latency"],
        ["--symptom", "   "],
        ["--seconds", "0"],
        ["--status", "saved", "--step"],
        ["--resume", "saved", "--seconds", "600"],
        ["--status", "saved", "--timezone", "Asia/Seoul"],
        ["--resume", "saved", "--start", "2025-01-01T22:00:00"],
        ["--resume", "saved", "--symptom", "변경"],
        ["--resume", "saved", "--model", "qwen2.5:3b"],
        ["--resume", "saved", "--no-compare-previous"],
    ],
)
def test_cli_rejects_invalid_input_before_io(arguments, tmp_path, monkeypatch):
    root = tmp_path / "must-not-be-created"
    monkeypatch.setattr(run_agent, "AGENT_ROOT", root)
    monkeypatch.setattr("sys.argv", ["run_agent.py", *arguments])

    async def forbidden(args):
        pytest.fail("잘못된 입력에서 실행을 시작했습니다.")

    monkeypatch.setattr(run_agent, "run", forbidden)
    with pytest.raises(SystemExit) as exc:
        run_agent.main()
    assert exc.value.code == 2
    assert not root.exists()
