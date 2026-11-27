import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from lib import job_detail
from lib.dashboard import collect_snapshot
from lib.queue import JobRequest, SQLiteJobQueue, job_from_row
from lib.state_backup import _refuse_spilled_without_file
from tests.test_dashboard import NOW, add_job, add_ticket, seeded

REPO = "maxharris1/cleffy"
MERGED = "90"
OPEN = "91"
SMALL = {"pr_number": "90", "resume": True, "retry_of": "job-0", "round": "2/3"}
COLD = ("cold-done", "cold-cancelled", "cold-failed-handled", "cold-presplit")
KEPT = ("live-queued", "live-running", "failed-unhandled", "open-done")


def _dump(data: object) -> str:
    return json.dumps(data, separators=(",", ":"), sort_keys=True)


def _bulky(**extra: object) -> str:
    return _dump({"brief": "b" * 5000, "live": {"log": "l" * 3000}, "note": "n" * 1022, **SMALL, **extra})


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    db, log = seeded(tmp_path)
    queue = SQLiteJobQueue(db, clock=lambda: NOW)
    presplit = queue.enqueue(
        JobRequest(
            repo=REPO,
            ticket_id=MERGED,
            action="build",
            recipient="claude",
            source_comment_id="comment-presplit",
            timeout_seconds=5400,
            detail=_bulky(answers="a" * 50),
        ),
        dedupe_key="presplit",
    )
    with sqlite3.connect(db) as connection:
        add_ticket(connection, REPO, MERGED, state="merged")
        add_ticket(connection, REPO, OPEN, state="building")
        connection.execute("UPDATE jobs SET id = 'cold-presplit', state = 'done' WHERE id = ?", (presplit.id,))
        (tmp_path / "job-detail" / f"{presplit.id}.json").rename(tmp_path / "job-detail" / "cold-presplit.json")
        for job_id, state in (
            ("cold-done", "done"),
            ("cold-cancelled", "cancelled"),
            ("cold-failed-handled", "failed"),
            ("live-queued", "queued"),
            ("live-running", "running"),
            ("failed-unhandled", "failed"),
        ):
            add_job(connection, job_id, state=state, ticket_id=MERGED, detail=_bulky())
        add_job(connection, "open-done", state="done", ticket_id=OPEN, detail=_bulky())
        add_job(connection, "cold-plain", state="done", ticket_id=MERGED, detail="stuck: " + "p" * 5000)
        add_job(connection, "cold-array", state="done", ticket_id=MERGED, detail=_dump(["x" * 5000]))
        connection.execute("UPDATE jobs SET failure_handled = 1 WHERE id = 'cold-failed-handled'")
    return db, log


def _connect(db: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db, isolation_level=None)
    connection.row_factory = sqlite3.Row
    return connection


def _rows(db: Path) -> dict[str, tuple]:
    with sqlite3.connect(db) as connection:
        return {row[0]: tuple(row) for row in connection.execute("SELECT id, * FROM jobs ORDER BY rowid")}


def _reads(db: Path) -> dict[str, tuple[object, object]]:
    connection = _connect(db)
    try:
        return {
            row["id"]: (
                job_from_row(row, connection).request.detail,
                job_detail.load_row(connection, row["id"], row["detail"]),
            )
            for row in connection.execute("SELECT * FROM jobs ORDER BY rowid")
        }
    finally:
        connection.close()


def _spill(db: Path) -> int:
    connection = _connect(db)
    try:
        return job_detail.spill_cold(connection)
    finally:
        connection.close()


def _parsed(reads: dict[str, tuple[object, object]]) -> dict[str, list[object]]:
    def parse(value: object) -> object:
        try:
            return json.loads(str(value))
        except ValueError:
            return value

    return {job_id: [parse(value) for value in pair] for job_id, pair in reads.items()}


def test_only_cold_rows_lose_their_large_keys(tmp_path: Path) -> None:
    db, _ = _fixture(tmp_path)
    before = _rows(db)

    assert _spill(db) == len(COLD)

    after = _rows(db)
    unchanged = [job_id for job_id in before if job_id not in COLD]
    assert {job_id: after[job_id] for job_id in unchanged} == {job_id: before[job_id] for job_id in unchanged}
    assert set(KEPT) <= set(unchanged)
    with sqlite3.connect(db) as connection:
        for job_id in COLD:
            (detail,) = connection.execute("SELECT detail FROM jobs WHERE id = ?", (job_id,)).fetchone()
            assert len(detail.encode()) < 2048
            assert json.loads(detail) == {**SMALL, "note": "n" * 1022, "spilled": ["brief", "live"]} | (
                {"spilled": ["answers", "brief", "live"]} if job_id == "cold-presplit" else {}
            )
    moved = json.loads((tmp_path / "job-detail" / "cold-presplit.json").read_text())
    assert moved == {"answers": "a" * 50, "brief": "b" * 5000, "live": {"log": "l" * 3000}}


def test_reads_and_the_snapshot_are_the_same_after_the_spill(tmp_path: Path) -> None:
    db, log = _fixture(tmp_path)
    small = "SELECT id, " + ", ".join(f"json_extract(detail, '$.{key}')" for key in SMALL) + " FROM jobs WHERE json_valid(detail)"
    reads = _reads(db)
    with sqlite3.connect(db) as connection:
        extracted = connection.execute(small).fetchall()
    snapshot = collect_snapshot(db, log, now=NOW)

    _spill(db)

    after = _reads(db)
    assert _parsed(after) == _parsed(reads)
    for job_id in COLD:
        job, loaded = after[job_id]
        assert json.loads(str(job)) == json.loads(str(loaded)) == json.loads(_bulky(**({"answers": "a" * 50} if job_id == "cold-presplit" else {})))
    with sqlite3.connect(db) as connection:
        assert connection.execute(small).fetchall() == extracted
    assert collect_snapshot(db, log, now=NOW) == snapshot


def test_requeue_of_a_spilled_cancelled_job_is_retired(tmp_path: Path) -> None:
    db, _ = _fixture(tmp_path)
    _spill(db)
    queue = SQLiteJobQueue(db, clock=lambda: NOW)
    connection = _connect(db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        assert queue.requeue("cold-cancelled", "answer-rejected", connection=connection) is False
        connection.execute("COMMIT")
        assert connection.execute("SELECT state FROM jobs WHERE id = 'cold-cancelled'").fetchone()[0] == "cancelled"
        events = connection.execute(
            "SELECT event, detail FROM job_events WHERE job_id = 'cold-cancelled' ORDER BY rowid"
        ).fetchall()
    finally:
        connection.close()
    assert [tuple(event) for event in events] == [("cancelled", "finished:answer-rejected")]


def test_a_request_built_from_a_spilled_job_enqueues_with_the_full_detail(tmp_path: Path) -> None:
    db, _ = _fixture(tmp_path)
    _spill(db)
    queue = SQLiteJobQueue(db, clock=lambda: NOW)
    connection = _connect(db)
    try:
        spilled = job_from_row(connection.execute("SELECT * FROM jobs WHERE id = 'cold-presplit'").fetchone(), connection)
    finally:
        connection.close()
    full = json.loads(_bulky(answers="a" * 50))
    assert json.loads(str(spilled.request.detail)) == full

    again = queue.enqueue(replace(spilled.request, ticket_id=OPEN), dedupe_key="again")

    assert json.loads(str(again.request.detail)) == full
    connection = _connect(db)
    try:
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (again.id,)).fetchone()
        assert json.loads(str(job_from_row(row, connection).request.detail)) == full
    finally:
        connection.close()


def test_a_second_spill_moves_nothing(tmp_path: Path) -> None:
    db, _ = _fixture(tmp_path)
    _spill(db)
    rows = _rows(db)
    files = {path.name: path.read_bytes() for path in (tmp_path / "job-detail").iterdir()}

    assert _spill(db) == 0

    assert _rows(db) == rows
    assert {path.name: path.read_bytes() for path in (tmp_path / "job-detail").iterdir()} == files


def test_a_failed_update_after_the_file_write_reads_the_same(tmp_path: Path) -> None:
    db, _ = _fixture(tmp_path)
    rows = _rows(db)
    reads = _reads(db)
    connection = _connect(db)
    try:
        connection.execute(
            "CREATE TEMP TRIGGER refuse BEFORE UPDATE OF detail ON jobs BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
        with pytest.raises(sqlite3.Error, match="injected"):
            job_detail.spill_cold(connection)
        assert not connection.in_transaction
    finally:
        connection.close()

    assert "brief" in json.loads((tmp_path / "job-detail" / "cold-presplit.json").read_text())
    assert _rows(db) == rows
    assert _parsed(_reads(db)) == _parsed(reads)

    assert _spill(db) == len(COLD)
    assert _parsed(_reads(db)) == _parsed(reads)


def test_non_object_detail_is_left_alone(tmp_path: Path) -> None:
    db, _ = _fixture(tmp_path)
    before = _rows(db)

    _spill(db)

    after = _rows(db)
    assert (after["cold-plain"], after["cold-array"]) == (before["cold-plain"], before["cold-array"])
    assert not (tmp_path / "job-detail" / "cold-plain.json").exists()
    assert not (tmp_path / "job-detail" / "cold-array.json").exists()


def test_the_backup_move_check_accepts_spilled_rows(tmp_path: Path) -> None:
    db, _ = _fixture(tmp_path)
    _spill(db)

    _refuse_spilled_without_file(db)


def test_the_survey_names_the_large_key_and_writes_nothing(tmp_path: Path) -> None:
    db, _ = _fixture(tmp_path)
    with sqlite3.connect(db) as connection:
        add_job(connection, "planted", state="done", ticket_id=MERGED, detail=_dump({"planted_blob": "z" * 60_000, **{f"k{index}": index for index in range(10)}}))
    checkpoint = sqlite3.connect(db)
    checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    checkpoint.close()
    files = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}

    connection = _connect(db)
    try:
        survey = job_detail.survey(connection)
    finally:
        connection.close()

    assert {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()} == files
    assert survey.largest_keys[0] == ("planted_blob", len(_dump("z" * 60_000)))
    assert len(survey.largest_keys) == 10
    assert [size for _, size in survey.largest_keys] == sorted((size for _, size in survey.largest_keys), reverse=True)
    assert survey.non_object == 2
    assert survey.classes["cold"].rows == len(COLD) + 3
    assert survey.classes["live"].rows >= 3
    assert survey.classes["replayable"].rows >= 1
    with sqlite3.connect(db) as connection:
        total = connection.execute("SELECT COUNT(*), SUM(length(CAST(detail AS BLOB))) FROM jobs").fetchone()
    assert sum(share.rows for share in survey.classes.values()) == total[0]
    assert sum(share.inline_bytes for share in survey.classes.values()) == total[1]
    moved = 4 * (len(_dump("b" * 5000)) + len(_dump({"log": "l" * 3000}))) + len(_dump("z" * 60_000))
    assert survey.movable_bytes == moved

    _spill(db)
    connection = _connect(db)
    try:
        assert job_detail.survey(connection).movable_bytes == 0
    finally:
        connection.close()
