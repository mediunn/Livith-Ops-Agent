"""Agent 전체 루프 평가 CLI."""

import asyncio

from ops_agent.evaluation.agent import main

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
