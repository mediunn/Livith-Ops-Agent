import asyncio
import time
from contextlib import AsyncExitStack
from pathlib import Path
from uuid import uuid4

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from mcp import Client

from ops_agent.agent.budget import read_budget
from ops_agent.agent.http.graph import build_graph
from ops_agent.agent.http.planner import llm_planner, rule_planner
from ops_agent.agent.http.state import CONTRACT_VERSION
from ops_agent.collectors.grafana import create_grafana_server
from ops_agent.persistence.artifacts import save_json
from ops_agent.tools.http import execute_http_query


async def dispatch_planner(state, context):
    return await {"llm": llm_planner, "rules": rule_planner}[state["planner_kind"]](
        state, context
    )


async def run(
    directory: Path, state=None, *, step=False, planner=None, tool_executor=None
) -> dict:
    config = {"configurable": {"thread_id": directory.name}, "recursion_limit": 40}
    started = time.monotonic()
    invocation = {"status": "running", "error_type": None}
    async with AsyncExitStack() as stack:
        client = None
        connection_error = None

        async def collect(active, query):
            if connection_error is not None:
                raise connection_error
            return await execute_http_query(active, query, client=client)

        saver = await stack.enter_async_context(
            AsyncSqliteSaver.from_conn_string(str(directory / "checkpoints.sqlite"))
        )
        graph = build_graph(
            saver,
            planner=planner or dispatch_planner,
            tool_executor=tool_executor or collect,
        )
        snapshot = await graph.aget_state(config)
        if state is None:
            if not snapshot.values:
                raise ValueError("저장된 HTTP 조사를 찾을 수 없습니다.")
            saved = snapshot.values
        else:
            if snapshot.values:
                raise ValueError("기존 조사에 초기 상태를 다시 적용할 수 없습니다.")
            saved = state
        if saved.get("contract_version") != CONTRACT_VERSION:
            raise ValueError("지원하지 않는 HTTP 조사 상태입니다.")
        if (
            saved.get("directory") != str(directory)
            or saved.get("thread_id") != directory.name
        ):
            raise ValueError("저장된 조사 경로가 일치하지 않습니다.")
        try:
            if state is not None or snapshot.next:
                if tool_executor is None and saved["remaining_seconds"] > 0:
                    # MCP의 AnyIO cancel scope는 진입·종료를 같은 task에서 해야 한다.
                    # LangGraph 노드 task 안에서는 세션을 만들지 않는다.
                    connected_at = time.monotonic()
                    try:
                        async with asyncio.timeout(
                            min(20.0, saved["remaining_seconds"])
                        ):
                            client = await stack.enter_async_context(
                                Client(create_grafana_server())
                            )
                    except Exception as exc:  # noqa: BLE001
                        connection_error = exc
                    remaining = max(
                        0.0,
                        saved["remaining_seconds"] - (time.monotonic() - connected_at),
                    )
                    if state is not None:
                        state = {**state, "remaining_seconds": remaining}
                    else:
                        await graph.aupdate_state(
                            config, {"remaining_seconds": remaining}
                        )
                await graph.ainvoke(
                    state,
                    config=config,
                    durability="sync",
                    interrupt_after=["query"] if step else [],
                )
            snapshot = await graph.aget_state(config)
            saved = dict(snapshot.values)
            if snapshot.next:
                result = {
                    "status": "paused",
                    "thread_id": directory.name,
                    "next_nodes": list(snapshot.next),
                    "remaining_seconds": saved["remaining_seconds"],
                    "evidence_count": len(saved["evidence"]),
                    "budget": read_budget(saved),
                }
            else:
                result = saved["report"]
                save_json(directory / "report.json", result)
            invocation["status"] = result["status"]
            return result
        except BaseException as exc:
            invocation.update(
                status="interrupted_or_failed", error_type=type(exc).__name__
            )
            raise
        finally:
            invocation["elapsed_seconds"] = round(time.monotonic() - started, 3)
            save_json(directory / f"invocation-{uuid4().hex}.json", invocation)
