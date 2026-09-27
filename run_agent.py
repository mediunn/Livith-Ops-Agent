"""macOS/Linux 로컬 Agent CLI. 같은 프로젝트의 동시 실행을 제한한다."""

import argparse
import asyncio
import fcntl

from ops_agent.agent.request import request_from_args
from ops_agent.config import AGENT_ROOT
from ops_agent.persistence.session import run

MODELS = ("qwen2.5:3b", "huihui_ai/qwen2.5-abliterate:14b-instruct")


def main() -> int:
    parser = argparse.ArgumentParser(description="Livith 조사 Agent")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--resume", help="재개할 thread_id")
    mode.add_argument("--status", help="상태를 확인할 thread_id")
    parser.add_argument("--service", choices=["livith-server"])
    parser.add_argument(
        "--environment",
        choices=["unspecified"],
        help="환경별 라벨 매핑 확인 전에는 unspecified만 지원",
    )
    parser.add_argument("--investigation-type", choices=["external_api"])
    parser.add_argument("--timezone", help="기본값: Asia/Seoul")
    parser.add_argument("--start", help="ISO 8601 조회 시작 시각")
    parser.add_argument("--end", help="ISO 8601 조회 종료 시각")
    parser.add_argument("--symptom")
    parser.add_argument("--model", choices=MODELS)
    parser.add_argument(
        "--seconds", type=int, help="새 조사 실행 예산. 기본 90초, 중단 시간도 포함"
    )
    parser.add_argument(
        "--step", action="store_true", help="조회 하나 완료 후 저장하고 멈춤"
    )
    args = parser.parse_args()
    new_run_fields = (
        "service",
        "environment",
        "investigation_type",
        "timezone",
        "start",
        "end",
        "symptom",
        "model",
        "seconds",
    )
    if args.resume or args.status:
        supplied = [
            "--" + name.replace("_", "-")
            for name in new_run_fields
            if getattr(args, name) is not None
        ]
        if supplied:
            parser.error(
                "재개·상태 확인에는 저장된 설정을 사용합니다: " + ", ".join(supplied)
            )
        if args.status and args.step:
            parser.error("--status와 --step은 함께 사용할 수 없습니다.")
    else:
        args.model = args.model or MODELS[0]
        args.seconds = 90 if args.seconds is None else args.seconds
        if not 1 <= args.seconds <= 3600:
            parser.error("--seconds는 1~3600이어야 합니다.")
        try:
            args.request = request_from_args(args)
        except ValueError as exc:
            parser.error(str(exc))
    AGENT_ROOT.mkdir(parents=True, exist_ok=True)
    with (AGENT_ROOT / "agent.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("다른 Agent CLI가 실행 중입니다.")
        try:
            return asyncio.run(run(args))
        except KeyboardInterrupt:
            print("\n중단됐습니다. 출력된 Thread ID로 재개할 수 있습니다.")
            return 130
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
