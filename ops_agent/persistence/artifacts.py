import json
import os
from pathlib import Path
from uuid import uuid4


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON 객체가 아닙니다: {path.name}")
    return value


def save_json(path: Path, value: dict) -> None:
    """완성된 임시 파일로 교체해 부분 기록을 피한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")

    with temporary.open("w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())

    os.replace(temporary, path)


def cached_query(
    path: Path,
    *,
    thread_id: str,
    tool: str,
    arguments: dict,
) -> dict | None:
    if not path.exists():
        return None

    record = read_json(path)
    if (
        record.get("thread_id") != thread_id
        or record.get("tool") != tool
        or record.get("arguments") != arguments
    ):
        raise ValueError("저장된 근거의 조사 ID 또는 조회 조건이 다릅니다.")

    return record
