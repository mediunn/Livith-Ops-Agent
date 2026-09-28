# 운영 보고서 평가

[문서 목록](../docs/README.md) · [프로젝트 사용법](../README.md)

`ops_agent/cli/evaluate_reports.py`는 고정 보고서의 출력과 관측값을 검증합니다.
`ops_agent/cli/evaluate_agent.py`는 합성 수집 결과를 사용해 실제 모델의 도구 선택부터 최종 보고서까지
Agent 루프를 실행합니다. 두 평가기는 별도 데이터셋과 검사 기준을 사용합니다.

평가 로직은 `ops_agent/evaluation/agent.py`와 `ops_agent/evaluation/reports.py`에 있으며,
`ops_agent/cli/`의 두 평가 CLI에서 호출합니다. 프로젝트 루트에서
`uv run python -m ops_agent.cli.evaluate_agent` 또는
`uv run python -m ops_agent.cli.evaluate_reports`로 실행합니다.

| 디렉터리 | 내용 |
|---|---|
| `datasets/agent/` | Agent 전체 루프와 이전 정책 회귀 데이터셋 |
| `datasets/report/` | 고정 보고서 합성 데이터셋 |
| `results/agent/` | Agent 정책·관측·조회 효율 검증 문서 |
| `results/report/` | 보고서 버전 비교와 관측값 검증 문서 |

실행 중 생성되는 근거와 평가 결과는 기존 `artifacts/` 아래에 저장합니다.

## HTTP 조사 평가

```bash
uv run python -m ops_agent.cli.evaluate_http_agent --planner rules
uv run python -m ops_agent.cli.evaluate_http_agent --planner both --model qwen2.5:3b

# 같은 호출 제한으로 두 모델을 각각 두 번 비교
uv run python -m ops_agent.cli.evaluate_http_agent --planner llm \
  --models qwen2.5:3b huihui_ai/qwen2.5-abliterate:14b-instruct \
  --repeat 2 --seconds 300 --llm-seconds 90 --warmup
```

`evaluation/http_agent.py`의 개발용 네 사례를 실제 HTTP 실행기·파서·그래프·SQLite에 통과시킵니다.
Grafana 대신 조건별 합성 MCP 응답을 사용하며, `llm`·`both`는 로컬 Ollama를 호출합니다.
데이터 없는 경우를 제외한 세 사례는 지연 증가·요청률 0으로 감소·증가 의심 반박을 다룹니다.
기대 결과는 모델 입력에 포함하지 않습니다. 독립 평가 데이터셋은 아닙니다.

기본값은 반복 1회·조사 180초·모델 호출 45초이며, `--repeat`(최대 10회), `--seconds`,
`--llm-seconds`(최대 180초)로 바꿀 수 있습니다. 모든 비교 모델에 같은 예산을 적용하고 반복마다
모델 순서를 순환합니다. `--warmup`은 모델별 배치 전에 빈 메시지로 사전 로딩하며 조사 예산과
별도로 시간·성공 여부를 저장합니다. 로딩 실패도 기록하고 실제 평가 요청은 계속합니다.
`--case`를 반복 지정하면 원하는 사례만 실행합니다. 근거 UUID는 실행마다 달라집니다.

`metadata.json`에는 데이터셋·설정·실행 코드 해시를, `summary.json`에는 개별 결과·모델별 집계를
저장합니다. 사례마다 요약을 갱신하므로 중단 시 완료된 사례가 남습니다. `status`가
`interrupted_or_failed`이거나 `running`이면 전체 평가가 완료된 것이 아닙니다. 기존 결과 디렉터리는
덮어쓰지 않습니다. `--planner both`의 규칙 기준선은 모델 수와 관계없이 반복당 한 번만 실행합니다.

`outcome`은 시간 초과(`timeout`), 잘못된 조회 응답(`invalid_decision`), 필요한 비교 누락
(`missing_comparison`), 가설 상태·인용 검사 실패(`hypothesis_check_failed`) 등을 구분합니다.
`automatic_checks_passed`도 가설 문장이 옳다는 판정은 아닙니다. 빈 데이터는 집계의
`comparison_runs`에서 제외합니다. 토큰은 수신한 합계(`known_*`)와 미수신 호출 수를 함께 기록하며,
서버 로딩·입력 평가·출력 생성 시간은 응답이 있을 때만 기록합니다. 사용량 미상은 0이 아닙니다.

`checks`는 완료·중복·예산·필요한 대상의 직전 구간 조회를 검사하며 실패하면 CLI 종료 코드는 1입니다.
`hypothesis_status_match`는 상태와 양쪽 구간 근거 인용만 확인하는 별도 진단값입니다.
가설의 문장 의미는 이 값이나 완료 상태로 판정하지 않습니다. `semantic_review`는 별도 검토 전까지
`pending`입니다. 규칙 기준선은 증상 해석 없이 가장 큰 지연을 고르므로 증상 대상 선택의 강한 기준선은 아닙니다.

실행별 원본·모델 응답·보고서·요약은 `artifacts/evaluations/http-agent/<ID>/`에 남습니다.
[HTTP 실행 결과](results/agent/http-agent-validation.md)에 실제 3B 실패와 Grafana 연결 검증을 기록했습니다.
[3B·14B 동일 예산 비교](results/agent/http-model-comparison.md)에는 판단 실패와 응답 시간 초과를 구분해 기록했습니다.
[90초 호출 제한의 반복 평가](results/agent/http-repeat-evaluation.md)에는 모델별 두 번 실행한 결과와 원문 검토를 기록했습니다.

## Agent 전체 루프 평가

```bash
# 외부 호출 없이 일곱 사례의 파싱된 관측만 생성
uv run python -m ops_agent.cli.evaluate_agent --dry-run

# 합성 관측 + 실제 로컬 모델 + LangGraph/SQLite/예산/보고서 실행
uv run python -m ops_agent.cli.evaluate_agent

# 특정 사례 반복; 추적 전송은 명시적으로 활성화
OPS_LANGFUSE_ENABLED=true uv run python -m ops_agent.cli.evaluate_agent --case rate_increase --repeat 2
```

`datasets/agent/agent-cases.json`(agent-loop-v2)은 기존 요청률 0·증가·경고 레벨·경고 문자열만 있는 info 로그에
일반 조회 한도 밖의 경고, 완전성 정보 누락, 잘리지 않은 경고 로그를 더한 일곱 사례입니다.
이전 네 사례의 원본은 `datasets/agent/agent-cases-v1.json`에 보존합니다.
실제 Grafana에는 접속하지 않습니다. 수집 결과는 기존 Prometheus/Loki 파서로 만들고,
모델은 운영 코드의 planner를 그대로 사용합니다. 기대 결과는 모델 입력에서 제외합니다.
모든 사례는 원인을 특정할 정보가 없으므로 최종 가설 목록이 비어 있는지를 검사합니다.

자동 검사는 완료 여부, 요청한 이전 구간 비교·로그 조회, 기대 관측 선택, 가설 정책,
문장 끝 형식과 조회 중복을 확인합니다. v2 사례는 추가 경고 조회의 수행·생략과 예상 도구
호출 수도 검사합니다. 이는 고정 사례의 조회 효율 검사이며 문법·의미 정확성 점수는 아닙니다.
v4 Agent의 문장은 코드가 작성하므로 문장 끝 검사를 모델 문장 품질 점수로 해석하지 않습니다.
필수 조회 완료와 코드 설명 출처도 별도 검사합니다. 이전 v3 모델은 `추가 정보를 얻.`처럼
문장부호만 있는 불완전 문장도 형식 검사를 통과할 수 있었습니다.
`semantic_review`는 별도 검토 전까지 `pending`입니다.

결과 목록·토큰·시간은 `artifacts/evaluations/agent-<ID>/summary.json`, 각 실행의
관측·모델 원문·SQLite·보고서는 `artifacts/agent/eval-<ID>/`에 저장합니다.
실패도 보존하고 다음 사례를 계속 실행하며 하나라도 자동 검사를 실패하면 종료 코드 1입니다.
Langfuse에는 합성 평가임을 명시하고 자동 조건 점수를 남깁니다.

[이전 전체 루프 결과](results/agent/agent-loop-validation.md)와
[필수 조사·코드 설명 정책 검증 결과](results/agent/agent-policy-validation.md),
[추가 로그 조회 효율 검증 결과](results/agent/agent-query-efficiency.md)를 별도로 기록합니다.

## 고정 보고서와 기존 회귀 검사

`datasets/agent/agent-reference-cases.json`은 Agent의 근거 참조 회귀 사례 3개입니다. 실제 실패 패턴을
합성 데이터로 재구성했으며, 참조 검사 기대값과 의미적 검토 기준을 분리합니다.
기존 참조 검사만으로는 미조회 구간 단정과 요청률 0의 장애 단정을 검출하지 못했습니다.
`expected_validation_error`는 당시 기대값을 보존하며 `expected_claim_validation_error`는
이전 v3 관측 정책을 적용한 기대값입니다. 이전 정책은 선택할 검증 관측이 없는 가설을 거부합니다.
이 사례는 v3 회귀 검사로 보존합니다. 새 v4 모델은 action·claim_ids만 출력합니다.
이를 모든 가설 내용의 정확성 검증으로 해석하지 않습니다. 이 파일은
`tests/agent/decision/test_validation.py`의 회귀 검사에서 사용하며 고정 보고서 평가기의 데이터셋은 아닙니다.
[기존 참조 검사 결과](results/agent/agent-reference-validation.md)와
[관측 주장 정책 검증 결과](results/agent/agent-claim-validation.md)를 별도로 기록합니다.

`datasets/report/cases.json`은 운영 데이터가 아닌 합성 사례 6개와 검토 기준입니다.
기존 네 사례는 요청률 0, 데이터 없음, 경고 로그, 로그 잘림을 다룹니다.
v2에서 추가한 두 사례는 유효하지 않은 수치와 소수 요청률·오류 로그를 다룹니다.
기존 사례의 기대 분류는 변경하지 않았습니다. 새 사례는 이번 변경 후 처음 실행하는
추가 검증이며 대규모 독립 평가 세트는 아닙니다.
기대 분류와 검토 기준은 모델에 전달하지 않습니다.

프로젝트 루트에서 실행합니다.

```bash
# 모델 호출 없이 합성 근거 생성
uv run python -m ops_agent.cli.evaluate_reports --dry-run

# 기본 3B 모델로 전체 사례 실행
uv run python -m ops_agent.cli.evaluate_reports

# 같은 모델에서 v1과 v2 비교
uv run python -m ops_agent.cli.evaluate_reports --prompt-versions ops_report_v1 ops_report_v2

# 설치된 두 모델을 같은 입력·프롬프트·생성 옵션으로 비교
uv run python -m ops_agent.cli.evaluate_reports --models qwen2.5:3b huihui_ai/qwen2.5-abliterate:14b-instruct

# 한 사례만 다시 실행
uv run python -m ops_agent.cli.evaluate_reports --case warning_present --models qwen2.5:3b
```

결과는 `artifacts/evaluations/<실행 ID>/`에 저장합니다. 실제 근거와 별도
디렉터리를 사용하므로 기본 보고서 생성에서 합성 근거가 선택되지 않습니다.

- `dataset.json`: 실행에 사용한 사례와 기대 결과
- `summary.json`: 모델별 자동 검사 결과, 지연 시간, 토큰 수
- `review.md`: 생성 보고서와 내용 검토 체크리스트
- 사례·모델별 JSON: 정확한 입력, 프롬프트, 스키마, 응답, 실패 정보

자동 검사는 JSON 형식, 근거 ID, 기대 분류, 가설 목록이 비었는지를 확인합니다.
단어 포함 여부로 사실성을 판정하지 않습니다. 자동 검사를 통과해도 수치 해석이나
주장의 정확성을 보장하지 않으므로 `review.md`에서 각 항목을 pass/fail과 이유로
검토해야 합니다. 이 문서를 편집해도 JSON의 `semantic_review`는 자동 변경되지 않습니다.

자동 검사 실패 또는 생성 오류가 있으면 종료 코드 1을 반환합니다. 실패한 사례도
결과에 남고 다음 사례를 계속 실행합니다. 다운로드나 실제 Grafana 조회는 하지 않습니다.
모델 로딩과 캐시가 실행 시간에 영향을 주므로 1회 실행으로 속도 우열을 확정하지 마세요.
이 작은 사례 집합의 결과는 모델의 일반적 정확도를 나타내지 않습니다.

## v1 / v2 비교 범위

v1·v2는 기존 비교 기준으로 보존합니다. `python -m ops_agent.reporting.generator --prompt-version
ops_report_v1`로 이전 버전을 실행할 수 있습니다. v1은 기존 프롬프트와 요약 입력을
유지하고, v2는 새 프롬프트와 단위 설명이 추가된 입력을 사용합니다. 따라서 결과는
프롬프트 하나의 효과가 아니라 **프롬프트와 입력 개선을 합친 효과**입니다.
원본 조회 조건과 요약값, 출력 스키마, 생성 옵션은 같습니다.

v2의 assessment는 서비스 상태 판단을 위한 조사 필요성을 나타냅니다.
경고·오류가 있으면 추가 조사, 그런 징후가 없어도 필요한 근거나 평가 기준이 없으면
근거 부족을 선택하도록 프롬프트에 우선순위를 명시했습니다. 이는 이 프로젝트의
분류 정책이며 범용 SRE 표준은 아닙니다. 모델의 분류를 코드에서 덮어쓰지 않습니다.

v2는 확인된 외부 API rate 쿼리에만 `requests_per_second`, 300초 계산 구간,
조회 조건의 step을 설명으로 붙입니다. 알 수 없는 쿼리의 단위는 `unknown`입니다.
`sample_count`는 반환된 평가 시점 수이며 요청 건수가 아닙니다.
[Prometheus rate 문서](https://prometheus.io/docs/prometheus/latest/querying/functions/#rate),
[range query 문서](https://prometheus.io/docs/prometheus/latest/querying/api/#range-queries)를 참고했습니다.

모든 실행 결과에는 `prompt_version`, `context_version`, 프롬프트 원문과 해시가
저장됩니다. 버전별 파일명을 분리해 같은 모델의 결과가 서로 덮어써지지 않습니다.

## v3 관측값 검증

기본 보고서와 평가는 이제 v3입니다. v3는 출력 스키마와 관측 사실 작성 방식이
바뀌었으므로 v1·v2와의 차이를 단순 프롬프트 성능 향상으로 해석하지 마세요.
입력 `observed_values`에는 정답 분류가 아닌 파싱된 관측값이 들어갑니다.
모델이 반환한 `observations`를 원본 요약과 대조하고, 통과하면 코드가 사실 문장을
작성합니다. 값이 틀리면 `factual_validation_error`와 모델 원문을 저장합니다.

평가에 `observation_values` 검사를 추가했고 Langfuse에는
`auto_observation_values`로 기록합니다. 이 점수는 관측값 복사·보존 검증이며 자유
서술 정확도 점수가 아닙니다. v1·v2는 이 검사를 하지 않으므로 점수를 부여하지 않습니다.
가설·한계·다음 확인 사항 및 상태 분류는 계속 별도로 검토해야 합니다.

```bash
uv run python -m ops_agent.cli.evaluate_reports --prompt-versions ops_report_v3
uv run python -m ops_agent.cli.evaluate_reports --prompt-versions ops_report_v2 ops_report_v3 --case warning_present
```
