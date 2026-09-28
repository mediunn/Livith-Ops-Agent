import argparse
import asyncio
import json
from uuid import uuid4

from ops_agent.config import ARTIFACTS_DIR
from ops_agent.evaluation.http_agent import CASES, evaluate


def main():
    parser = argparse.ArgumentParser(
        description="합성 HTTP 관측으로 규칙과 로컬 LLM의 선택 비교"
    )
    parser.add_argument("--planner", choices=["rules", "llm", "both"], default="rules")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--model", default="qwen2.5:3b")
    group.add_argument("--models", nargs="+")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--seconds", type=int, default=180)
    parser.add_argument("--llm-seconds", type=int, default=45)
    parser.add_argument("--case", action="append", choices=[c["id"] for c in CASES])
    parser.add_argument(
        "--warmup", action="store_true", help="모델 사전 로딩을 조사 예산 밖에서 실행"
    )
    args = parser.parse_args()
    root = ARTIFACTS_DIR / "evaluations" / "http-agent" / uuid4().hex
    planners = ("rules", "llm") if args.planner == "both" else (args.planner,)
    print(f"평가 디렉터리: {root}", flush=True)
    try:
        result = asyncio.run(
            evaluate(
                root,
                planners=planners,
                model=args.model,
                models=args.models,
                repeats=args.repeat,
                seconds=args.seconds,
                llm_seconds=args.llm_seconds,
                warmup=args.warmup,
                cases=[c for c in CASES if not args.case or c["id"] in args.case],
                on_progress=lambda event: print(
                    json.dumps(event, ensure_ascii=False), flush=True
                ),
            )
        )
    except ValueError as exc:
        parser.error(str(exc))
    except KeyboardInterrupt:
        print(f"평가 중단. 완료된 사례는 {root / 'summary.json'}에 보존했습니다.")
        return 130
    print(json.dumps(result["aggregate"], ensure_ascii=False, indent=2))
    print(f"평가 결과: {root / 'summary.json'}")
    return 0 if all(all(row["checks"].values()) for row in result["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
