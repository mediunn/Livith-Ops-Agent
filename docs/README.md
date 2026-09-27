# 프로젝트 문서

[프로젝트 사용법](../README.md)

| 문서 | 내용 |
|---|---|
| [Langfuse 연결](setup/langfuse.md) | 설정, 합성 연결 확인, 전송 범위 |
| [Agent 동작과 설계](architecture/agent-workflow.md) | 조사·종료 판단, 예산, 시간 범위, SQLite와 메모리, Sentry 확장 후보 |
| [다음 작업](roadmap.md) | 구현 상태와 후속 작업 순서 |
| [Git과 커밋 구성](private/version-control.md) | 공개·비공개 구분, 현재 변경의 파일별 커밋 명령 |
| [고정 보고서 평가](../evals/README.md) | 합성 사례, 실행법, 검증 범위 |
| [v2 비교 기록](../evals/v2-comparison.md) | 기존 고정 보고서 비교 결과 |
| [v3 검증 기록](../evals/v3-validation.md) | 관측값 검증과 실제 수집 통합 실행 결과 |

공개 가능한 설명은 이 폴더에, 실행 결과는 Git에서 제외된 `artifacts/`에 둡니다.
개인 계획과 대화 원문은 `docs/private/`에 보관하며 해당 폴더 전체를 Git에서 제외합니다.
개인 파일은 공개 문서의 상대 링크 대상으로 사용하지 않습니다.
