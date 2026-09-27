"""합성 근거로 보고서를 비교한다. 기대 정답은 모델에 전달하지 않는다."""

import argparse
import hashlib
import json
from datetime import UTC, datetime
from itertools import product
from pathlib import Path
from uuid import uuid4

from ops_agent.collectors.loki_parser import parse_loki_response
from ops_agent.collectors.prometheus_parser import parse_prometheus_response
from ops_agent.reporting.generator import (
    PROJECT_DIR,
    PROMPT_VERSION,
    PROMPT_VERSIONS,
    run_report,
)
from ops_agent.telemetry.langfuse import record_evaluation

CASES_PATH = PROJECT_DIR / "evals" / "cases.json"
START = "2026-01-01T00:00:00Z"
END = "2026-01-01T00:02:00Z"
START_SECONDS = int(datetime.fromisoformat(START).timestamp())


def write_json(path: Path, data: dict) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def create_evidence(case: dict, directory: Path) -> Path:
    """실제 서비스에 접속하지 않고 기존 파서로 합성 근거를 만든다."""
    directory.mkdir(parents=True, exist_ok=True)
    series = []
    if case["values"]:
        series = [
            {
                "metric": {"api": "synthetic-api"},
                "values": [
                    [START_SECONDS + index * 60, value]
                    for index, value in enumerate(case["values"])
                ],
            }
        ]
    prom_response = {"structuredContent": {"data": series}}
    prom_summary = parse_prometheus_response(prom_response)
    prom_id = f"{case['id']}-prom"
    prom_path = directory / "prometheus.json"
    write_json(
        prom_path,
        {
            "evidence_id": prom_id,
            "tool": "query_prometheus",
            "source": "synthetic_fixture",
            "status": prom_summary["status"],
            "arguments": {
                "datasourceUid": "synthetic-prom",
                "expr": "sum by (api) (rate(external_api_request_total[5m]))",
                "queryType": "range",
                "startTime": START,
                "endTime": END,
                "stepSeconds": 60,
            },
            "summary": prom_summary,
            "response": prom_response,
        },
    )
    entries = [
        {
            "timestamp": str(START_SECONDS * 1_000_000_000 + index),
            "line": "Synthetic log; no incident details provided.",
            "labels": {"job": "livith-server"},
            "structuredMetadata": {"detected_level": level},
        }
        for level, count in case["log_levels"].items()
        for index in range(count)
    ]
    loki_response = {
        "structuredContent": {
            "data": entries,
            "metadata": {"resultsTruncated": case["truncated"]},
        }
    }
    loki_summary = parse_loki_response(loki_response, limit=case["limit"])
    loki_path = directory / "loki.json"
    write_json(
        loki_path,
        {
            "evidence_id": f"{case['id']}-loki",
            "tool": "query_loki_logs",
            "source": "synthetic_fixture",
            "related_prometheus_file": prom_path.name,
            "related_prometheus_evidence_id": prom_id,
            "status": loki_summary["status"],
            "arguments": {
                "datasourceUid": "synthetic-loki",
                "logql": '{job="livith-server"}',
                "queryType": "range",
                "startRfc3339": START,
                "endRfc3339": END,
                "direction": "backward",
                "format": "full",
                "limit": case["limit"],
            },
            "summary": loki_summary,
            "response": loki_response,
        },
    )
    return loki_path


def evaluate_record(record: dict, expected: dict) -> dict:
    generated = record["status"] == "generated"
    report = record["report"] if generated else None
    checks = {
        "schema_and_references": record.get("validation", {}).get(
            "schema_and_references", generated
        ),
        "assessment": (
            report["assessment"] in expected["assessments"] if report else False
        ),
        "hypotheses_policy": (
            not report["hypotheses"]
            if report and expected["empty_hypotheses"]
            else generated
        ),
    }
    if record.get("prompt_version") == "ops_report_v3":
        checks["observation_values"] = (
            record.get("validation", {}).get("observation_values") is True
        )
    return {
        "checks": checks,
        "automatic_pass": all(checks.values()),
        "semantic_review": "pending",
        "review_checks": expected["review_checks"],
    }


def write_review(path: Path, rows: list[dict]) -> None:
    lines = [
        "# 합성 근거 보고서 평가",
        "",
        "자동 검사는 형식·근거 ID·분류·가설 목록만 확인합니다. 내용 정확도는 아래에서 별도로 검토하세요.",
        "모든 입력은 합성 데이터입니다. 실제 서비스 상태나 모델의 일반적 정확도를 나타내지 않습니다.",
        "실행 시간은 모델 로딩·캐시 영향을 포함하며 정밀 벤치마크가 아닙니다.",
        "",
    ]
    for row in rows:
        record = row["record"]
        evaluation = row["evaluation"]
        lines += [
            f"## {row['case_id']} / {record['model']} / {record['prompt_version']}",
            "",
            f"- 생성 상태: {record['status']}",
            f"- 자동 검사: {evaluation['checks']}",
            f"- 실행 시간: {record['elapsed_seconds']}초",
            "- 내용 검토: pending (각 항목에 pass / fail과 이유를 작성)",
            "",
        ]
        lines += [f"- [ ] {check}" for check in evaluation["review_checks"]]
        lines += [
            "",
            "```json",
            json.dumps(
                record["report"] or record["error"], ensure_ascii=False, indent=2
            ),
            "```",
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="합성 근거로 로컬 모델·프롬프트 버전 비교"
    )
    parser.add_argument("--models", nargs="+", default=["qwen2.5:3b"])
    parser.add_argument("--case", help="특정 사례 ID만 실행")
    parser.add_argument(
        "--prompt-versions",
        nargs="+",
        choices=PROMPT_VERSIONS,
        default=[PROMPT_VERSION],
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="모델 호출 없이 근거 생성"
    )
    args = parser.parse_args()
    if any(not model.strip() or model.endswith("-cloud") for model in args.models):
        parser.error("설치된 로컬 모델 이름을 지정하세요.")
    dataset = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases = [case for case in dataset["cases"] if args.case in (None, case["id"])]
    if not cases:
        parser.error("해당 사례 ID가 없습니다.")

    output = (
        PROJECT_DIR
        / "artifacts"
        / "evaluations"
        / (f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{str(uuid4())[:8]}")
    )
    output.mkdir(parents=True)
    write_json(output / "dataset.json", dataset)
    paths = {case["id"]: create_evidence(case, output / case["id"]) for case in cases}
    print(f"평가 폴더: {output}", flush=True)
    if args.dry_run:
        print("합성 근거만 생성했습니다. 모델은 호출하지 않았습니다.")
        return

    rows = []
    summary = {"dataset_version": dataset["version"], "synthetic": True, "results": []}
    # 모델별로 순차 실행해 로딩 경합과 메모리 부담을 줄인다.
    for model in dict.fromkeys(args.models):
        model_key = hashlib.sha256(model.encode()).hexdigest()[:12]
        for version, case in product(dict.fromkeys(args.prompt_versions), cases):
            print(f"평가 중: {model} / {version} / {case['id']}", flush=True)
            record = run_report(
                paths[case["id"]],
                model=model,
                prompt_version=version,
                trace_metadata={
                    "evaluation_run_id": output.name,
                    "dataset_version": dataset["version"],
                    "case_id": case["id"],
                    "case_group": case.get("group", "regression"),
                    "synthetic": True,
                },
            )
            evaluation = evaluate_record(record, case["expected"])
            record_evaluation(record, evaluation)
            row = {"case_id": case["id"], "record": record, "evaluation": evaluation}
            rows.append(row)
            record_name = f"{case['id']}-{model_key}-{version}.json"
            write_json(output / record_name, row)
            summary["results"].append(
                {
                    "case_id": case["id"],
                    "case_group": case.get("group", "regression"),
                    "model": model,
                    "prompt_version": record["prompt_version"],
                    "context_version": record["context_version"],
                    "file": record_name,
                    "status": record["status"],
                    "elapsed_seconds": record["elapsed_seconds"],
                    "usage": record.get("usage"),
                    "telemetry": record.get("telemetry"),
                    "prompt_sha256": record.get("prompt_sha256"),
                    **evaluation,
                }
            )
            # 중간에 중단돼도 완료한 사례의 결과와 검토 문서를 보존한다.
            write_json(output / "summary.json", summary)
            write_review(output / "review.md", rows)
            print(f"자동 검사: {evaluation['checks']}", flush=True)
    print(f"내용 검토 문서: {output / 'review.md'}")
    raise SystemExit(
        0 if all(row["evaluation"]["automatic_pass"] for row in rows) else 1
    )


if __name__ == "__main__":
    main()
