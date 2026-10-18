"""SQLite 조사 세션을 열고 실행별 추적·결과 파일을 관리한다."""

import hashlib
import sys
import time
from pathlib import Path
from uuid import uuid4

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from ops_agent.agent.budget import initialize_budget
from ops_agent.agent.graph import build_graph
from ops_agent.agent.request import request_from_args
from ops_agent.cli.presentation import print_result
from ops_agent.config import (
    AGENT_PROMPT_PATH,
    AGENT_ROOT,
    CHECKPOINT_DB,
    PROMPT_VERSION,
    STATE_VERSION,
)
from ops_agent.persistence.artifacts import save_json
from ops_agent.telemetry.langfuse import ReportTrace


def initial_state(args, thread_id: str) -> dict:
    request = getattr(args, "request", None)
    if request is None:
        request = request_from_args(args)
    prompt = AGENT_PROMPT_PATH.read_text(encoding="utf-8").strip()
    if not prompt:
        raise ValueError("Agent 프롬프트가 비어 있습니다.")
    directory = AGENT_ROOT / thread_id
    directory.mkdir(parents=True, exist_ok=False)
    initialize_budget(directory)
    return {
        "version": STATE_VERSION,
        "thread_id": thread_id,
        "model": args.model,
        "request": request.model_dump(mode="json"),
        "symptom": request.symptom,
        "directory": str(directory),
        "window": request.as_window(),
        "deadline": time.time() + args.seconds,
        "limits": {"tool_calls": 6, "llm_calls": 6, "reserved_tokens": 110000},
        "prompt": prompt,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "action": "",
        "evidence": [],
        "decisions": [],
        "log_sample_pages": [],
        "log_sample_request": None,
        "stop_reason": "",
        "error_type": None,
        "decision_error": None,
        "report": None,
    }


def display(state: dict, pending, *, output="answer") -> None:
    stream = sys.stderr if output == "json" else sys.stdout
    print(f"Thread ID: {state['thread_id']}", file=stream)
    print(f"다음 노드: {list(pending)}", file=stream)
    print(
        f"근거 수: {len(state['evidence'])}, 모델 판단 수: {len(state['decisions'])}",
        file=stream,
    )
    print(f"결과 폴더: {state['directory']}", file=stream)
    display_request(state, file=stream)
    result = state.get("report") or {
        "status": "paused",
        "thread_id": state["thread_id"],
        "next_nodes": list(pending),
    }
    print_result(result, output=output, directory=Path(state["directory"]))


def display_request(state: dict, *, file=None) -> None:
    request = state["request"]
    print(
        f"조사 대상: {request['service']} / "
        f"{request['environment']} / {request['investigation_type']}",
        file=file,
    )
    print(
        f"조회 구간(UTC): {state['window']['start']} ~ {state['window']['end']}",
        file=file,
    )
    print(
        f"직전 구간 비교 필수: {request.get('compare_previous', '이전 요청에 없음')}",
        file=file,
    )
    print(f"입력 해석 시간대: {request['timezone']}", file=file)
    print(f"기본값 적용: {', '.join(request['defaults_applied']) or '없음'}", file=file)


def result_code(state: dict) -> int:
    report = state.get("report")
    return 0 if report and report["status"] == "completed" else 1


async def run(args) -> int:
    existing_id = args.resume or args.status
    thread_id = existing_id or getattr(args, "thread_id", None) or uuid4().hex
    output = getattr(args, "output", "answer")
    stream = sys.stderr if output == "json" else sys.stdout
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 40}
    async with AsyncSqliteSaver.from_conn_string(str(CHECKPOINT_DB)) as saver:
        graph = build_graph(saver)
        snapshot = await graph.aget_state(config)
        if existing_id:
            if not snapshot.values:
                raise ValueError("해당 ID의 저장된 조사가 없습니다.")
            state = dict(snapshot.values)
            if state.get("version") not in {4, 5, STATE_VERSION}:
                raise ValueError("이전 구조의 상태입니다. 새 조사를 시작하세요.")
            if args.status:
                display(state, snapshot.next, output=output)
                return 0
            if not snapshot.next:
                if state.get("report"):
                    save_json(Path(state["directory"]) / "report.json", state["report"])
                display(state, snapshot.next, output=output)
                return result_code(state)
            if not (Path(state["directory"]) / "budget.json").exists():
                raise ValueError("예산 파일이 없어 재개할 수 없습니다.")
            graph_input = None
        else:
            state = initial_state(args, thread_id)
            graph_input = state

        invocation_id = uuid4().hex
        record = {
            "run_id": invocation_id,
            "model": state["model"],
            "prompt_version": state.get("prompt_version", "ops_agent_v2"),
            "context_version": f"agent-state-v{state['version']}",
            "request": state["request"],
            "prompt_sha256": state["prompt_sha256"],
            "status": "running",
            "report": None,
            "error": None,
        }
        trace = ReportTrace(
            record,
            {
                "thread_id": thread_id,
                "incident_id": thread_id,
                "service": state["request"]["service"],
                "environment": state["request"]["environment"],
                "investigation_type": state["request"]["investigation_type"],
                "timezone": state["request"]["timezone"],
                "window": state["window"],
                "environment_filter_applied": False,
                "metric_service_filter_applied": False,
            },
            name="ops-agent",
        )
        graph = build_graph(saver, trace=trace)
        print(f"Thread ID: {thread_id}", flush=True, file=stream)
        print(f"결과 폴더: {state['directory']}", flush=True, file=stream)
        display_request(state, file=stream)
        try:
            await graph.ainvoke(
                graph_input,
                config=config,
                durability="sync",
                interrupt_after=["query"] if args.step else [],
            )
            snapshot = await graph.aget_state(config)
            state = dict(snapshot.values)
            paused = bool(snapshot.next)
            record["status"] = "paused" if paused else state["report"]["status"]
            record["report"] = state.get("report")
            record["validation"] = {
                "semantic_review": "pending",
                "stop_reason": state["stop_reason"],
                "decision_error": state.get("decision_error"),
            }
            if state.get("error_type"):
                record["error"] = {"type": state["error_type"]}
            if state.get("report"):
                save_json(Path(state["directory"]) / "report.json", state["report"])
            display(state, snapshot.next, output=output)
            if paused:
                print(
                    f"\n재개: uv run python -m ops_agent.cli.run_agent --resume {thread_id}",
                    file=stream,
                )
            return 0 if paused else result_code(state)
        except BaseException as exc:
            record.update(
                status="interrupted_or_failed", error={"type": type(exc).__name__}
            )
            raise
        finally:
            trace.finish()
            save_json(
                Path(state["directory"]) / f"invocation-{invocation_id}.json", record
            )
            if url := record["telemetry"].get("trace_url"):
                print(f"Langfuse: {url}", file=stream)
