import json

import pytest

from ops_agent.telemetry import langfuse as obs


def test_default_trace_certificate_bundle(monkeypatch):
    for name in (
        "OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE",
        "OTEL_EXPORTER_OTLP_CERTIFICATE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(obs.certifi, "where", lambda: "/test/ca.pem")
    obs.configure_trace_certificates()
    assert obs.os.environ["OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE"] == "/test/ca.pem"


@pytest.mark.parametrize(
    "setting",
    [
        "OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE",
        "OTEL_EXPORTER_OTLP_CERTIFICATE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    ],
)
def test_explicit_trust_settings_preserved(monkeypatch, setting):
    for name in (
        "OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE",
        "OTEL_EXPORTER_OTLP_CERTIFICATE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(setting, "/custom/trust")
    obs.configure_trace_certificates()
    assert obs.os.environ[setting] == "/custom/trust"
    if setting != "OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE":
        assert "OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE" not in obs.os.environ


class FakeSpan:
    trace_id = "a" * 32

    def __init__(self):
        self.updates = []
        self.children = []
        self.ended = False

    def update(self, **kwargs):
        self.updates.append(kwargs)

    def start_observation(self, **kwargs):
        child = FakeSpan()
        self.children.append((kwargs, child))
        return child

    def end(self):
        self.ended = True


class FakeClient:
    def __init__(self):
        self.root = FakeSpan()
        self.scores = []
        self.flush_count = 0

    def start_observation(self, **kwargs):
        self.root_args = kwargs
        return self.root

    def get_trace_url(self, **kwargs):
        return "https://example.test/trace/" + kwargs["trace_id"]

    def flush(self):
        self.flush_count += 1

    def create_score(self, **kwargs):
        self.scores.append(kwargs)


@pytest.fixture
def record():
    return {
        "run_id": "run-test",
        "model": "local-model",
        "prompt_version": "ops_report_v2",
        "context_version": "summary-v2",
        "prompt_sha256": "test-hash",
        "input": {"summary": "test-only"},
        "messages": [{"role": "user", "content": "test-only"}],
        "options": {"temperature": 0},
        "response": {"message": {"content": '{"test":true}'}},
        "usage": {"input_tokens": 12, "output_tokens": 7},
        "report": {"test": True},
        "error": None,
        "status": "generated",
    }


def test_disabled_trace_has_no_client(record):
    trace = obs.ReportTrace(record)
    trace.start_generation()
    trace.end_generation()
    trace.finish()
    assert trace.client is None
    assert record["telemetry"]["status"] == "disabled"


def test_missing_configuration_does_not_raise(record, monkeypatch):
    monkeypatch.setenv("OPS_LANGFUSE_ENABLED", "true")
    monkeypatch.setattr(obs, "load_dotenv", lambda *args: None)
    for name in obs.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    trace = obs.ReportTrace(record)
    trace.finish()
    assert record["telemetry"]["status"] == "error"
    assert record["status"] == "generated"


def test_parenting_io_usage_and_scores(record, monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(obs, "configured_client", lambda: client)
    trace = obs.ReportTrace(record, {"case_id": "zero_rate", "synthetic": True})
    trace.start_generation()
    trace.end_generation()
    trace.finish()
    args, generation = client.root.children[0]
    assert args["as_type"] == "generation"
    assert args["input"] == record["messages"]
    assert generation.updates[0]["usage_details"] == {"input": 12, "output": 7}
    assert generation.updates[0]["output"] == '{"test":true}'
    assert generation.ended and client.root.ended
    assert client.root.updates[-1]["metadata"]["case_id"] == "zero_rate"
    assert client.root.updates[-1]["metadata"]["prompt_sha256"] == "test-hash"
    assert record["telemetry"]["trace_id"] == client.root.trace_id
    assert record["telemetry"]["status"] == "flush_attempted"
    obs.record_evaluation(
        record, {"checks": {"assessment": False, "schema_and_references": True}}
    )
    assert [score["value"] for score in client.scores] == [0.0, 1.0]
    assert all(score["trace_id"] == client.root.trace_id for score in client.scores)
    assert all(score["name"].startswith("auto_") for score in client.scores)
    assert record["telemetry"]["scores_status"] == "flush_attempted"


@pytest.mark.parametrize(
    "operation", ["initialize", "start_observation", "flush", "create_score"]
)
def test_telemetry_failures_do_not_change_report(record, monkeypatch, operation):
    client = FakeClient()

    def fail(*args, **kwargs):
        raise RuntimeError("private-secret-must-not-be-saved")

    monkeypatch.setattr(
        obs, "configured_client", fail if operation == "initialize" else lambda: client
    )
    if operation != "initialize":
        monkeypatch.setattr(client, operation, fail)
    trace = obs.ReportTrace(record)
    trace.start_generation()
    trace.finish()
    obs.record_evaluation(record, {"checks": {"assessment": True}})
    assert record["status"] == "generated"
    assert record["report"] == {"test": True}
    assert record["telemetry"]["errors"]
    assert "private-secret" not in json.dumps(record["telemetry"])


def test_model_failure_ends_generation_with_error(record, monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(obs, "configured_client", lambda: client)
    trace = obs.ReportTrace(record)
    trace.start_generation()
    record.update(
        status="generation_error",
        report=None,
        response=None,
        error={"type": "TimeoutError"},
    )
    record.pop("usage")
    trace.finish()
    generation = client.root.children[0][1]
    assert generation.ended
    assert generation.updates[0]["level"] == "ERROR"
    assert generation.updates[0]["usage_details"] == {}
    assert client.root.updates[-1]["level"] == "ERROR"


def test_workflow_tool_and_report_parenting(record, monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(obs, "configured_client", lambda: client)
    parent = obs.ReportTrace(dict(record), name="ops-investigation")
    step = parent.start_step("query_prometheus", {"expr": "test"})
    parent.end_step(
        step,
        {"status": "no_data", "summary": {}, "response": "raw must not be exported"},
    )
    child = obs.ReportTrace(record, parent_span=parent.root)
    child.start_generation()
    child.finish()
    parent.finish()
    assert [args["name"] for args, span in parent.root.children] == [
        "query_prometheus",
        "ops-report",
    ]
    assert step.ended
    assert "raw must not" not in json.dumps(step.updates)
