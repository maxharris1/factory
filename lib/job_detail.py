"""The bulky keys of a job's detail JSON live in <db dir>/job-detail/<id>.json (#653).

A fixed key list decides what leaves jobs.detail on enqueue, so a raw SQL
reader of any other key finds it in the row while the job can still run.
Once a job is finished and its ticket is closed (a cold job), spill_cold also
moves any top-level key whose compact JSON is over INLINE_LIMIT bytes.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

SPILLED = ("answers", "brief_text", "decisions", "evidence", "instruction", "plan_text", "unmapped")
MARKER = "spilled"
DIRECTORY = "job-detail"
BATCH = 200
INLINE_LIMIT = 1024
FINISHED_TICKET_STATES = ("merged", "done", "abandoned")
CLASSES = ("live", "replayable", "cold")
_FINISHED = """EXISTS (
    SELECT 1 FROM ticket_state
    WHERE ticket_state.repo = lower(jobs.repo)
      AND ticket_state.ticket_id = jobs.ticket_id
      AND ticket_state.workflow_state IN ({states})
)"""
_CLASSIFY = """(CASE
    WHEN (state IN ('done', 'cancelled') OR (state = 'failed' AND failure_handled = 1))
         AND {finished} THEN 'cold'
    WHEN state IN ('queued', 'running') OR (state = 'failed' AND failure_handled = 0) THEN 'live'
    ELSE 'replayable'
END)"""


class JobDetailError(ValueError):
    pass


@dataclass(frozen=True)
class Share:
    rows: int
    inline_bytes: int


@dataclass(frozen=True)
class Survey:
    classes: dict[str, Share]
    movable_bytes: int
    non_object: int
    largest_keys: list[tuple[str, int]]


def root(connection: sqlite3.Connection) -> Path | None:
    for _seq, name, file in connection.execute("PRAGMA database_list"):
        if name == "main":
            return Path(file).parent if file else None
    return None


def split(detail: str | None) -> tuple[str | None, dict[str, object] | None]:
    data = as_object(detail)
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
    data = as_object(detail)
    if data is None or MARKER not in data:
        return detail
    if directory is None:
        raise JobDetailError(f"job {job_id}: spilled detail but no database directory")
    del data[MARKER]
    return _dump({**data, **_spilled(directory, job_id)})


def load_row(connection: sqlite3.Connection, job_id: str, detail: str | None) -> str | None:
    return load(root(connection), job_id, detail)


def spill_existing(connection: sqlite3.Connection) -> int:
    directory = root(connection)
    moved = 0
    last = 0
    while True:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT rowid, id, detail FROM jobs WHERE rowid > ? ORDER BY rowid LIMIT ?",
            (last, BATCH),
        ).fetchall()
        for rowid, job_id, detail in rows:
            last = rowid
            data = as_object(detail)
            if data is None or MARKER in data:
                continue
            slim, spill = split(detail)
            if spill is None:
                continue
            write(directory, str(job_id), spill)
            connection.execute("UPDATE jobs SET detail = ? WHERE rowid = ?", (slim, rowid))
            moved += 1
        connection.commit()
        if len(rows) < BATCH:
            return moved


def spill_cold(connection: sqlite3.Connection) -> int:
    directory = root(connection)
    classify, params = _classification(connection)
    moved = 0
    last = 0
    while True:
        connection.execute("BEGIN IMMEDIATE")
        try:
            rows = connection.execute(
                f"SELECT rowid, id, detail FROM jobs WHERE rowid > ? AND {classify} = 'cold' "
                "ORDER BY rowid LIMIT ?",
                (last, *params, BATCH),
            ).fetchall()
            for rowid, job_id, detail in rows:
                last = rowid
                data = as_object(detail)
                if data is None:
                    continue
                moving = [key for key, size in _key_sizes(data) if size > INLINE_LIMIT]
                if not moving:
                    continue
                spill = {key: data.pop(key) for key in moving}
                marked = data.get(MARKER)
                existing = _spilled(directory, str(job_id)) if marked is not None else {}
                write(directory, str(job_id), {**existing, **spill})
                data[MARKER] = sorted(set(marked or ()) | set(spill))
                connection.execute("UPDATE jobs SET detail = ? WHERE rowid = ?", (_dump(data), rowid))
                moved += 1
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        if len(rows) < BATCH:
            return moved


def survey(connection: sqlite3.Connection) -> Survey:
    classify, params = _classification(connection)
    shares = {name: [0, 0] for name in CLASSES}
    keys: dict[str, int] = {}
    movable = 0
    non_object = 0
    cursor = connection.execute(
        f"SELECT {classify}, detail, coalesce(length(CAST(detail AS BLOB)), 0) FROM jobs", params
    )
    for kind, detail, size in cursor:
        shares[kind][0] += 1
        shares[kind][1] += size
        data = as_object(detail)
        if data is None:
            non_object += detail is not None
            continue
        for key, key_size in _key_sizes(data):
            keys[key] = keys.get(key, 0) + key_size
            if kind == "cold" and key_size > INLINE_LIMIT:
                movable += key_size
    return Survey(
        classes={name: Share(*share) for name, share in shares.items()},
        movable_bytes=movable,
        non_object=non_object,
        largest_keys=sorted(keys.items(), key=lambda item: (-item[1], item[0]))[:10],
    )


def as_object(detail: str | None) -> dict[str, object] | None:
    if not detail:
        return None
    try:
        data = json.loads(detail)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _classification(connection: sqlite3.Connection) -> tuple[str, tuple[str, ...]]:
    has_tickets = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ticket_state'"
    ).fetchone()
    if has_tickets is None:
        return _CLASSIFY.format(finished="0"), ()
    finished = _FINISHED.format(states=", ".join("?" * len(FINISHED_TICKET_STATES)))
    return _CLASSIFY.format(finished=finished), FINISHED_TICKET_STATES


def _key_sizes(data: dict[str, object]) -> Iterator[tuple[str, int]]:
    for key, value in data.items():
        if key != MARKER:
            yield key, len(json.dumps(value, separators=(",", ":"), sort_keys=True).encode())


def _spilled(directory: Path | None, job_id: str) -> dict[str, object]:
    if directory is None:
        raise JobDetailError(f"job {job_id}: spilled detail but no database directory")
    try:
        spill = json.loads(_path(directory, job_id).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(spill, dict):
        raise JobDetailError(f"job {job_id}: spilled detail is not a JSON object")
    return spill


def _path(directory: Path, job_id: str) -> Path:
    return directory / DIRECTORY / f"{job_id}.json"


def _dump(data: dict[str, object]) -> str:
    return json.dumps(data, separators=(",", ":"), sort_keys=True)
