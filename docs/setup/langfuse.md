# Langfuse 연결

[문서 목록](../README.md) · [프로젝트 사용법](../../README.md)

LLM 실행은 로컬 Ollama가 수행하고, Langfuse는 그 실행의 관측과 평가 기록을 보관합니다.
현재 연동 대상은 외부 API 조사와 고정 보고서이며, HTTP 조사 기록은 로컬에 저장합니다.
아래 명령은 프로젝트 루트에서 실행합니다.

## 프로젝트와 키 준비

1. [Langfuse Cloud](https://cloud.langfuse.com)에 가입합니다.
2. 조직을 만들고 `Livith-Ops-Agent` 프로젝트를 생성합니다.
3. 프로젝트 Settings → API Keys에서 프로젝트 키를 생성합니다.
4. 화면의 Public Key, Secret Key, 서버 주소를 **기존 `.env`에 추가**합니다.
   기존 Grafana 설정을 덮어쓰지 마세요. 키를 채팅이나 Git에 올리지 않습니다.

```dotenv
OPS_LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-lf-여기에_실제_키
LANGFUSE_SECRET_KEY=sk-lf-여기에_실제_키
LANGFUSE_BASE_URL=https://cloud.langfuse.com
```

서버 주소는 프로젝트 리전과 일치해야 합니다. EU는 `https://cloud.langfuse.com`,
US는 `https://us.cloud.langfuse.com`, Japan은 `https://jp.cloud.langfuse.com`입니다.
프로젝트 화면에 표시된 주소를 사용하세요.
[공식 설정 안내](https://langfuse.com/docs/observability/get-started)

## 연결 확인과 첫 실행

```bash
uv run python -m ops_agent.cli.check_langfuse
uv run python -m ops_agent.cli.evaluate_reports --case warning_present
```

첫 명령은 인증을 확인하고 작은 합성 trace를 보낸 뒤, 해당 trace를 서버 API에서
실제로 조회한 경우에만 종료 코드 0을 반환합니다. 제한된 대기 시간 내 조회되지 않으면
전송 실패 또는 인덱싱 지연으로 보고 종료 코드 1을 반환합니다. 두 번째는 합성 평가 사례 하나를
로컬 모델로 실행하고 자동 검사 점수까지 기록합니다. 평가 실패로 종료 코드 1이
나와도 전송 실패를 의미하지 않습니다. `telemetry` 필드를 별도로 확인하세요.

프로젝트의 Traces에서 `ops-agent-connection-check`와 `ops-report`를 확인합니다.
보고서 trace 안의 `ollama-report` generation에서 모델·입력·출력·토큰·시간을 볼 수
있습니다. 실제 저장된 운영 근거로 실행하려면 다음 명령을 사용합니다.

```bash
uv run python -m ops_agent.reporting.generator
```

새 근거 수집까지 한 번에 실행하려면 `uv run python -m ops_agent.cli.run_investigation` 명령을 사용합니다.
이때 `ops-investigation` 아래에 Prometheus·Loki 수집과 보고서 생성이 연결됩니다.
`uv run python -m ops_agent.cli.run_agent`를 사용하면 실행·재개별 `ops-agent` trace에 조회 도구와
`ollama-planner`가 연결됩니다. 조사 ID는 metadata에 기록됩니다.

## 기록 범위

- 프롬프트 원문, 버전·해시, 근거의 조회 조건과 요약, LLM 원문 응답과 최종 보고서
- 모델 이름, 생성 옵션, 입력·출력 토큰 수, 실행 시간과 실패 종류
- Agent 판단 ID·시도 번호·수정 대상 ID·형식/도구/근거 참조 검증 결과
- 평가 실행 ID, 사례 ID, 데이터셋 버전, 합성 데이터 여부
- `auto_schema_and_references`, `auto_assessment`, `auto_hypotheses_policy` 점수
- v3 평가의 `auto_observation_values`: 구조화된 관측값이 원본 요약과 일치하는지 검사

추적을 켜면 위 데이터는 설정한 Langfuse 서버로 전송됩니다. 원본 Grafana 응답 전체,
로그 본문, `.env` 및 로컬 파일 경로는 추적 입력에 넣지 않습니다. 근거 요약에도
서비스 라벨이나 조회 조건은 포함됩니다. 자동 점수는 조건 통과 여부일 뿐 내용의
정확도 점수가 아니며, 별도 내용 검토는 계속 필요합니다.

저장된 근거로 보고서만 생성하면 기존 Grafana 수집을 새 호출로 기록하지 않습니다.
Ollama 호출 자체의 시간은 generation에, 보고서 처리 과정은 상위 span에 기록합니다.
로컬 모델의 API 비용이나 전력 비용을 임의의 금액으로 기록하지 않습니다.

## 실패와 비활성화

`OPS_LANGFUSE_ENABLED=false`가 기본값입니다. 키가 있어도 이 값이 false이면
추적을 보내지 않습니다. SDK 예외가 나도 보고서 생성과 로컬 저장은 계속됩니다.

로컬 결과의 `telemetry.status`는 `disabled`, `recording`, `error`,
`flush_attempted` 중 하나입니다. `flush_attempted`는 SDK 전송을 시도했다는 뜻이며
서버 수신 완료를 보장하지 않습니다. 실제 수신 여부는 trace 화면에서 확인합니다.
Agent의 잘못된 판단 응답에는 `schema_error`, `invalid_action`, `unknown_evidence`,
`unavailable_evidence`, `output_truncated` 같은 코드가 generation의 검증 metadata와
오류 메시지에 기록됩니다. 수정 응답은 별도 generation으로 연결합니다.
점수 전송 상태는 `telemetry.scores_status`로 별도 기록합니다.

인증은 성공하지만 trace 전송에서 `CERTIFICATE_VERIFY_FAILED`가 발생한다면
인증 요청과 OpenTelemetry 전송이 서로 다른 CA 설정을 사용했을 수 있습니다.
코드는 별도 CA 설정이 없을 때 trace 전송에 `certifi` CA 묶음을 사용합니다.
인증서 검증을 끄지 않습니다. 이미 설정한 `OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE`,
`OTEL_EXPORTER_OTLP_CERTIFICATE`, `SSL_CERT_FILE`, `SSL_CERT_DIR`는 덮어쓰지 않습니다.
회사 프록시 등 별도 신뢰 체인이 필요하면 해당 조직에서 제공하는 CA 묶음을 사용하세요.

Langfuse SDK의 `flush()` 자체는 전송 실패를 예외로 올리지 않을 수 있고,
trace URL도 서버에 기록되기 전에 만들어질 수 있습니다. URL 출력만으로 전송 성공을
판정하지 않으며, 연결 점검은 서버 조회 결과까지 확인합니다.


[추적 SDK 문서](https://langfuse.com/docs/observability/sdk/instrumentation),
[평가 점수 SDK 문서](https://langfuse.com/docs/evaluation/evaluation-methods/scores-via-sdk)
