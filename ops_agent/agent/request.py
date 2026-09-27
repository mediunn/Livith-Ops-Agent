"""사용자의 명시적 입력을 검증된 조사 요청으로 변환한다."""

from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

DEFAULT_SYMPTOM = "외부 API 요청률과 관련 로그의 추가 조사 필요성 확인"
DEFAULT_TIMEZONE = "Asia/Seoul"
DEFAULT_WINDOW = timedelta(minutes=30)
MAX_WINDOW = timedelta(hours=6)


class InvestigationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    service: Literal["livith-server"]
    environment: Literal["unspecified"]
    investigation_type: Literal["external_api"]
    timezone: str
    compare_previous: bool = Field(default=True, strict=True)
    start_at: AwareDatetime
    end_at: AwareDatetime
    symptom: str = Field(min_length=1, max_length=1000)
    defaults_applied: tuple[str, ...] = ()

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("유효한 IANA 시간대가 필요합니다.") from exc
        return value

    @field_validator("symptom")
    @classmethod
    def validate_symptom(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("증상을 입력하세요.")
        return value

    @field_validator("start_at", "end_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_window(self):
        duration = self.end_at - self.start_at
        if duration <= timedelta(0):
            raise ValueError("시작 시각은 종료 시각보다 앞서야 합니다.")
        if duration > MAX_WINDOW:
            raise ValueError("조회 범위는 최대 6시간입니다.")
        if self.end_at > datetime.now(UTC):
            raise ValueError("미래 시각까지 조회할 수 없습니다.")
        return self

    def as_window(self) -> dict[str, str]:
        return {
            "start": self.start_at.isoformat(),
            "end": self.end_at.isoformat(),
        }


def parse_time(value: str, timezone: str) -> datetime:
    """오프셋이 있으면 그대로, 없으면 지정 시간대의 벽시계로 해석한다."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC)

    zone = ZoneInfo(timezone)
    candidates = set()
    # DST 전환으로 존재하지 않거나 두 번 발생하는 현지 시각을 구분한다.
    for fold in (0, 1):
        candidate = parsed.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        restored = candidate.astimezone(zone).replace(tzinfo=None)
        if restored == parsed:
            candidates.add(candidate)
    if len(candidates) != 1:
        raise ValueError(
            "모호하거나 존재하지 않는 현지 시각입니다. UTC 오프셋을 명시하세요."
        )
    return candidates.pop()


def request_from_args(args) -> InvestigationRequest:
    defaults = []

    def resolve(name, default):
        value = getattr(args, name, None)
        if value is None:
            defaults.append(name)
            return default
        return value

    timezone = resolve("timezone", DEFAULT_TIMEZONE)
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"지원하지 않는 시간대: {timezone}") from exc

    start_text = getattr(args, "start", None)
    end_text = getattr(args, "end", None)
    if (start_text is None) != (end_text is None):
        raise ValueError("--start와 --end를 함께 입력하세요.")
    if start_text is None:
        end = datetime.now(UTC).replace(microsecond=0)
        start = end - DEFAULT_WINDOW
        defaults.append("time_window")
    else:
        start = parse_time(start_text, timezone)
        end = parse_time(end_text, timezone)

    return InvestigationRequest(
        service=resolve("service", "livith-server"),
        environment=resolve("environment", "unspecified"),
        investigation_type=resolve("investigation_type", "external_api"),
        timezone=timezone,
        compare_previous=resolve("compare_previous", True),
        start_at=start,
        end_at=end,
        symptom=resolve("symptom", DEFAULT_SYMPTOM),
        defaults_applied=tuple(defaults),
    )
