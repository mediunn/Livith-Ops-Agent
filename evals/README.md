# 평가 방법과 결과

[프로젝트 소개](../README.md) · [문서 목록](../docs/README.md)

실행 제어, 조회 선택, 관측값 보존과 모델 해석을 구분해 평가합니다.
현재 결과는 **개발용 합성 사례를 이용한 내부 평가**입니다. 자동 조건 통과율과 가설 내용의
정확성은 별도로 다루며, 독립 평가 세트의 일반 성능 수치로 해석하지 않습니다.

| 결과 | 확인 범위 |
|---|---|
| [외부 API](results/agent/agent-query-efficiency.md) | 필수 조회·추가 로그 정책·관측 보존과 실제 Grafana 실행 |
| [HTTP](results/agent/http-agent-validation.md) | 실제 수집·재개, 대상 선택·가설·종료 판단 |
| [고정 보고서](results/report/v3-validation.md) | 관측값 대조와 분류·가설 정책 |

## HTTP 조사

합성 MCP 응답을 실제 실행기·파서·LangGraph·SQLite에 통과시킵니다.
`llm`은 로컬 Ollama를 호출하며, Grafana 연결은 사용하지 않습니다.

```bash
uv run python -m ops_agent.cli.evaluate_http_agent --planner rules
uv run python -m ops_agent.cli.evaluate_http_agent --planner both --model qwen2.5:3b

# 같은 설정으로 모델별 반복 평가
uv run python -m ops_agent.cli.evaluate_http_agent --planner llm \
  --models qwen2.5:3b huihui_ai/qwen2.5-abliterate:14b-instruct \
  --repeat 2 --seconds 300 --llm-seconds 90 --warmup
```

[evaluation/http_agent.py](../ops_agent/evaluation/http_agent.py)에 다음 네 사례가 정의되어 있습니다.
기대 결과는 모델 입력에 포함하지 않습니다.

| 사례 | 관측 | 검사 목적 |
|---|---|---|
| `latency_increase` | checkout 지연 100 → 800ms | 증상 대상·직전 지연 선택 |
| `traffic_drop` | orders 요청률 8 → 0 req/s | 0인 대상 선택과 감소 비교 |
| `contradicted_latency_increase` | checkout 지연 800 → 800ms | 증가 가설 반박 |
| `no_data` | 지표 없음 | 불필요한 조회·모델 호출 없는 종료 |

비교 사례에는 지연이 큰 내부 대기 엔드포인트를 함께 넣습니다. 규칙 기준선은 이 엔드포인트를
고르므로 증상을 해석하는 강한 기준선은 아닙니다. 빈 데이터는 코드가 처리하며 모델 비교 분모에서 제외합니다.

`--case`는 반복 지정할 수 있습니다. 기본값은 반복 1회·조사 180초·모델 호출 45초입니다.
`--repeat`는 최대 10회, `--seconds`는 최대 3,600초, `--llm-seconds`는 최대 180초입니다.
모델은 순차 실행하고 반복마다 순서를 순환합니다. `--warmup`은 조사 예산 밖에서 사전 로딩합니다.

| 검사·기록 | 의미 |
|---|---|
| `checks` | 완료, 중복 없음, 예산 준수, 필요한 직전 조회 |
| `hypothesis_status_match` | 마지막 가설의 예상 상태와 양쪽 구간 근거 ID 인용 |
| `outcome` | 시간 초과, 잘못된 선택, 비교 누락 등 종료 결과 |
| `semantic_review` | 별도 내용 검토 상태. 기본 `pending` |
| `metrics` | 호출별 제한·실제 시간, 서버 시간, 수신 토큰과 미수신 호출 수 |

`hypothesis_status_match`는 가설 문장의 의미를 검사하지 않으며 미완료 보고서에서도 계산합니다.
`checks` 실패 시 CLI는 1을 반환합니다. 가설 상태·인용 진단만으로 종료 코드를 결정하지 않습니다.
사용량을 받지 못한 호출은 미상으로 기록하며, 수신 토큰 합계를 전체 소비량으로 간주하지 않습니다.

결과는 `artifacts/evaluations/http-agent/<ID>/`에 저장됩니다. `metadata.json`은 사례·예산·
생성 옵션·소스 해시를, `summary.json`은 개별 결과와 집계를 담습니다. 사례마다 저장하여
중단 시 완료된 결과를 보존합니다. 전체 실행 완료 여부는 `status`로 확인합니다.
근거 UUID와 로컬 부하는 실행마다 달라질 수 있습니다.

## 외부 API 조사

```bash
uv run python -m ops_agent.cli.evaluate_agent
```

[외부 API 데이터셋](datasets/agent/agent-cases.json)의 합성 관측으로 실제 모델·planner·
그래프·SQLite·보고서 경로를 실행합니다. 필수 조회 이행, 관측 보존, 추가 로그 조회의 수행·생략,
도구 중복과 예산을 검사합니다. 결과는 `artifacts/evaluations/agent-<ID>/`와
`artifacts/agent/eval-<ID>/`에 저장됩니다. Langfuse를 활성화하면 합성 평가 표시와 자동 점수를 전송합니다.

## 고정 보고서

```bash
uv run python -m ops_agent.cli.evaluate_reports --dry-run
uv run python -m ops_agent.cli.evaluate_reports --prompt-versions ops_report_v3
uv run python -m ops_agent.cli.evaluate_reports --case warning_present
```

[보고서 데이터셋](datasets/report/cases.json)의 여섯 사례로 구조·근거 ID·관측값·분류·가설 정책을
검사합니다. `--dry-run`은 모델 호출 없이 합성 근거를 생성합니다. 원문과 자동 결과는
`artifacts/evaluations/<ID>/`에 저장하며, 내용 검토는 `review.md`에서 자동 검사와 구분합니다.
버전별 프롬프트 비교는 `--prompt-versions`, 모델 비교는 `--models`로 지정합니다.

## 개발 검사

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

테스트는 범위 이탈·중복 조회·잘못된 수치·근거 ID·예산 종료·원자적 저장·SQLite 재개와
CLI 입력을 확인합니다. 테스트 통과는 실제 모델의 조사 정확도와 별개의 지표입니다.
