# Livith OpsAgent

**Grafana 지표와 로그를 수집하고, 근거를 보존하며 운영 문제를 조사하는 로컬 에이전트.**

외부 API 요청률·서비스 로그 조사와 HTTP 엔드포인트별 후속 조사를 지원합니다.
Grafana MCP로 데이터를 읽고, LangGraph와 SQLite로 조사 상태를 저장합니다.
HTTP 조사에서는 로컬 Ollama 모델이 후속 조회 대상·지표·구간 또는 종료를 선택합니다.

## 지원 기능

| 조사 | 수집 대상 | 판단 방식 |
|---|---|---|
| 외부 API | 현재·직전 요청률, 서비스 로그, 선택적 경고 문자열 로그 | 필수 조회와 관측 설명은 코드가 보장하고 모델은 허용된 조회·관측을 선택 |
| HTTP | 메서드·엔드포인트별 요청률과 평균 지연 | 초기 두 조회 이후 모델이 후속 조회와 근거를 인용한 가설을 작성 |

조회 조건별 캐시, 원본 응답 보존, 호출·시간 예산, 중단·재개를 지원합니다.
보고서는 파싱된 관측과 모델 해석을 구분합니다.
외부 API 조사에서는 필수 조회 후 저장된 로그 샘플을 최대 2페이지 읽어 다음 판단에 활용할 수 있습니다.

## 결과 예시

실제 Grafana 데이터를 규칙 planner로 조사한 HTTP 보고서의 실행 정보 발췌입니다.
현재·직전 요청률과 평균 지연을 조회했으며, 네 조회 모두 데이터를 반환했습니다.

```json
{
  "report_version": "http-agent-v1",
  "status": "completed",
  "stop_reason": "planner_finished",
  "budget": {
    "tool_calls": 4,
    "llm_calls": 0,
    "reserved_tokens": 0
  }
}
```

전체 보고서에는 `facts`(관측), `decisions`(선택 이력), `interpretation`(가설·종료 이유)이
포함됩니다. [실행 검증과 모델 평가](evals/results/agent/http-agent-validation.md)에서 확인 범위를 볼 수 있습니다.

## 설계 이유

- **실행 범위 제한:** 읽기 전용 MCP와 검증된 쿼리 템플릿으로 조사 범위를 유지합니다.
- **사실과 해석 분리:** 수치는 파서에서 가져오고, 가설에는 근거 ID를 연결합니다.
- **다시 확인할 수 있는 조사:** 조회 원본·판단·체크포인트를 저장해 재개와 결과 검토를 지원합니다.

## 시작하기

Python 3.13 이상, uv, 로컬 Ollama, Grafana 읽기 전용 서비스 계정이 필요합니다.
조사 CLI는 macOS/Linux를 지원합니다. 기본 모델은 `qwen2.5:3b`입니다.

```bash
uv sync
```

[.env.example](.env.example)을 참고해 `.env`에 Grafana URL과 토큰을 설정하고,
Ollama에 기본 모델을 준비합니다. HTTP 데이터소스와 job은 [config.py](ops_agent/config.py)에서 확인합니다.

```bash
# HTTP 조사
uv run python -m ops_agent.cli.run_http_agent \
  --symptom '/api/v7/recommendation//concerts 평균 지연이 직전 구간보다 증가했는지 확인'

# 외부 API 요청률과 서비스 로그 조사
uv run python -m ops_agent.cli.run_agent --symptom '외부 API 요청률과 관련 로그 확인'

# 모델 없이 HTTP 지표 수집
uv run python -m ops_agent.cli.query_http
```

기본 조회 구간은 최근 30분입니다. [실행 안내](docs/usage.md)에 시간·대상 지정, 규칙 planner,
중단·재개와 결과 파일 위치를 정리했습니다.

## 현재 범위와 검증

외부 API·HTTP의 수집, 실행 제어, 근거 저장과 평가 경로가 구현되어 있습니다.
실제 Grafana 수집·재개를 확인했으며, HTTP 모델의 가설 갱신과 종료 판단은 검증 중입니다.
외부 API 조사는 관측 정책에 따른 결과를 제공하고, 자동 원인 분석이나 서비스 정상 판정은 하지 않습니다.
HTTP는 요청률·평균 지연을 지원하며 오류율·p95/p99·Sentry·백엔드 코드 조사는 지원 범위 밖입니다.
`completed`는 실행 완료 상태입니다. 모델 가설의 정확성은 [내부 평가 결과](evals/README.md)와 구분해 확인합니다.
Langfuse 추적은 외부 API·고정 보고서 경로에서 지원하며 HTTP 기록은 로컬에 저장합니다.

## 개발 검증

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

테스트는 합성 응답을 사용하며 Grafana·Ollama 호출과 Langfuse 전송 없이 실행합니다.

[실행 안내](docs/usage.md) · [조사 구조](docs/architecture/agent-workflow.md) ·
[평가 방법과 결과](evals/README.md) · [Langfuse 설정](docs/setup/langfuse.md)
