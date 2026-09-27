# Livith OpsAgent

Grafana MCP로 지표와 로그를 수집하고 로컬 Ollama로 운영 보고서 초안을 만듭니다.
고정 수집 보고서와, 허용된 조회 중 다음 도구를 선택하는 LangGraph Agent CLI를 제공합니다.
운영 조치는 실행하지 않습니다. 현재 Agent의 지표 범위는 외부 API 요청률이며,
로그는 `livith-server` 서비스 로그입니다. Grafana의 모든 대시보드를 자동으로 탐색하지는 않습니다.

## 시작

```bash
uv sync
```

`.env.example`을 참고해 `.env`에 Grafana URL·읽기 전용 서비스 계정 토큰을
설정하고 로컬 Ollama를 실행하세요. Langfuse는 선택 사항이며
[설정 안내](docs/setup/langfuse.md)에 따라 연결할 수 있습니다.

## 현재 가능한 것과 남은 것

- 증상 문자열을 모델에 전달하고, 허용된 조회 중 다음 행동 또는 종료를 선택합니다.
- 조회 구간은 시작 시점의 최근 30분입니다. 자연어의 날짜·시간·서비스·환경을 추출해서
  조회 인자로 반영하는 기능은 아직 없습니다. `--seconds`는 조회 구간이 아니라 실행 시간 예산입니다.
- 최종 결과는 JSON으로 터미널에 출력하고 저장합니다. Markdown 보고서와 사용자 UI는 아직 없습니다.
- SQLite는 같은 조사의 근거·판단·다음 노드를 복원합니다. 후속 대화 입력과 조사 간 장기 기억 검색은 아직 없습니다.
- HTTP 지연·오류율, 서버·DB 지표, Sentry, 저장소 코드 조회는 후속 확장입니다.
- 고정 보고서 평가는 구현돼 있지만, Agent의 도구 선택·종료 판단을 같은 사례로 비교하는 평가는 남아 있습니다.

## 프로젝트 구성과 문서

```text
run_agent.py / run_investigation.py / evaluate_reports.py / check_langfuse.py
ops_agent/
  agent/         # 상태, 다음 행동 선택, graph, 예산
  collectors/    # Grafana MCP 연결, 수집 CLI, 응답 파서
  tools/         # Agent가 선택하는 Grafana 조사 도구
  persistence/   # 조사 세션, 체크포인트, 원본 파일 저장
  reporting/     # 고정 보고서와 Agent 결과 구성
  telemetry/     # Agent 실행 자체의 Langfuse 추적
prompts/         # Agent와 고정 보고서 프롬프트
tests/           # 합성 응답 기반 테스트
evals/           # 합성 평가 사례와 평가 기록
docs/            # 설정, 설계, 개발 안내, 로드맵
artifacts/       # 로컬 실행 결과; Git 제외
```

[문서 목록](docs/README.md), [다음 작업](docs/roadmap.md),
[Git 포함 범위와 파일별 커밋 순서](docs/private/version-control.md)를 참고하세요.
개인 계획·대화 원문은 Git에서 제외되는 `docs/private/`에 보관합니다.

## LangGraph 조사 Agent

```bash
# 첫 조회 후 저장하고 멈추기. 재개 실습을 위해 시간 예산을 600초로 지정
uv run python run_agent.py --seconds 600 --step --symptom "최근 요청률과 관련 로그 확인"

# 위 명령에서 출력된 ID 사용
uv run python run_agent.py --status THREAD_ID
uv run python run_agent.py --resume THREAD_ID

# 한 번에 실행 (기본 시간 예산 90초)
uv run python run_agent.py
```

조사 시간과 근거는 같은 `thread_id`에 저장됩니다. 재개는 완료된 노드 다음부터
진행하며 시간·호출 예산을 초기화하지 않습니다. `--seconds`, `--model`, `--symptom`은
새 조사에만 적용됩니다. 중단 시간도 예산에 포함되므로 오래 지난 조사는 시간 초과로
종료합니다. 새 시간대는 새 조사로 시작하세요.

시작 시 현재 지표를 수집하고 모델이 이전 구간 지표·일반 로그·경고 키워드 로그·종료를
선택합니다. 자유로운 쿼리 생성은 허용하지 않습니다. 조회 오류, 출력 검증 실패,
근거 크기 초과, 예산 소진을 구분해 부분 결과를 보존합니다.

`artifacts/agent/checkpoints.sqlite`는 조사 상태, 조사별 폴더는 원본 근거·모델 응답·
예산·최종 `report.json`을 저장합니다. `completed`는 조사 루프의 종료이며 서비스 정상이나
분석 정확성을 뜻하지 않습니다. 모델 해석은 `semantic_review: pending`입니다.

프로세스 잠금을 사용하는 macOS/Linux 로컬 CLI입니다. 상세 파일 역할, 데이터 흐름,
예산·복구의 한계는 [동작 설명](docs/architecture/agent-workflow.md)을 참고하세요.
추가 조회는 모델이 필요성을 설명하고 선택하며, 예산은 실행 상한입니다.
현재 제한값과 30분 구간은 성능 평가로 최적화한 값이 아닌 초기 설정입니다.

## 수집부터 보고서까지 한 번에 실행

```bash
uv run python run_investigation.py

# 조사 요청을 함께 기록
uv run python run_investigation.py --symptom "최근 외부 API 관련 경고 확인"
```

최근 30분의 외부 API 요청률을 수집하고, 같은 시간 범위의 Loki 로그를 최대 100건
조회한 뒤 기본 v3 보고서를 생성합니다. 증상 입력은 조사 맥락이며 쿼리를 자동으로
변경하지 않습니다. 현재 쿼리에는 환경 필터가 없으므로 결과를 운영 환경만의 결과라고
가정하지 않습니다. 환경은 `unspecified`로 기록합니다.

각 실행은 `artifacts/investigations/<run_id>/`에 독립 저장됩니다.

- Prometheus·Loki 원본 응답과 요약: 실제 수집 근거
- `report.json`: 입력·모델 원문·검증 결과·보고서·Langfuse 링크
- `investigation.json`: incident_id, 실행 ID, 시간 범위, 근거 파일, 최종 상태

단계 사이에는 생성한 파일 경로를 직접 전달합니다. 수집 실패 시 과거 파일을 대신
선택하지 않고 종료 코드 1을 반환합니다. `no_data`와 `invalid_data`는 수집된 근거의
상태로 보고서 입력에 전달되며, 정상 상태로 바꾸지 않습니다.

Langfuse를 켜면 `ops-investigation` 아래에 실제 MCP 수집 도구 호출과
`ops-report → ollama-report`가 연결됩니다. 원본 로그 본문은 trace에 보내지 않습니다.

## 저장된 근거 재사용과 평가

```bash
uv run python -m ops_agent.reporting.generator --loki-evidence /absolute/path/to/loki-evidence.json
uv run python evaluate_reports.py --prompt-versions ops_report_v3
```

`--loki-evidence`를 생략하면 최상위 `artifacts/`의 기존 Loki 파일을
선택합니다. 통합 실행 결과를 재사용할 때는 해당 실행의 Loki 파일을 명시하세요.
평가 방법과 v1·v2 재실행은 [평가 문서](evals/README.md)를 참고하세요.

## 고정 보고서 v3의 검증 범위

모델은 관측값을 `observations`로 반환합니다. 코드가 근거 ID, 데이터 상태, 단위,
계산 구간·평가 간격, 시계열 라벨·샘플 수·최소/최대/마지막 값, 로그 수·레벨·잘림 여부를
파싱된 원본 요약과 비교합니다. 값이 다르면 `factual_validation_error`로 기록하고
성공 보고서를 만들지 않습니다. 잘못된 응답 원문은 분석을 위해 보존합니다.
수치 필드만 상대 오차 `1e-12`, 절대 오차 `0`으로 부동소수점 끝자리 표현 차이를
허용합니다. 상태·단위·건수는 정확히 같아야 하며, null과 0 또는 0과 양수는 구분합니다.

통과한 관측값의 `facts` 문장은 코드의 템플릿으로 만듭니다. 모델이 자유롭게 쓴
수치 설명을 검증했다고 주장하지 않으며, 모델의 출력값을 조용히 정정하지 않습니다.
v3의 관측값 대조는 정답 복사 정확도를 확인하는 계약 검사이지 추론 능력 평가가 아닙니다.

**assessment, hypotheses, limitations, next_checks의 자유 서술은 별도 검토가 필요합니다.**
`semantic_review`는 pending으로 유지합니다. `generated`는 구조·관측값 계약 통과이며
서비스 정상 판정이나 보고서 전체의 사실성 보증이 아닙니다.

```bash
uv run pytest -q
uv run ruff check .
```

테스트는 실제 Grafana·Ollama 대신 합성 응답을 사용하고 Langfuse 전송을 끕니다.
Agent 테스트에는 별도 프로세스 간 재개, 호출 예산, 중복 조회, 출력 검증과 캐시 재사용이 포함됩니다.
Agent 보고서는 관측값을 파싱 결과에서 직접 복사하므로 위 v3 생성·대조 경로와 다릅니다.
자동 테스트 통과는 모델의 조사 선택이나 자유 서술 정확도를 보장하지 않습니다.
