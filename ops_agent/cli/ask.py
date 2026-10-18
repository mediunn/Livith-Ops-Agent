"""자연어 질문을 HTTP 또는 외부 API 조사로 연결하는 공통 CLI."""

import argparse
import asyncio
import fcntl
import sys
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

from ops_agent.agent.budget import initialize_budget, read_budget, remaining_seconds
from ops_agent.agent.contracts import AgentAnswer
from ops_agent.agent.http.graph import initial_state as http_initial_state
from ops_agent.agent.http.session import run as run_http
from ops_agent.agent.request import DEFAULT_TIMEZONE, parse_time, request_from_args
from ops_agent.agent.routing import route_request, validate_route_scope
from ops_agent.cli.presentation import print_result
from ops_agent.config import (
    AGENT_ROOT,
    HTTP_AGENT_ROOT,
    REQUEST_ROOT,
    TOKEN_RESERVATION,
)
from ops_agent.persistence.artifacts import read_json, save_json
from ops_agent.persistence.session import run as run_external
from ops_agent.tools.http import HTTPQuery


def validate_options(args):
    question = args.question.strip()
    if not 1 <= len(question) <= 1000:
        raise ValueError("질문은 1~1000자여야 합니다.")
    if not args.model.strip() or args.model.endswith("-cloud"):
        raise ValueError("로컬 모델 이름이 필요합니다.")
    if not 1 <= args.seconds <= 3600:
        raise ValueError("실행 시간은 1~3600초여야 합니다.")
    ZoneInfo(args.timezone)
    if (args.start is None) != (args.end is None):
        raise ValueError("--start와 --end를 함께 입력하세요.")
    HTTPQuery.validate_route(args.route)
    if args.start is not None:
        HTTPQuery(
            metric="http_request_rate",
            route=args.route,
            method=args.method,
            start_at=parse_time(args.start, args.timezone),
            end_at=parse_time(args.end, args.timezone),
        )
    return question


async def ask(args) -> int:
    question = validate_options(args)
    request_id = uuid4().hex
    directory = REQUEST_ROOT / request_id
    directory.mkdir(parents=True, exist_ok=False)
    initialize_budget(directory)
    reference_time = datetime.now(UTC).replace(microsecond=0)
    routing_state = {
        "directory": str(directory),
        "model": args.model,
        "deadline": time.time() + args.seconds,
        "limits": {
            "tool_calls": 0,
            "llm_calls": 2,
            "reserved_tokens": 2 * TOKEN_RESERVATION,
        },
    }
    explicit = {
        name: getattr(args, name)
        for name in (
            "domain",
            "route",
            "method",
            "start",
            "end",
            "timezone",
            "compare_previous",
        )
    }
    explicit["reference_time"] = reference_time.isoformat()
    record = {
        "request_id": request_id,
        "question": question,
        "explicit": explicit,
        "status": "routing",
        "route": None,
        "investigation_id": None,
    }
    print(f"Request ID: {request_id}", file=sys.stderr, flush=True)
    save_json(directory / "request.json", record)
    try:
        plan = await route_request(routing_state, question, explicit)
        validate_route_scope(plan, question, explicit)
        output = args.output or plan.output_format
        record["route"] = plan.model_dump(mode="json")
        if plan.action != "proceed":
            result = {
                "status": "needs_input" if plan.action == "clarify" else "unsupported",
                "stop_reason": plan.action,
                "request_id": request_id,
                "answer": AgentAnswer(
                    text=plan.question or plan.unsupported_reason
                ).model_dump(mode="json"),
            }
            record["status"] = result["status"]
            print_result(result, output=output, directory=directory)
            return 0 if plan.action == "clarify" else 1
        if args.start is not None:
            start, end = (
                parse_time(args.start, args.timezone),
                parse_time(args.end, args.timezone),
            )
        else:
            end = reference_time
            start = end - timedelta(minutes=plan.lookback_minutes or 30)
        thread_id = uuid4().hex
        record.update(status="investigating", investigation_id=thread_id)
        domain_root = HTTP_AGENT_ROOT if plan.domain == "http" else AGENT_ROOT
        record["investigation_directory"] = str(domain_root / thread_id)
        save_json(directory / "request.json", record)
        if plan.domain == "http":
            scope = HTTPQuery(
                metric="http_request_rate",
                start_at=start,
                end_at=end,
                route=args.route or plan.route,
                method=args.method or plan.method,
            )
            target = HTTP_AGENT_ROOT / thread_id
            state = http_initial_state(
                target,
                scope,
                symptom=question,
                model=args.model,
                seconds=remaining_seconds(routing_state),
            )
            with (target / "run.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = await run_http(target, state, step=args.step)
            print_result(result, output=output, directory=target)
            if result["status"] == "paused":
                print(
                    f"재개: uv run python -m ops_agent.cli.run_http_agent --resume {thread_id}",
                    file=sys.stderr,
                )
            record["status"] = result["status"]
            return 0 if result["status"] in {"completed", "paused"} else 1
        external_args = SimpleNamespace(
            resume=None,
            status=None,
            step=args.step,
            output=output,
            thread_id=thread_id,
            model=args.model,
            seconds=remaining_seconds(routing_state),
            symptom=question,
            start=start.isoformat(),
            end=end.isoformat(),
            timezone=args.timezone,
            compare_previous=plan.compare_previous
            if args.compare_previous is None
            else args.compare_previous,
        )
        external_args.request = request_from_args(external_args)
        AGENT_ROOT.mkdir(parents=True, exist_ok=True)
        with (AGENT_ROOT / "agent.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code = await run_external(external_args)
        report_path = AGENT_ROOT / thread_id / "report.json"
        record["status"] = (
            read_json(report_path)["status"] if report_path.exists() else "paused"
        )
        return code
    except BaseException as exc:
        record.update(status="interrupted_or_failed", error_type=type(exc).__name__)
        raise
    finally:
        record["routing_budget"] = read_budget(routing_state)
        save_json(directory / "request.json", record)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question", required=True)
    parser.add_argument("--domain", choices=["http", "external_api"])
    parser.add_argument("--route")
    parser.add_argument(
        "--method", choices=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    )
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument(
        "--compare-previous",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="외부 API의 필수 직전 비교 설정. HTTP 비교 목적은 질문에 지정",
    )
    parser.add_argument("--model", default="qwen2.5:3b")
    parser.add_argument("--seconds", type=int, default=180)
    parser.add_argument("--output", choices=["answer", "json", "report"])
    parser.add_argument("--step", action="store_true")
    args = parser.parse_args()
    try:
        return asyncio.run(ask(args))
    except KeyboardInterrupt:
        print(
            "중단됐습니다. 저장된 요청 기록에서 조사 ID를 확인하세요.", file=sys.stderr
        )
        return 130
    except (ValueError, RuntimeError, OSError, TimeoutError, KeyError) as exc:
        print(f"요청 처리 실패: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
