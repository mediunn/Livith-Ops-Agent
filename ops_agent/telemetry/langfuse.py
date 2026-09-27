"""선택적으로 Langfuse에 기록한다. 추적 실패가 보고서 생성을 중단하지 않는다."""

import os

import certifi
from dotenv import load_dotenv

from ops_agent.config import PROJECT_DIR

REQUIRED_ENV = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL")


def configure_trace_certificates() -> None:
    """별도 신뢰 저장소 설정이 없으면 certifi의 CA로 TLS 검증을 유지한다."""
    explicit_settings = (
        "OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE",
        "OTEL_EXPORTER_OTLP_CERTIFICATE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    )
    if any(os.getenv(name) for name in explicit_settings):
        return
    os.environ["OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE"] = certifi.where()


def configured_client():
    load_dotenv(PROJECT_DIR / ".env")
    if os.getenv("OPS_LANGFUSE_ENABLED", "false").lower() != "true":
        return None
    missing = [name for name in REQUIRED_ENV if not os.getenv(name, "").strip()]
    if missing:
        raise ValueError("누락된 설정: " + ", ".join(missing))
    configure_trace_certificates()
    from langfuse import Langfuse

    return Langfuse(
        public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
        secret_key=os.environ["LANGFUSE_SECRET_KEY"],
        base_url=os.environ["LANGFUSE_BASE_URL"],
        timeout=5,
        environment="local",
    )


class ReportTrace:
    def __init__(
        self,
        record: dict,
        metadata: dict | None = None,
        *,
        parent_span=None,
        name="ops-report",
    ):
        self.record = record
        self.client = None
        self.root = None
        self.generation = None
        self.metadata = {
            "run_id": record["run_id"],
            "model": record["model"],
            "prompt_version": record["prompt_version"],
            "context_version": record["context_version"],
            **(metadata or {}),
        }
        self.state = {"status": "disabled", "trace_id": None, "errors": []}
        record["telemetry"] = self.state
        self.client = self.safe("initialize", configured_client)
        if self.client is None:
            return
        self.state["status"] = "recording"
        self.root = self.safe(
            "start_trace",
            lambda: (parent_span or self.client).start_observation(
                name=name,
                as_type="span",
                metadata=self.metadata,
            ),
        )
        if self.root is not None:
            self.state["trace_id"] = self.root.trace_id

    def start_step(self, name: str, arguments: dict):
        if self.root is None:
            return None
        return self.safe(
            "start_step",
            lambda: self.root.start_observation(
                name=name, as_type="tool", input=arguments
            ),
        )

    def end_step(self, span, evidence: dict):
        if span is None:
            return
        error_type = evidence.get("error_type")
        self.safe(
            "step_output",
            lambda: span.update(
                output={
                    key: evidence.get(key)
                    for key in ("evidence_id", "status", "summary")
                },
                level="ERROR" if error_type else "DEFAULT",
                status_message=error_type,
            ),
        )
        self.safe("end_step", span.end)

    def safe(self, operation, callback):
        try:
            return callback()
        except Exception as exc:  # noqa: BLE001
            # SDK 오류 문자열에 자격 증명이나 요청 내용이 포함될 수 있어 종류만 저장.
            self.state["status"] = "error"
            self.state["errors"].append(
                {"operation": operation, "type": type(exc).__name__}
            )
            return None

    def start_generation(self, name="ollama-report", metadata: dict | None = None):
        if self.root is None:
            return
        self.safe(
            "input",
            lambda: self.root.update(
                input=self.record["input"],
                metadata={
                    **self.metadata,
                    "prompt_sha256": self.record["prompt_sha256"],
                },
            ),
        )
        self.generation = self.safe(
            "start_generation",
            lambda: self.root.start_observation(
                name=name,
                as_type="generation",
                model=self.record["model"],
                input=self.record["messages"],
                model_parameters=self.record["options"],
                version=self.record["prompt_version"],
                metadata=metadata or {},
            ),
        )

    def end_generation(
        self, error_type: str | None = None, *, validation: dict | None = None
    ):
        if self.generation is None:
            return
        response = self.record.get("response") or {}
        usage = self.record.get("usage") or {}
        details = {
            key: value
            for key, value in (
                ("input", usage.get("input_tokens")),
                ("output", usage.get("output_tokens")),
            )
            if isinstance(value, int) and not isinstance(value, bool)
        }
        diagnostic = {"metadata": {"validation": validation}} if validation else {}
        status_message = (
            validation["code"] if validation and not validation["valid"] else error_type
        )
        self.safe(
            "generation_output",
            lambda: self.generation.update(
                output=(response.get("message") or {}).get("content"),
                usage_details=details,
                level="ERROR" if error_type else "DEFAULT",
                status_message=status_message,
                **diagnostic,
            ),
        )
        self.safe("end_generation", self.generation.end)
        self.generation = None

    def finish(self):
        error_type = (self.record.get("error") or {}).get("type")
        self.end_generation(error_type)
        if self.root is None:
            return
        self.safe(
            "trace_output",
            lambda: self.root.update(
                output={
                    "status": self.record["status"],
                    "report": self.record["report"],
                },
                metadata={
                    **self.metadata,
                    "prompt_sha256": self.record.get("prompt_sha256"),
                    "error_type": error_type,
                    "validation": self.record.get("validation"),
                    "semantic_review": "pending",
                },
                level="ERROR" if error_type else "DEFAULT",
                status_message=error_type,
            ),
        )
        self.safe("end_trace", self.root.end)
        self.state["trace_url"] = self.safe(
            "trace_url",
            lambda: self.client.get_trace_url(trace_id=self.state["trace_id"]),
        )
        self.safe("flush", self.client.flush)
        if not self.state["errors"]:
            # SDK의 flush는 서버 수신 확인을 반환하지 않는다.
            self.state["status"] = "flush_attempted"


def record_evaluation(record: dict, evaluation: dict) -> None:
    state = record.get("telemetry", {})
    trace_id = state.get("trace_id")
    if not trace_id:
        return
    try:
        client = configured_client()
        if client is None:
            return
        for name, passed in evaluation["checks"].items():
            client.create_score(
                name=f"auto_{name}",
                value=float(passed),
                data_type="BOOLEAN",
                trace_id=trace_id,
                score_id=f"{record['run_id']}-{name}",
                comment="자동 조건 검사이며 보고서 내용의 정확도 점수가 아닙니다.",
            )
        client.flush()
        state["scores_status"] = "flush_attempted"
    except Exception as exc:  # noqa: BLE001
        state["scores_status"] = "error"
        state["errors"].append({"operation": "scores", "type": type(exc).__name__})
