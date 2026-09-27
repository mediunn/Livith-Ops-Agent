import json
import math


def parse_prometheus_response(response: dict) -> dict:
    """현재 확인한 Grafana MCP의 range 응답을 해석한다."""
    payload = response.get("structuredContent")

    if payload is None:
        text_blocks = [
            block["text"]
            for block in response.get("content", [])
            if block.get("type") == "text"
        ]

        if len(text_blocks) != 1:
            raise ValueError("JSON 텍스트 응답이 정확히 하나여야 합니다.")

        payload = json.loads(text_blocks[0])

    if not isinstance(payload, dict):
        raise TypeError("응답 본문이 객체가 아닙니다.")

    series = payload.get("data")

    # 형식이 잘못된 응답을 데이터 없음으로 처리하지 않는다.
    if not isinstance(series, list):
        raise TypeError("응답의 data 필드가 배열이 아닙니다.")

    summaries = []
    total_samples = 0
    valid_samples = 0
    invalid_samples = 0

    for item in series:
        if not isinstance(item, dict):
            raise TypeError("시계열 항목이 객체가 아닙니다.")

        labels = item.get("metric")
        samples = item.get("values")

        if not isinstance(labels, dict) or not isinstance(samples, list):
            raise TypeError("시계열의 metric 또는 values 형식이 잘못됐습니다.")

        finite_values = []
        latest_value = None
        invalid_count = 0

        for sample in samples:
            if not isinstance(sample, list) or len(sample) != 2:
                raise ValueError("샘플은 [timestamp, value] 형식이어야 합니다.")

            timestamp, raw_value = sample

            if (
                isinstance(timestamp, bool)
                or not isinstance(timestamp, (int, float))
                or not math.isfinite(timestamp)
            ):
                raise ValueError("샘플의 timestamp가 유효하지 않습니다.")

            if isinstance(raw_value, bool) or not isinstance(
                raw_value, (str, int, float)
            ):
                raise TypeError("샘플 값이 숫자 또는 숫자 문자열이 아닙니다.")

            value = float(raw_value)

            # NaN, +Inf, -Inf는 0이나 정상 수치로 해석하지 않는다.
            if math.isfinite(value):
                finite_values.append(value)
                latest_value = value
            else:
                invalid_count += 1
                latest_value = None

        total_samples += len(samples)
        valid_samples += len(finite_values)
        invalid_samples += invalid_count

        summaries.append(
            {
                "labels": labels,
                "sample_count": len(samples),
                "valid_sample_count": len(finite_values),
                "invalid_sample_count": invalid_count,
                "first_timestamp": samples[0][0] if samples else None,
                "last_timestamp": samples[-1][0] if samples else None,
                "min_value": min(finite_values) if finite_values else None,
                "max_value": max(finite_values) if finite_values else None,
                "latest_value": latest_value,
                "all_zero": (
                    all(value == 0 for value in finite_values)
                    if finite_values and invalid_count == 0
                    else None
                ),
            }
        )

    if total_samples == 0:
        status = "no_data"
    elif valid_samples == 0:
        status = "invalid_data"
    else:
        status = "data_available"

    return {
        "status": status,
        "series_count": len(series),
        "sample_count": total_samples,
        "valid_sample_count": valid_samples,
        "invalid_sample_count": invalid_samples,
        "empty_series_count": sum(item["sample_count"] == 0 for item in summaries),
        "series": summaries,
    }
