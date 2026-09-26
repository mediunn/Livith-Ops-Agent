import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Annotated, Literal
from uuid import uuid4

from ollama import Client
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

PROJECT_DIR = Path(__file__).resolve().parent
ARTIFACTS_DIR = PROJECT_DIR / "artifacts"
PROMPT_VERSIONS = ("ops_report_v1", "ops_report_v2")
PROMPT_VERSION = "ops_report_v2"
PROMPT_PATH = PROJECT_DIR / "prompts" / f"{PROMPT_VERSION}.txt"
KNOWN_RATE_QUERY = "sum by (api) (rate(external_api_request_total[5m]))"
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class EvidenceClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: NonEmptyText
    evidence_ids: list[NonEmptyText] = Field(min_length=1)


class OpsReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assessment: Literal[
        "insufficient_evidence", "needs_investigation", "no_issue_observed"
    ]
    facts: list[EvidenceClaim] = Field(
        min_length=2,
        description="Prometheus의 관측값과 Loki의 로그 수·레벨을 각각 사실로 작성",
    )
    hypotheses: list[EvidenceClaim] = Field(
        description="원인에 관한 미확인 추론만 작성. 원인 단서가 없으면 빈 배열"
    )
    limitations: list[NonEmptyText] = Field(min_length=1)
    next_checks: list[NonEmptyText] = Field(min_length=1)


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def read_evidence(path: Path, expected_tool: str) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("tool") != expected_tool:
        raise ValueError(f"올바른 {expected_tool} 파일이 아닙니다: {path.name}")
    if data.get("status") not in {"data_available", "no_data", "invalid_data"}:
        raise ValueError(f"조회가 실패한 근거입니다: {path.name}")
    if not isinstance(data.get("summary"), dict):
        raise TypeError(f"summary가 객체가 아닙니다: {path.name}")
    if data["summary"].get("status") != data["status"]:
        raise ValueError(f"근거와 요약의 상태가 다릅니다: {path.name}")
    if not isinstance(data.get("arguments"), dict):
        raise TypeError(f"조회 조건이 객체가 아닙니다: {path.name}")
    evidence_id = data.get("evidence_id")
    if not isinstance(evidence_id, str) or not evidence_id.strip():
        raise ValueError(f"evidence_id가 없습니다: {path.name}")
    return data


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("근거의 조회 시각에 시간대가 없습니다.")
    return parsed


def metric_semantics(arguments: dict) -> dict:
    """확인된 쿼리만 설명한다. 임의의 PromQL에 단위를 추측해 붙이지 않는다."""
    result = {
        "query_step_seconds": arguments.get("stepSeconds"),
        "sample_count_meaning": "반환된 평가 시점의 수이며 요청 건수가 아님",
        "unit": "unknown",
    }
    expression = arguments.get("expr", "")
    if isinstance(expression, str) and "".join(expression.split()) == "".join(
        KNOWN_RATE_QUERY.split()
    ):
        result.update(
            {
                "unit": "requests_per_second",
                "unit_ko": "초당 요청 수",
                "rate_window_seconds": 300,
                "rate_window_meaning": "각 평가 시점 직전 5분으로 평균 초당 증가율을 계산",
                "query_step_meaning": "쿼리 평가 시점 간 간격이며 원본 수집 간격은 아님",
                "scope": "api별 외부 API 요청률; 전체 서비스 요청률이나 성공률이 아님",
                "environment_filter": "none",
            }
        )
    return result


def build_context(loki_path: Path | None, *, enriched: bool = True) -> dict:
    """서로 연결된 근거의 요약만 전달한다. 원본 로그는 포함하지 않는다."""
    if loki_path is None:
        candidates = list(ARTIFACTS_DIR.glob("loki-[0-9]*.json"))
        if not candidates:
            raise ValueError("먼저 query_loki.py를 실행하세요.")
        loki_path = max(candidates, key=lambda path: path.stat().st_mtime)

    loki_path = loki_path.resolve()
    loki = read_evidence(loki_path, "query_loki_logs")
    reference = loki.get("related_prometheus_file")
    if not isinstance(reference, str) or not reference:
        raise ValueError("Loki 근거에 Prometheus 파일 경로가 없습니다.")
    prom_path = Path(reference)
    if not prom_path.is_absolute():
        prom_path = loki_path.parent / prom_path
    prom = read_evidence(prom_path, "query_prometheus")

    if loki.get("related_prometheus_evidence_id") != prom["evidence_id"]:
        raise ValueError("Prometheus와 Loki의 근거 연결이 일치하지 않습니다.")
    if prom["evidence_id"] == loki["evidence_id"]:
        raise ValueError("서로 다른 근거의 ID가 중복됐습니다.")

    prom_args, loki_args = prom["arguments"], loki["arguments"]
    if any(args.get("queryType") != "range" for args in (prom_args, loki_args)):
        raise ValueError("두 근거 모두 range 조회여야 합니다.")
    start, end = parse_time(prom_args["startTime"]), parse_time(prom_args["endTime"])
    if start >= end:
        raise ValueError("조회 시간 범위가 잘못됐습니다.")
    if start != parse_time(loki_args["startRfc3339"]) or end != parse_time(
        loki_args["endRfc3339"]
    ):
        raise ValueError("Prometheus와 Loki의 조회 시간이 다릅니다.")

    context = {
        "window": {"start": prom_args["startTime"], "end": prom_args["endTime"]},
        "scope": "수집된 조회 결과의 요약만 제공. 로그 본문은 미포함.",
        "evidence": [
            {
                "evidence_id": item["evidence_id"],
                "tool": item["tool"],
                "status": item["status"],
                "arguments": item["arguments"],
                "summary": item["summary"],
            }
            for item in (prom, loki)
        ],
    }
    if enriched:
        context["assessment_scope"] = "service_health_triage"
        context["coverage"] = {
            "log_bodies_included": False,
            "service_health_criteria_provided": False,
            "description": (
                "외부 API 지표와 로그 요약만 제공한다. "
                "서비스 HTTP 오류율·지연·가용성 기준 및 SLO는 포함하지 않는다."
            ),
        }
        context["evidence"][0]["measurement"] = metric_semantics(prom_args)
    return context


def validate_references(report: OpsReport, context: dict) -> None:
    """ID 존재 여부만 검증한다. 주장의 사실성 검증은 별도 평가가 필요하다."""
    allowed = {item["evidence_id"] for item in context["evidence"]}
    for claim in report.facts + report.hypotheses:
        unknown = set(claim.evidence_ids) - allowed
        if unknown:
            raise ValueError(f"알 수 없는 근거 ID: {sorted(unknown)}")
    covered = {item for claim in report.facts for item in claim.evidence_ids}
    if missing := allowed - covered:
        raise ValueError(f"facts에서 누락된 근거 ID: {sorted(missing)}")


def run_report(
    loki_path: Path | None, *, model: str, prompt_version: str | None = None
) -> dict:
    """수집된 근거로 보고서를 생성한다. 저장 위치는 호출자가 결정한다."""
    version = prompt_version or PROMPT_VERSION
    prompt_path = (
        PROJECT_DIR / "prompts" / f"{version}.txt" if prompt_version else PROMPT_PATH
    )
    run_id = str(uuid4())
    record = {
        "run_id": run_id,
        "started_at": now(),
        "model": model,
        "prompt_version": version,
        "context_version": "summary-v1" if version == "ops_report_v1" else "summary-v2",
        "status": "pending",
        "input": None,
        "response": None,
        "report": None,
        "error": None,
    }
    started = perf_counter()
    stage = "prompt"
    try:
        if version not in PROMPT_VERSIONS:
            raise ValueError(f"지원하지 않는 프롬프트 버전: {version}")
        system_prompt = prompt_path.read_text(encoding="utf-8").strip()
        if not system_prompt:
            raise ValueError(f"프롬프트가 비어 있습니다: {prompt_path}")
        record["system_prompt"] = system_prompt
        record["prompt_sha256"] = hashlib.sha256(system_prompt.encode()).hexdigest()

        stage = "input"
        context = build_context(loki_path, enriched=version != "ops_report_v1")
        schema = OpsReport.model_json_schema()
        record["input"] = context
        record["output_schema"] = schema
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": (
                            "두 evidence를 모두 읽고 보고서를 작성하라. "
                            "facts에는 Prometheus 관측값과 Loki 로그 요약을 각각 넣어라. "
                            "로그 수·레벨은 가설이 아니라 관측 사실이다. "
                            "원인 단서가 없으면 hypotheses는 빈 배열로 두어라."
                        ),
                        "context": context,
                        "output_schema": schema,
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        options = {"temperature": 0, "num_ctx": 8192, "num_predict": 2048}
        record["messages"] = messages
        record["options"] = options

        stage = "generation"
        print(f"보고서 생성 중: {model}", flush=True)
        client = Client(host="http://127.0.0.1:11434", timeout=180.0)
        response = client.chat(
            model=model,
            messages=messages,
            format=schema,
            stream=False,
            options=options,
        )
        record["response"] = response.model_dump(mode="json")
        record["usage"] = {
            "input_tokens": response.prompt_eval_count,
            "output_tokens": response.eval_count,
        }

        stage = "validation"
        if not response.done or response.done_reason == "length":
            raise ValueError("모델 출력이 끝나기 전에 잘렸습니다.")
        report = OpsReport.model_validate_json(response.message.content or "")
        validate_references(report, context)
        record["report"] = report.model_dump(mode="json")
        record["status"] = "generated"
    # 실패해도 입력·수신 응답·실패 단계를 남기기 위한 실행 경계.
    except Exception as exc:  # noqa: BLE001
        record["status"] = f"{stage}_error"
        record["error"] = {"type": type(exc).__name__, "message": str(exc)}

    record["completed_at"] = now()
    record["elapsed_seconds"] = round(perf_counter() - started, 3)
    return record


def generate_report(
    loki_path: Path | None, *, model: str, prompt_version: str | None = None
) -> int:
    record = run_report(loki_path, model=model, prompt_version=prompt_version)
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    output = ARTIFACTS_DIR / (
        f"report-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{record['run_id'][:8]}.json"
    )
    output.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"상태: {record['status']}")
    print(json.dumps(record["report"] or record["error"], ensure_ascii=False, indent=2))
    print(f"결과 저장: {output}")
    return 0 if record["status"] == "generated" else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Ollama 운영 보고서 생성")
    parser.add_argument("--model", default="qwen2.5:3b")
    parser.add_argument("--loki-evidence", type=Path)
    parser.add_argument(
        "--prompt-version", choices=PROMPT_VERSIONS, default=PROMPT_VERSION
    )
    args = parser.parse_args()
    if not args.model.strip() or args.model.endswith("-cloud"):
        parser.error("설치된 로컬 모델 이름을 지정하세요.")
    raise SystemExit(
        generate_report(
            args.loki_evidence, model=args.model, prompt_version=args.prompt_version
        )
    )


if __name__ == "__main__":
    main()
