from pathlib import Path

import pytest

from ops_agent.agent.budget import (
    BudgetExceeded,
    initialize_budget,
    read_budget,
    reserve,
)
from ops_agent.config import TOKEN_RESERVATION


@pytest.mark.parametrize(
    "kind,limit,reason",
    [
        ("tool", "tool_calls", "tool_budget"),
        ("llm", "llm_calls", "llm_budget"),
        ("llm", "reserved_tokens", "token_budget"),
    ],
)
def test_budgets_persist_without_refund(state, kind, limit, reason):
    state["limits"][limit] = TOKEN_RESERVATION if limit == "reserved_tokens" else 1
    reserve(state, kind)
    with pytest.raises(BudgetExceeded, match=reason):
        reserve(state, kind)
    budget = read_budget(state)
    assert budget[f"{kind}_calls"] == 1
    with pytest.raises(ValueError, match="초기화"):
        initialize_budget(Path(state["directory"]))
