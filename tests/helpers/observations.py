"""정책과 후속 조회 테스트에 사용하는 합성 관측."""

from ops_agent.evaluation.agent import CASES_PATH, synthetic_observation
from ops_agent.persistence.artifacts import read_json

CASES = read_json(CASES_PATH)["cases"]


def observe(state, actions, case_id="zero_no_logs"):
    case = next(c for c in CASES if c["id"] == case_id)
    state["evidence"] = [synthetic_observation(state, case, a) for a in actions]
