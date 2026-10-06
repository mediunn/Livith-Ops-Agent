from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
ARTIFACTS_DIR = PROJECT_DIR / "artifacts"

AGENT_ROOT = ARTIFACTS_DIR / "agent"
CHECKPOINT_DB = AGENT_ROOT / "checkpoints.sqlite"
HTTP_ROOT = ARTIFACTS_DIR / "http"
HTTP_DATASOURCE_UID = "grafanacloud-prom"
HTTP_JOB = "livith-server-production"
HTTP_AGENT_ROOT = ARTIFACTS_DIR / "http-agent"
HTTP_AGENT_PROMPT_PATH = PROJECT_DIR / "prompts" / "agent" / "http_agent_v1.txt"
OLLAMA_HOST = "http://127.0.0.1:11434"

AGENT_PROMPT_PATH = PROJECT_DIR / "prompts" / "agent" / "ops_agent_v9.txt"
REPORT_PROMPT_DIR = PROJECT_DIR / "prompts" / "report"

PROMPT_VERSION = "ops_agent_v9"
STATE_VERSION = 5

NUM_CTX = 16384
NUM_PREDICT = 1024
TOKEN_RESERVATION = NUM_CTX + NUM_PREDICT
