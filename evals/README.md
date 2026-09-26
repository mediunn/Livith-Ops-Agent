# 운영 보고서 평가

`cases.json`은 운영 데이터가 아닌 합성 사례 4개와 검토 기준입니다.
요청률 0, 데이터 없음, 경고 로그, 로그 잘림을 각각 다룹니다.
기대 분류와 검토 기준은 모델에 전달하지 않습니다.

프로젝트 루트에서 실행합니다.

```bash
# 모델 호출 없이 합성 근거 생성
uv run python evaluate_reports.py --dry-run

# 기본 3B 모델로 전체 사례 실행
uv run python evaluate_reports.py

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
