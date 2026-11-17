"""The bulky keys of a job's detail JSON live in <db dir>/job-detail/<id>.json (#653).

A fixed key list, not a size threshold, decides what leaves jobs.detail, so
a raw SQL reader of any other key always finds it in the row.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path

SPILLED = ("answers", "brief_text", "evidence", "instruction", "plan_text", "unmapped")
MARKER = "spilled"
DIRECTORY = "job-detail"


class JobDetailError(ValueError):
    pass


def root(connection: sqlite3.Connection) -> Path | None:
    for _seq, name, file in connection.execute("PRAGMA database_list"):
        if name == "main":
            return Path(file).parent if file else None
    return None


def split(detail: str | None) -> tuple[str | None, dict[str, object] | None]:
    data = _object(detail)
    if data is None:
        return detail, None
    marked = data.pop(MARKER, None) is not None
    spill = {key: data.pop(key) for key in SPILLED if key in data}
    if not spill:
        return (_dump(data) if marked else detail), None
    data[MARKER] = sorted(spill)
    return _dump(data), spill


def write(directory: Path | None, job_id: str, spill: dict[str, object]) -> None:
    if directory is None:
        raise JobDetailError(f"job {job_id}: no database directory to spill detail into")
    path = _path(directory, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(_dump(spill))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load(directory: Path | None, job_id: str, detail: str | None) -> str | None:
    data = _object(detail)
    if data is None or MARKER not in data:
        return detail
    if directory is None:
        raise JobDetailError(f"job {job_id}: spilled detail but no database directory")
    del data[MARKER]
    try:
        spill = json.loads(_path(directory, job_id).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _dump(data)
    if not isinstance(spill, dict):
        raise JobDetailError(f"job {job_id}: spilled detail is not a JSON object")
    return _dump({**data, **spill})


def load_row(connection: sqlite3.Connection, job_id: str, detail: str | None) -> str | None:
    return load(root(connection), job_id, detail)


def _object(detail: str | None) -> dict[str, object] | None:
    if not detail:
        return None
    try:
        data = json.loads(detail)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _path(directory: Path, job_id: str) -> Path:
    return directory / DIRECTORY / f"{job_id}.json"


def _dump(data: dict[str, object]) -> str:
    return json.dumps(data, separators=(",", ":"), sort_keys=True)
