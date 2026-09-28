# 실행 안내

[프로젝트 소개](../README.md) · [조사 구조](architecture/agent-workflow.md)

프로젝트 루트에서 실행합니다. `.env`에는 `GRAFANA_URL`과
`GRAFANA_SERVICE_ACCOUNT_TOKEN`이 필요합니다. 기본 Ollama 주소는 `http://127.0.0.1:11434`입니다.
HTTP 데이터소스·job과 Ollama 주소는 [config.py](../ops_agent/config.py)에서 설정합니다.

## HTTP 조사

```bash
uv run python -m ops_agent.cli.run_http_agent \
  --method GET --route '/api/v7/recommendation//concerts' \
  --symptom '평균 지연이 직전 구간보다 증가했는지 확인'

# 규칙 planner로 같은 실행 경로 확인
uv run python -m ops_agent.cli.run_http_agent --planner rules

# 조회 하나 후 저장하고 재개
uv run python -m ops_agent.cli.run_http_agent --step
uv run python -m ops_agent.cli.run_http_agent --resume THREAD_ID
```

`THREAD_ID`는 첫 실행에서 출력한 ID입니다. 규칙 planner는 관측된 최대 평균 지연이 가장 큰
엔드포인트의 직전 지연·요청률을 조회한 뒤 종료합니다. 자연어 증상은 해석하지 않습니다.

| 옵션 | 기본값·범위 |
|---|---|
| `--planner` | `llm` 또는 `rules`, 기본 `llm` |
| `--model` | `qwen2.5:3b`, 설치된 로컬 모델 지정 |
| `--route`, `--method` | 생략하면 전체. 실제 라벨 값에 정확히 매칭 |
| `--seconds` | 조사 실행 예산 180초, 1~3,600초 |
| `--llm-seconds` | 모델 호출당 45초, 1~180초. 남은 조사 시간을 함께 적용 |
| `--tool-calls` | 초기 두 조회를 포함해 6회, 2~10회 |

정상적인 `--step` 중단 시간은 HTTP 실행 예산에서 제외합니다. 재개할 때는 저장된 설정을
사용하므로 새 모델·대상·예산 옵션을 함께 지정할 수 없습니다. 실행·재개 한 번에 MCP 세션 하나를 공유합니다.

결과는 `artifacts/http-agent/<thread_id>/report.json`에 저장합니다.

| 필드 | 내용 |
|---|---|
| `facts` | 파싱한 관측값, 조회 조건, 단위와 근거 ID |
| `decisions` | 후속 조회·종료 선택과 당시 가설 |
| `interpretation.hypotheses` | 마지막으로 반영된 모델 가설 |
| `interpretation.unreviewed_evidence_ids` | 마지막 판단 이후 추가된 근거 |
| `status`, `stop_reason` | 완료·미완료 상태와 종료 사유 |
| `budget`, `limits` | 사용한 예산과 설정한 상한 |

후속 조회 후보는 초기 관측의 최대 평균 지연이 높은 상위 12개 엔드포인트와 두 지표,
직전·앞 절반·뒤 절반 구간입니다. 10분 미만 구간에는 절반 조회를 제공하지 않습니다.
대상을 명확히 지정하려면 `--route`와 `--method`를 사용합니다.

## 조회 구간

기본값은 최근 30분입니다. `--start`와 `--end`는 함께 지정하며 HTTP는 최대 24시간,
외부 API는 최대 6시간의 과거 구간을 지원합니다.

```bash
uv run python -m ops_agent.cli.run_http_agent \
  --start '2026-09-26T22:00:00' --end '2026-09-26T23:00:00' \
  --timezone Asia/Seoul --symptom 'HTTP 요청률과 평균 지연 변화 확인'
```

오프셋 없는 시각은 `--timezone`(기본 `Asia/Seoul`)으로 해석하고 UTC로 저장합니다.
오프셋이 있으면 해당 값을 우선합니다. 증상 문장에서 시간 범위를 자동 추출하지 않습니다.

## HTTP 도구 직접 실행

모델 호출 없이 요청률과 평균 지연을 조회합니다.

```bash
uv run python -m ops_agent.cli.query_http
uv run python -m ops_agent.cli.query_http \
  --metric http_mean_latency --method GET --route '/api/v7/recommendation//concerts'
```

`--metric`은 `all`(기본), `http_request_rate`, `http_mean_latency` 중 하나입니다.
시간·대상 옵션은 HTTP 조사와 같고, 연결을 포함한 `--seconds` 기본값은 90초입니다.
결과는 매번 새로 생성되는 `artifacts/http/<run_id>/`에 저장합니다.

HTTP 대상 기본값은 데이터소스 `grafanacloud-prom`, job `livith-server-production`입니다.
route의 `//`나 `:id`를 정규화하지 않습니다. 요청률은 req/s, 지연은 5분 요청 가중 평균 ms입니다.
평가 간격은 60초이며 `sample_count`는 평가 시점 수입니다. 빈 결과와 유효한 0은 구분합니다.

## 외부 API 조사

```bash
uv run python -m ops_agent.cli.run_agent --symptom '외부 API 요청률과 관련 로그 확인'
uv run python -m ops_agent.cli.run_agent --no-compare-previous
uv run python -m ops_agent.cli.run_agent --seconds 600 --step
uv run python -m ops_agent.cli.run_agent --status THREAD_ID
uv run python -m ops_agent.cli.run_agent --resume THREAD_ID
```

현재 지표·직전 지표·서비스 로그가 기본 필수 조회입니다. `--no-compare-previous`로 직전 비교를
제외할 수 있습니다. 추가 경고 문자열 조회는 로그 완전성 정책과 모델 선택에 따라 실행합니다.
서비스 옵션은 `livith-server`, 환경은 `unspecified`, 조사 유형은 `external_api`만 지원합니다.
현재 외부 API 지표에는 서비스·환경 필터가 없으며 로그 job은 `livith-server`입니다.

외부 API의 시간 예산은 기본 90초이며 **중단 시간도 포함**합니다. 재개·상태 확인에는
저장된 설정을 사용합니다. 지원 상태 버전은 v4이며 결과는 `artifacts/agent/<thread_id>/`에 저장합니다.

## 고정 수집·보고서

정해진 순서로 지표·로그를 수집하고 모델 보고서를 생성하는 별도 실행 경로입니다.

```bash
uv run python -m ops_agent.cli.run_investigation
```

관측값 검증과 자유 서술 품질은 [고정 보고서 평가](../evals/results/report/v3-validation.md)에서
확인할 수 있습니다. 추적을 켜려면 [Langfuse 설정](setup/langfuse.md)을 참고하세요.
