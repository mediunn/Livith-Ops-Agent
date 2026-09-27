# 운영 보고서 평가

[문서 목록](../docs/README.md) · [프로젝트 사용법](../README.md)

이 평가기는 고정 보고서의 출력과 관측값을 검증합니다. Agent의 도구 선택·종료 판단은
평가 대상에 포함하지 않습니다.

`agent-reference-cases.json`은 Agent의 근거 참조 회귀 사례 3개입니다. 실제 실패 패턴을
합성 데이터로 재구성했으며, 참조 검사 기대값과 의미적 검토 기준을 분리합니다.
기존 참조 검사만으로는 미조회 구간 단정과 요청률 0의 장애 단정을 검출하지 못했습니다.
`expected_validation_error`는 당시 기대값을 보존하며 `expected_claim_validation_error`는
현재 관측 정책을 적용한 기대값입니다. 현재는 선택할 검증 관측이 없는 가설을 거부합니다.
이를 모든 가설 내용의 정확성 검증으로 해석하지 않습니다. 이 파일은
`tests/test_agent.py`의 회귀 검사에서 사용하며 고정 보고서 평가기의 데이터셋은 아닙니다.
[기존 참조 검사 결과](agent-reference-validation.md)와
[관측 주장 정책 검증 결과](agent-claim-validation.md)를 별도로 기록합니다.

`cases.json`은 운영 데이터가 아닌 합성 사례 6개와 검토 기준입니다.
기존 네 사례는 요청률 0, 데이터 없음, 경고 로그, 로그 잘림을 다룹니다.
v2에서 추가한 두 사례는 유효하지 않은 수치와 소수 요청률·오류 로그를 다룹니다.
기존 사례의 기대 분류는 변경하지 않았습니다. 새 사례는 이번 변경 후 처음 실행하는
추가 검증이며 대규모 독립 평가 세트는 아닙니다.
기대 분류와 검토 기준은 모델에 전달하지 않습니다.

프로젝트 루트에서 실행합니다.

```bash
# 모델 호출 없이 합성 근거 생성
uv run python evaluate_reports.py --dry-run

# 기본 3B 모델로 전체 사례 실행
uv run python evaluate_reports.py

# 같은 모델에서 v1과 v2 비교
uv run python evaluate_reports.py --prompt-versions ops_report_v1 ops_report_v2

# 설치된 두 모델을 같은 입력·프롬프트·생성 옵션으로 비교
uv run python evaluate_reports.py --models qwen2.5:3b huihui_ai/qwen2.5-abliterate:14b-instruct

# 한 사례만 다시 실행
uv run python evaluate_reports.py --case warning_present --models qwen2.5:3b
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
uv run python evaluate_reports.py --prompt-versions ops_report_v3
uv run python evaluate_reports.py --prompt-versions ops_report_v2 ops_report_v3 --case warning_present
```
