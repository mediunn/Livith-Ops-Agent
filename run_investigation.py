"""정해진 읽기 전용 쿼리로 수집→보고서를 실행한다. 자율 도구 선택은 하지 않는다."""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from ops_agent.collectors.loki import DEFAULT_LOGQL
from ops_agent.collectors.loki import collect_evidence as collect_logs
from ops_agent.collectors.prometheus import PROMQL
from ops_agent.collectors.prometheus import collect as collect_metrics
from ops_agent.reporting.generator import PROJECT_DIR, PROMPT_VERSION, run_report
from ops_agent.telemetry.langfuse import ReportTrace

READABLE_STATUSES = {"data_available", "no_data", "invalid_data"}


def save(path: Path, record: dict) -> None:
    path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


async def investigate(*, model: str, symptom: str) -> tuple[Path, dict]:
    started = perf_counter()
    run_id = str(uuid4())
    scope = {
        "incident_id": str(uuid4()),
        "service": "livith-server",
        "environment": "unspecified",
        "environment_filter_applied": False,
        "symptom": symptom,
    }
    directory = PROJECT_DIR / "artifacts" / "investigations" / run_id
    directory.mkdir(parents=True)
    record = {
        "run_id": run_id,
        "started_at": datetime.now(UTC).isoformat(),
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "context_version": "summary-v3",
        "investigation": scope,
        "status": "pending",
        "evidence_files": {},
        "report_file": None,
        "report": None,
        "error": None,
    }
    trace = ReportTrace(record, scope, name="ops-investigation")
    active_step = None
    stage = "prometheus"
    try:
        active_step = trace.start_step(
            "query_prometheus", {"expr": PROMQL, "window_minutes": 30}
        )
        prom_path, prom = await collect_metrics(output_dir=directory)
        trace.end_step(active_step, prom)
        active_step = None
        record["evidence_files"]["prometheus"] = prom_path.name
        if prom["status"] not in READABLE_STATUSES:
            raise ValueError(f"Prometheus 수집 실패: {prom['status']}")
        record["window"] = {
            key: prom["arguments"][key] for key in ("startTime", "endTime")
        }

        stage = "loki"
        active_step = trace.start_step(
            "query_loki_logs",
            {**record["window"], "logql": DEFAULT_LOGQL, "limit": 100},
        )
        loki_path, loki = await collect_logs(
            prom_path,
            logql=DEFAULT_LOGQL,
            limit=100,
            discover=False,
            output_dir=directory,
        )
        trace.end_step(active_step, loki)
        active_step = None
        record["evidence_files"]["loki"] = loki_path.name
        if loki["status"] not in READABLE_STATUSES:
            raise ValueError(f"Loki 수집 실패: {loki['status']}")

        stage = "report"
        report = run_report(
            loki_path,
            model=model,
            prompt_version=PROMPT_VERSION,
            investigation=scope,
            trace_metadata={**scope, "investigation_run_id": run_id},
            trace_parent=trace.root,
        )
        save(directory / "report.json", report)
        record["report_file"] = "report.json"
        record["report"] = report["report"]
        record["validation"] = report["validation"]
        record["status"] = report["status"]
        record["error"] = report["error"]
        record["report_run_id"] = report["run_id"]
    except Exception as exc:  # noqa: BLE001
        trace.end_step(
            active_step, {"status": "failed", "error_type": type(exc).__name__}
        )
        record["status"] = f"{stage}_error"
        record["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        record["completed_at"] = datetime.now(UTC).isoformat()
        record["elapsed_seconds"] = round(perf_counter() - started, 3)
        trace.finish()
        save(directory / "investigation.json", record)
    print(f"조사 결과: {record['status']}")
    print(f"결과 폴더: {directory}")
    if record["telemetry"].get("trace_url"):
        print(f"Trace: {record['telemetry']['trace_url']}")
    return directory, record


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Livith 고정 쿼리 수집과 검증된 보고서 생성"
    )
    parser.add_argument("--model", default="qwen2.5:3b")
    parser.add_argument(
        "--symptom", default="최근 30분 외부 API 요청률과 로그의 추가 조사 필요성 확인"
    )
    args = parser.parse_args()
    if not args.model.strip() or args.model.endswith("-cloud"):
        parser.error("설치된 로컬 모델 이름을 지정하세요.")
    if not args.symptom.strip() or len(args.symptom) > 1000:
        parser.error("--symptom은 1~1000자여야 합니다.")
    _, record = asyncio.run(investigate(model=args.model, symptom=args.symptom))
    raise SystemExit(0 if record["status"] == "generated" else 1)


if __name__ == "__main__":
    main()
