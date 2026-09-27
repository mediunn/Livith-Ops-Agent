"""SQLite 조사 세션을 열고 실행별 추적·결과 파일을 관리한다."""

import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from ops_agent.agent.budget import initialize_budget
from ops_agent.agent.graph import build_graph
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
    prompt = AGENT_PROMPT_PATH.read_text(encoding="utf-8").strip()
    if not prompt:
        raise ValueError("Agent 프롬프트가 비어 있습니다.")
    directory = AGENT_ROOT / thread_id
    directory.mkdir(parents=True, exist_ok=False)
    initialize_budget(directory)
    end = datetime.now(UTC).replace(microsecond=0)
    return {
        "version": STATE_VERSION,
        "thread_id": thread_id,
        "model": args.model,
        "symptom": args.symptom.strip(),
        "directory": str(directory),
        "window": {
            "start": (end - timedelta(minutes=30)).isoformat(),
            "end": end.isoformat(),
        },
        "deadline": time.time() + args.seconds,
        "limits": {"tool_calls": 6, "llm_calls": 6, "reserved_tokens": 110000},
        "prompt": prompt,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "action": "",
        "evidence": [],
        "decisions": [],
        "stop_reason": "",
        "error_type": None,
        "report": None,
    }


def display(state: dict, pending) -> None:
    print(f"Thread ID: {state['thread_id']}")
    print(f"다음 노드: {list(pending)}")
    print(f"근거 수: {len(state['evidence'])}, 모델 판단 수: {len(state['decisions'])}")
    print(f"결과 폴더: {state['directory']}")
    if state.get("report"):
        print(json.dumps(state["report"], ensure_ascii=False, indent=2))


def result_code(state: dict) -> int:
    report = state.get("report")
    return 0 if report and report["status"] == "completed" else 1


async def run(args) -> int:
    existing_id = args.resume or args.status
    thread_id = existing_id or uuid4().hex
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 40}
    async with AsyncSqliteSaver.from_conn_string(str(CHECKPOINT_DB)) as saver:
        graph = build_graph(saver)
        snapshot = await graph.aget_state(config)
        if existing_id:
            if not snapshot.values:
                raise ValueError("해당 ID의 저장된 조사가 없습니다.")
            state = dict(snapshot.values)
            if state.get("version") != STATE_VERSION:
                raise ValueError("이전 구조의 상태입니다. 새 조사를 시작하세요.")
            if args.status:
                display(state, snapshot.next)
                return 0
            if not snapshot.next:
                if state.get("report"):
                    save_json(Path(state["directory"]) / "report.json", state["report"])
                display(state, snapshot.next)
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
            "prompt_version": PROMPT_VERSION,
            "context_version": f"agent-state-v{STATE_VERSION}",
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
                "service": "livith-server",
                "environment": "unspecified",
                "environment_filter_applied": False,
            },
            name="ops-agent",
        )
        graph = build_graph(saver, trace=trace)
        print(f"Thread ID: {thread_id}", flush=True)
        print(f"결과 폴더: {state['directory']}", flush=True)
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
            }
            if state.get("error_type"):
                record["error"] = {"type": state["error_type"]}
            if state.get("report"):
                save_json(Path(state["directory"]) / "report.json", state["report"])
            display(state, snapshot.next)
            if paused:
                print(f"\n재개: uv run python run_agent.py --resume {thread_id}")
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
                print(f"Langfuse: {url}")
