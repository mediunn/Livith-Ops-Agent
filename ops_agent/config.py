from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
ARTIFACTS_DIR = PROJECT_DIR / "artifacts"

AGENT_ROOT = ARTIFACTS_DIR / "agent"
CHECKPOINT_DB = AGENT_ROOT / "checkpoints.sqlite"

AGENT_PROMPT_PATH = PROJECT_DIR / "prompts" / "agent" / "ops_agent_v6.txt"
REPORT_PROMPT_DIR = PROJECT_DIR / "prompts" / "report"

PROMPT_VERSION = "ops_agent_v6"
STATE_VERSION = 3

NUM_CTX = 16384
NUM_PREDICT = 1024
TOKEN_RESERVATION = NUM_CTX + NUM_PREDICT
