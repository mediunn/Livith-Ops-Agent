"""HTTP·외부 API 결과를 같은 사용자 답변 또는 Markdown 보고서로 표현한다."""

import json
import os
from pathlib import Path
from uuid import uuid4


def render_report(result: dict) -> str:
    answer = result.get("answer")
    lines = [
        "# 운영 조사 결과",
        "",
        result.get("symptom") or "운영 관측 조사",
        "",
        f"- 상태: {result['status']}",
        f"- 종료 사유: {result.get('stop_reason', 'unknown')}",
        "",
        "## 답변",
        "",
        answer["text"] if answer else "모델 답변이 생성되지 않았습니다.",
        "",
        "## 확인 한계",
        "",
    ]
    limits = answer["limitations"] if answer else result.get("limitations", [])
    lines.extend(f"- {limit}" for limit in limits)
    if not limits:
        lines.append(
            "- 별도로 기록된 확인 한계가 없습니다. 의미적 정확성 검토는 필요합니다."
        )
    lines.extend(["", "## 근거 목록", ""])
    for fact in result.get("facts", []):
        lines.append(
            f"- {fact['evidence_id']}: {fact.get('action', fact.get('tool', 'observation'))}"
            f" / {fact.get('status', 'unknown')}"
        )
    if not result.get("facts"):
        lines.append("- 수집된 관측이 없습니다.")
    return "\n".join(lines) + "\n"


def print_result(
    result: dict, *, output="answer", directory: Path | None = None
) -> None:
    if output == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result["status"] == "paused":
        print("조사를 저장하고 일시 중단했습니다.")
    elif output == "report":
        markdown = render_report(result)
        if directory is not None:
            path = directory / "report.md"
            temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
            temporary.write_text(markdown, encoding="utf-8")
            os.replace(temporary, path)
        print(markdown, end="")
    elif answer := result.get("answer"):
        print(answer["text"])
        if result["status"] == "incomplete":
            print(f"\n조사 미완료: {result['stop_reason']}")
        if answer["evidence_ids"]:
            print("\n근거: " + ", ".join(answer["evidence_ids"]))
        if answer["limitations"]:
            print("\n확인 한계:")
            for limitation in answer["limitations"]:
                print(f"- {limitation}")
    else:
        print("사용자 답변이 생성되지 않았습니다.")
        print(f"종료 사유: {result.get('stop_reason', 'unknown')}")
        print(f"저장된 근거: {len(result.get('facts', []))}건")
        if directory is not None:
            print(f"전체 결과: {directory / 'report.json'}")
