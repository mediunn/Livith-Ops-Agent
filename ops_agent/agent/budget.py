import time
from pathlib import Path

from ops_agent.config import TOKEN_RESERVATION
from ops_agent.persistence.artifacts import read_json, save_json


class BudgetExceeded(RuntimeError):
    pass


def read_budget(state: dict) -> dict:
    return read_json(Path(state["directory"]) / "budget.json")


def initialize_budget(directory: Path) -> None:
    path = directory / "budget.json"
    if path.exists():
        raise ValueError("기존 예산을 초기화할 수 없습니다.")

    save_json(
        path,
        {
            "tool_calls": 0,
            "llm_calls": 0,
            "reserved_tokens": 0,
        },
    )


def remaining_seconds(state: dict) -> float:
    remaining = state["deadline"] - time.time()
    if remaining <= 0:
        raise BudgetExceeded("time_budget")
    return remaining


def reserve(state: dict, kind: str) -> None:
    budget = read_budget(state)
    limits = state["limits"]

    if kind == "tool":
        if budget["tool_calls"] >= limits["tool_calls"]:
            raise BudgetExceeded("tool_budget")
        budget["tool_calls"] += 1

    elif kind == "llm":
        if budget["llm_calls"] >= limits["llm_calls"]:
            raise BudgetExceeded("llm_budget")

        reserved = budget["reserved_tokens"] + TOKEN_RESERVATION
        if reserved > limits["reserved_tokens"]:
            raise BudgetExceeded("token_budget")

        budget["llm_calls"] += 1
        budget["reserved_tokens"] = reserved

    else:
        raise ValueError(f"지원하지 않는 예산 종류: {kind}")

    save_json(Path(state["directory"]) / "budget.json", budget)
