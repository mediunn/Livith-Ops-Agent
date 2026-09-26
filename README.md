# Livith OpsAgent

Grafana MCP로 지표와 로그를 수집하고 로컬 Ollama로 운영 보고서 초안을 만듭니다.
현재는 고정 쿼리를 실행하는 CLI이며, 자율적으로 도구를 선택하거나 운영 조치를 실행하지 않습니다.

## 시작

```bash
uv sync
```

`.env.example`을 참고해 `.env`에 Grafana URL·읽기 전용 서비스 계정 토큰을
설정하고 로컬 Ollama를 실행하세요. Langfuse는 선택 사항이며
[설정 안내](LANGFUSE_SETUP.md)에 따라 연결할 수 있습니다.

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
uv run python generate_report.py --loki-evidence /absolute/path/to/loki-evidence.json
uv run python evaluate_reports.py --prompt-versions ops_report_v3
```

`generate_report.py`의 경로를 생략하면 최상위 `artifacts/`의 기존 Loki 파일을
선택합니다. 통합 실행 결과를 재사용할 때는 해당 실행의 Loki 파일을 명시하세요.
평가 방법과 v1·v2 재실행은 [평가 문서](evals/README.md)를 참고하세요.

## v3의 검증 범위

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
