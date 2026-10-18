"""HTTP 기본 관측 후 모델 또는 규칙 planner로 후속 조사를 실행한다."""

import argparse
import asyncio
import fcntl
import json
import re
import sys
from uuid import uuid4
from zoneinfo import ZoneInfoNotFoundError

from ops_agent.agent.http.graph import initial_state
from ops_agent.agent.http.session import run
from ops_agent.agent.request import DEFAULT_TIMEZONE
from ops_agent.cli.query_http import queries_from_args
from ops_agent.config import HTTP_AGENT_ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", help="저장된 HTTP thread_id")
    parser.add_argument("--step", action="store_true", help="조회 하나 후 중단")
    parser.add_argument("--planner", choices=["llm", "rules"])
    parser.add_argument("--model")
    parser.add_argument("--symptom")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--timezone")
    parser.add_argument("--route")
    parser.add_argument(
        "--method", choices=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    )
    parser.add_argument("--seconds", type=int)
    parser.add_argument("--llm-seconds", type=int, help="모델 호출당 제한 (기본 45초)")
    parser.add_argument("--tool-calls", type=int)
    parser.add_argument(
        "--output",
        choices=["answer", "json"],
        default="answer",
        help="사용자 답변 또는 전체 JSON 출력",
    )
    args = parser.parse_args()
    fields = (
        "planner",
        "model",
        "symptom",
        "start",
        "end",
        "timezone",
        "route",
        "method",
        "seconds",
        "llm_seconds",
        "tool_calls",
    )
    if args.resume:
        if not re.fullmatch(r"[0-9a-f]{32}", args.resume):
            parser.error("유효한 HTTP thread_id가 필요합니다.")
        if any(getattr(args, field) is not None for field in fields):
            parser.error("재개에는 저장된 조사 설정을 사용합니다.")
        directory = HTTP_AGENT_ROOT / args.resume
        if not directory.is_dir():
            parser.error("저장된 HTTP 조사를 찾을 수 없습니다.")
        state = None
    else:
        args.timezone = args.timezone or DEFAULT_TIMEZONE
        args.metric = "http_request_rate"
        directory = HTTP_AGENT_ROOT / uuid4().hex
        try:
            scope = queries_from_args(args)[0]
            state = initial_state(
                directory,
                scope,
                symptom=args.symptom or "HTTP 요청률과 평균 지연의 변화 확인",
                planner_kind=args.planner or "llm",
                model=args.model or "qwen2.5:3b",
                seconds=180 if args.seconds is None else args.seconds,
                tool_calls=6 if args.tool_calls is None else args.tool_calls,
                llm_seconds=45 if args.llm_seconds is None else args.llm_seconds,
            )
        except (ValueError, ZoneInfoNotFoundError) as exc:
            parser.error(str(exc))
    with (directory / "run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("같은 HTTP 조사가 이미 실행 중입니다.")
        output_stream = sys.stderr if args.output == "json" else sys.stdout
        print(f"Thread ID: {directory.name}", flush=True, file=output_stream)
        try:
            result = asyncio.run(run(directory, state, step=args.step))
        except KeyboardInterrupt:
            print(
                f"재개: uv run python -m ops_agent.cli.run_http_agent --resume {directory.name}",
                file=output_stream,
            )
            return 130
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
    if args.output == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result["status"] == "paused":
        print("조사를 저장하고 일시 중단했습니다.")

    elif answer := result.get("answer"):
        print(answer["text"])
        if result["status"] == "incomplete":
            print(f"\n조사 미완료: {result['stop_reason']}")

        if answer["evidence_ids"]:
            print("\n근거: " + ", ".join(answer["evidence_ids"]))

        if answer["limitations"]:
            print("\n확인 한계:")
            for limitation in answer["limitations"]:
                print(f"- {limitation}")

    else:
        print("사용자 답변이 생성되지 않았습니다.")
        print(f"종료 사유: {result.get('stop_reason', 'unknown')}")
        print(f"저장된 근거: {len(result.get('facts', []))}건")
        print(f"전체 결과: {directory / 'report.json'}")
    if result["status"] == "paused":
        print(
            f"재개: uv run python -m ops_agent.cli.run_http_agent --resume {directory.name}",
            file=output_stream,
        )
    return 0 if result["status"] in {"completed", "paused"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
