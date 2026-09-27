"""macOS/Linux 로컬 Agent CLI. 같은 프로젝트의 동시 실행을 제한한다."""

import argparse
import asyncio
import fcntl

from ops_agent.config import AGENT_ROOT
from ops_agent.persistence.session import run

MODELS = ("qwen2.5:3b", "huihui_ai/qwen2.5-abliterate:14b-instruct")


def main() -> int:
    parser = argparse.ArgumentParser(description="Livith 조사 Agent")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--resume", help="재개할 thread_id")
    mode.add_argument("--status", help="상태를 확인할 thread_id")
    parser.add_argument(
        "--symptom", default="최근 외부 API 요청률과 로그의 추가 조사 필요성 확인"
    )
    parser.add_argument("--model", choices=MODELS, default="qwen2.5:3b")
    parser.add_argument(
        "--seconds", type=int, default=90, help="새 조사 시간 예산. 중단 시간도 포함"
    )
    parser.add_argument(
        "--step", action="store_true", help="조회 하나 완료 후 저장하고 멈춤"
    )
    args = parser.parse_args()
    if not 1 <= args.seconds <= 3600:
        parser.error("--seconds는 1~3600이어야 합니다.")
    if not 1 <= len(args.symptom.strip()) <= 1000:
        parser.error("--symptom은 1~1000자여야 합니다.")
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
