# 조사 구조

[문서 목록](../README.md) · [프로젝트 사용법](../../README.md)

코드에서 조사 그래프와 상태는 `ops_agent/agent/`, 모델의 도구 선택과 응답 검증은
`ops_agent/agent/decision/`에 있습니다. 관측 주장과 조사 정책은 보고서에서도 공유하므로
`agent/claims.py`, `agent/policy.py`에 두며, 합성 평가 로직은 `ops_agent/evaluation/`에서 관리합니다.

OpsAgent는 Grafana의 읽기 전용 도구로 근거를 수집하고 로컬 모델로 조사 방향을 선택합니다.
LangGraph는 실행 흐름을 관리하고, SQLite 체크포인트는 같은 조사를 중단·재개할 수 있게 합니다.

## 실행 흐름

```mermaid
flowchart TD
  Start[새 조사 또는 저장된 조사 재개] --> Decide[decide: 근거 확인과 다음 행동 선택]
  Decide -->|조회| Query[query: Grafana MCP 호출]
  Query --> Save[원본 저장과 상태 체크포인트]
  Save --> Decide
  Decide -->|종료| Report[report: 관측 사실과 코드 설명 구성]
  Report --> Result[최종 JSON 저장과 출력]
```

1. CLI 요청의 대상·시간대·조회 구간·직전 구간 비교 여부를 검증하고 실행 예산을 설정합니다.
2. 현재 구간의 외부 API 요청률을 수집합니다.
3. 모델은 `action`과 `claim_ids`만 선택합니다. 자유 문장·원인 가설은 생성하지 않습니다.
4. 코드는 도구 중복·관측 ID·필수 조회 이행을 검사합니다.
5. 조회 결과와 상태를 저장하고 코드가 관측·설명·한계·다음 확인 항목을 구성합니다.

기본 필수 조회는 현재 지표·직전 지표·서비스 로그입니다. `--no-compare-previous`를
지정하면 직전 구간을 조회하지 않습니다. 비교 여부는 `request.compare_previous`에
저장하며 자연어 증상이 명시 설정을 덮어쓰지 않습니다. 재개 시에도 설정을 유지합니다.

`required_checks`에는 필수·완료·미완료 항목과 반환 상태를 기록합니다. 완료로 계산하려면
현재 요청의 고정 쿼리·도구·시간 구간과 일치하는 `data_available`·`no_data`·`invalid_data`
응답이어야 합니다. 데이터 없음도 조회 이행에는 포함하지만 건강 상태의 근거는 아닙니다.
`warning_logs`는 필수 `logs` 조회를 대신하지 않습니다.

필수 조회가 끝난 뒤 `warning_log_followup`으로 추가 문자열 조회의 적격 여부와 이유,
근거 ID, 실제 조회 여부를 기록합니다. 동일한 서비스 로그 쿼리의 응답이 성공했고,
건수가 100건 미만이며 서버 잘림·한도 도달·잘림 가능성 플래그가 모두 명시적으로 false이면
부분집합인 `warning_logs`를 선택 목록에서 제외합니다. 반환 상태·건수·한도 메타데이터도
일치해야 합니다. 100건 도달, 잘림, 완전성 정보 누락·불일치이면 추가 조회를 허용합니다.
이 판단은 경고 레벨 유무나 요청률에 의존하지 않습니다. 일반 조회 밖의 경고가 있을 수
있으므로 잘린 info 로그만 보고 추가 조회를 생략하지 않습니다.

이유는 `required_checks_pending`, `base_result_complete`, `base_result_truncated`,
`base_completeness_unknown` 중 하나입니다. 필수 조회 중에는 추가 조회를 허용하지 않습니다.
생성 스키마·출력 검증뿐 아니라 실제 호출 직전에도 검사하며, 저장된 선택이 현재 정책에서
차단되면 `optional_query_blocked`와 `incomplete`로 종료합니다. 이미 수행된 조회는 보존합니다.
같은 시점의 고정 범위 내 중복을 줄이는 정책이며 이후 지연 수집·전체 서비스 정상 여부를
확인하는 정책은 아닙니다. 추가 조회가 가능해도 필수 항목으로 강제하지는 않습니다.

필수 항목이 남으면 생성 스키마에서 `finish`를 제외하고, 사후 검사에서도
`required_checks_missing`으로 거부합니다. 그래프와 보고서에도 종료 검사를 둡니다.
예산 부족·수집 실패는 `incomplete`로 끝나며 미완료 항목을 보고서에 남깁니다.

코드는 두 종류의 관측을 계산해 `verified_claims`로 제공합니다. 모델은 이 목록에서
`claim_ids`를 선택하며 목록이 있으면 하나 이상 선택해야 합니다. 없으면 빈 배열입니다.

- 반환 로그에 기록된 `warn`·`warning`·`error` 레벨 집계. 문자열 매칭만으로 경고라고 판단하지 않습니다.
- 동일 라벨의 외부 API 요청률에서 두 구간의 마지막 평가값 증가. 중복·불일치 라벨,
  잘못된 값·오래된 마지막 샘플은 비교하지 않습니다. 전체 추세·평균·원인 판정은 아닙니다.

잘못된 출력은 원문을 보존한 뒤 한 번 수정합니다. 수정도 기존 호출·시간·토큰·입력 크기
예산을 사용합니다. timeout·통신 오류·출력 잘림은 자동 수정하지 않습니다.
모델의 원문은 판단 receipt와 Langfuse에 진단용으로 남으며, 추가 자유 문장은 `schema_error`로
거부합니다. `rationale_format`과 가설 참조 검사는 이전 v3의 회귀 검사에만 남아 있습니다.

## 조회 도구

| 도구 | 조회 내용 |
|---|---|
| `current_metrics` | 요청한 구간의 외부 API별 초당 요청률 |
| `previous_metrics` | 요청 구간과 같은 길이의 직전 구간 요청률 |
| `logs` | 현재 구간의 서비스 로그, 최대 100건 |
| `warning_logs` | `warn` 또는 `error` 문자열에 매칭되는 로그, 최대 100건 |

지표는 5분 rate 계산과 60초 평가 간격을 사용합니다. 조회 구간은 조사 시작 시 고정하며
재개 시에도 유지합니다. 증상 문자열은 이 구간을 변경하지 않습니다.
구간을 생략하면 최근 30분을 사용하며 명시적 구간은 최대 6시간입니다. 이 상한은 초기 실행 제한입니다.
시작·종료는 함께 입력해야 하며 역전·미래 구간은 거부합니다. 오프셋 없는 시각은 지정한
IANA 시간대(기본 `Asia/Seoul`)로 해석합니다. DST로 모호하거나 존재하지 않는 현지 시각은
오프셋을 명시해야 합니다. 확정된 시각은 UTC로 저장합니다.

서비스는 `livith-server`, 조사 유형은 `external_api`, 환경은 `unspecified`만 지원합니다.
현재 쿼리는 환경 라벨로 필터링하지 않으며, 지표에는 서비스 필터도 없습니다.
로그 대상은 `livith-server`입니다. 자연어 증상은 조사 맥락이며 실제 조회 범위를 변경하지 않습니다.

경고 문자열 검색은 실제 로그 레벨 필터와 다릅니다. 두 로그 조회는 결과가 겹칠 수 있고,
빈 조회 결과만으로 서비스가 정상이라고 판단하지 않습니다.

## 실행 제어와 상태 저장

### HTTP 도구 실행 경계

`tools/http.py`의 `HTTPQuery`는 지표 종류·시간 구간·정확한 route·HTTP method를 검증합니다.
`HTTP_TOOLS`는 요청률과 평균 지연의 설명·단위·PromQL 템플릿을 제공합니다. 임의 PromQL,
데이터소스·job 변경은 조회 입력으로 받지 않습니다. 시간은 UTC로 정규화하고 구간은 최대
24시간으로 제한합니다. 조회 직전에도 입력을 다시 검증합니다.

외부 API 도구와 HTTP 도구는 `tools/grafana.py`의 `execute_query()`를 공유합니다.
이 실행기는 예산 차감, 조회 조건별 캐시, 원본 응답 보존, 파서 호출과 오류 기록을 맡습니다.
외부 API 호출만 이전 `{action}.json` 캐시를 읽는 호환 경로를 사용합니다.

HTTP CLI는 `cli/query_http.py`이며 한 실행에서 하나의 MCP 세션을 공유합니다. 결과는
`artifacts/http/<run_id>/requests.json`, `queries/<query_key>.json`, `budget.json`,
`result.json`에 남습니다. 조회 실패·연결 실패는 종료 코드 1이며, 유효한 빈 결과는
`no_data`로 보존합니다. 이 단계에는 모델 판단·LangGraph 재개·원인 가설 생성이 없습니다.

### 기존 Agent 실행

기본 실행 예산은 90초, 도구·모델 호출은 각각 최대 6회입니다. 모델 호출 전에는
17,408 토큰을 예약하며 누적 예약 한도는 110,000입니다. 예약량과 실제 토큰 사용량은
별도로 기록합니다. 도구 호출은 최대 20초, 모델 호출은 최대 45초이며 남은 전체 시간을 적용합니다.
이 값들은 현재 실행 제한이며 분석 품질이나 최대 완료 시간을 보장하는 값은 아닙니다.

예산은 실행 상한이고, 추가 조회 여부는 모델이 선택합니다. 모델이 `finish`를 선택하고 필수 조회가 완료되면
`completed`, 오류나 시간·호출 제한으로 종료되면 `incomplete`와 종료 사유를 기록합니다.
모델의 조회 선택과 관측 선택은 검토 대상입니다. `completed`는 서비스 정상 판정을 뜻하지 않습니다.

| 저장 위치 | 내용 |
|---|---|
| `artifacts/agent/checkpoints.sqlite` | 조사별 상태, 근거 요약, 선택 이력과 다음 실행 위치 |
| `artifacts/agent/<thread_id>/` | 조회 원본, 모델 응답, 예산 장부와 최종 `report.json` |

`--step`은 조회 완료 후 상태를 저장하고 멈춥니다. `--resume`는 같은 ID의 조사를 이어가며
검증된 요청·조회 구간과 소비한 예산을 유지합니다. 중단 시간도 예산에 포함됩니다.
재개·상태 확인 시 새 조사 옵션은 거부합니다. 상태 v4는 새 모델 출력 계약과 비교 설정을 사용합니다. v2·v3 상태는
자동 변환하거나 CLI로 재개하지 않습니다. 이전 조사의 결과 파일은 보존되고 새 조사를 시작해야 합니다.
완료된 조사를 재개하면 저장된 결과를 출력합니다. 조회 도중 중단된 호출은 다시 실행될 수 있습니다.

## 보고서와 실행 추적

보고서 v4는 파싱된 관측을 `facts`, 코드가 검증한 전체 관측을 `verified_claims`, 모델의
최종 선택을 `selected_claim_ids`로 구분합니다. 선택에서 빠진 검증 관측도 삭제하지 않습니다.
`rationale`·`limitations`·`next_checks`는 코드가 구성하고 `narrative_source=code_generated`로
표시합니다. 서비스 건강 상태는 항상 `health_assessment=not_evaluated`입니다.

분류는 `assessment_source=observation_policy`로 기록합니다. 필수 조회를 마치고 검증 관측이
있으면 `needs_investigation`, 그 외에는 `insufficient_evidence`입니다. 오류로 중단된 보고서는
근거 부족으로 표시합니다. `model_assessment`는 null, `hypotheses`는 빈 배열입니다.
현재 입력은 집계 요약뿐이므로 원인 가설 생성은 지원하지 않습니다.

Agent 프롬프트는 `ops_agent_v8`, 조사 정책은 `investigation-policy-v2`입니다. 저장된 v4
프롬프트를 재개 시 사용하지만 코드의 현재 조회 정책을 적용합니다. 이전 프롬프트·결과 파일은 보존합니다. `semantic_review`는
계속 `pending`입니다. 코드 설명도 정책과 계측 범위에 대한 별도 검토가 필요합니다.

`tools/grafana.py`는 Livith의 운영 데이터를 조회하고, `telemetry/langfuse.py`는 OpsAgent
자신의 실행을 추적합니다. Langfuse를 켜면 실행·재개별 `ops-agent` trace에 도구 호출과
`ollama-planner`가 연결됩니다. 자세한 전송 범위는 [설정 안내](../setup/langfuse.md)를 참고하세요.
각 모델 응답은 판단 ID·시도 번호·수정 대상 ID·검증 결과를 별도로 저장하고 추적합니다.
수정 성공 후에도 첫 실패 응답은 남습니다.

## 고정 보고서와의 관계

`ops_agent/cli/run_investigation.py`는 지표 → 로그 → 보고서의 고정 순서로 실행합니다.
Agent와 수집·파싱·추적 코드를 공유하며, 보고서 형식과 생성 경로는 다릅니다.
고정 보고서 v3는 모델이 반환한 관측값을 원본 요약과 대조합니다.
`ops_agent/cli/evaluate_reports.py`는 이 고정 보고서를 대상으로 평가합니다.
`ops_agent/cli/evaluate_agent.py`는 합성 수집기를 주입해 운영 planner·그래프·예산·SQLite·보고서를
실행합니다. 기본 CLI의 수집기는 그대로 실제 Grafana를 사용합니다.
