"""관측값만 정확히 대조한다. 자유 서술의 사실성 판정기는 아니다."""

import json
import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

Count = Annotated[int, Field(strict=True, ge=0)]
DataStatus = Literal["data_available", "no_data", "invalid_data"]
FLOAT_REL_TOL = 1e-12


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SeriesSnapshot(StrictModel):
    labels: dict[str, str]
    sample_count: Count
    valid_sample_count: Count
    invalid_sample_count: Count
    min_value: FiniteFloat | None
    max_value: FiniteFloat | None
    latest_value: FiniteFloat | None


class MetricSnapshot(StrictModel):
    evidence_id: str
    status: DataStatus
    unit: Literal["requests_per_second", "unknown"]
    rate_window_seconds: Count | None
    query_step_seconds: Count | None
    series: list[SeriesSnapshot]


class LogSnapshot(StrictModel):
    evidence_id: str
    status: DataStatus
    log_count: Count
    level_counts: dict[str, Count]
    possibly_truncated: bool


class Observations(StrictModel):
    metrics: MetricSnapshot
    logs: LogSnapshot


def expected_observations(context: dict) -> Observations:
    prom, loki = context["evidence"]
    measurement = prom["measurement"]
    return Observations.model_validate(
        {
            "metrics": {
                "evidence_id": prom["evidence_id"],
                "status": prom["status"],
                "unit": measurement["unit"],
                "rate_window_seconds": measurement.get("rate_window_seconds"),
                "query_step_seconds": measurement.get("query_step_seconds"),
                "series": [
                    {key: series[key] for key in SeriesSnapshot.model_fields}
                    for series in prom["summary"]["series"]
                ],
            },
            "logs": {
                "evidence_id": loki["evidence_id"],
                "status": loki["status"],
                **{
                    key: loki["summary"][key]
                    for key in ("log_count", "level_counts", "possibly_truncated")
                },
            },
        }
    )


def validate_observations(actual: Observations, expected: Observations) -> None:
    def canonical(value):
        data = value.model_dump(mode="json")
        data["metrics"]["series"].sort(
            key=lambda item: json.dumps(item["labels"], sort_keys=True)
        )
        return data

    got, wanted = canonical(actual), canonical(expected)

    def same_series(left, right):
        if len(left) != len(right):
            return False
        for got_series, wanted_series in zip(left, right, strict=True):
            for key, wanted_value in wanted_series.items():
                got_value = got_series[key]
                if (
                    key in {"min_value", "max_value", "latest_value"}
                    and wanted_value is not None
                    and got_value is not None
                ):
                    if not math.isclose(
                        got_value, wanted_value, rel_tol=FLOAT_REL_TOL, abs_tol=0
                    ):
                        return False
                elif got_value != wanted_value:
                    return False
        return True

    mismatches = [
        f"{group}.{key}"
        for group in wanted
        for key in wanted[group]
        if not (
            same_series(got[group][key], wanted[group][key])
            if (group, key) == ("metrics", "series")
            else got[group][key] == wanted[group][key]
        )
    ]
    if mismatches:
        raise ValueError("근거와 다른 관측 필드: " + ", ".join(mismatches))


def render_facts(observations: Observations) -> list[dict]:
    metrics, logs = observations.metrics, observations.logs
    facts = []
    if metrics.status == "no_data":
        statements = [
            "Prometheus 조회에서 샘플이 반환되지 않았다. 요청률 0을 뜻하지 않는다."
        ]
    else:
        unit = (
            "초당 요청 수"
            if metrics.unit == "requests_per_second"
            else "단위 미확인 값"
        )
        statements = []
        for series in metrics.series:
            labels = json.dumps(series.labels, ensure_ascii=False, sort_keys=True)
            if series.valid_sample_count == 0:
                text = f"{labels}: 유효한 수치가 없어 요청률을 판단할 수 없다."
            else:
                latest = (
                    "유효하지 않음"
                    if series.latest_value is None
                    else f"{series.latest_value:g}"
                )
                text = (
                    f"{labels}: 유효한 관측값 범위는 {series.min_value:g}~{series.max_value:g} "
                    f"({unit}), 마지막 값은 {latest}이다."
                )
            statements.append(
                text
                + (
                    f" 샘플 {series.sample_count}개 중 유효 {series.valid_sample_count}개, "
                    f"유효하지 않은 값 {series.invalid_sample_count}개이며 샘플 수는 요청 건수가 아니다."
                )
            )
        if not statements:
            statements = ["유효한 지표 시계열이 없다."]
    if metrics.rate_window_seconds is not None:
        statements.append(
            f"요청률 계산 구간은 {metrics.rate_window_seconds}초, "
            f"조회 평가 간격은 {metrics.query_step_seconds}초다. 원본 수집 간격을 뜻하지 않는다."
        )
    facts.extend(
        {"statement": text, "evidence_ids": [metrics.evidence_id]}
        for text in statements
    )
    text = (
        f"Loki 조회에서 반환된 로그는 {logs.log_count}건이며 레벨별 건수는 "
        f"{json.dumps(logs.level_counts, ensure_ascii=False, sort_keys=True)}이다."
    )
    if logs.possibly_truncated:
        text += " 결과가 잘렸을 수 있어 전체 구간의 총 로그 수로 해석할 수 없다."
    text += " 로그 본문은 이 보고서 입력에 포함되지 않았다."
    facts.append({"statement": text, "evidence_ids": [logs.evidence_id]})
    return facts
