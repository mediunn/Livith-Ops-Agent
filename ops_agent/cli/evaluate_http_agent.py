import argparse
import asyncio
import json
from uuid import uuid4

from ops_agent.config import ARTIFACTS_DIR
from ops_agent.evaluation.http_agent import evaluate


def main():
    parser = argparse.ArgumentParser(
        description="합성 HTTP 관측으로 규칙과 로컬 LLM의 선택 비교"
    )
    parser.add_argument("--planner", choices=["rules", "llm", "both"], default="rules")
    parser.add_argument("--model", default="qwen2.5:3b")
    args = parser.parse_args()
    root = ARTIFACTS_DIR / "evaluations" / "http-agent" / uuid4().hex
    planners = ("rules", "llm") if args.planner == "both" else (args.planner,)
    result = asyncio.run(evaluate(root, planners=planners, model=args.model))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"평가 결과: {root / 'summary.json'}")
    return 0 if all(all(row["checks"].values()) for row in result["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
