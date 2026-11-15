"""The remote marker parser (#192, T17c).

A remote harness ends with `ORCH-HANDOFF: <comment-id>`. The waiter now
accepts the marker in backticks, finds it anywhere in the last 20 lines of
the harness's output, and runs T17a's handoff checks (lib/handoff_check.py)
on the comment it names before the job is done.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from lib.queue import job_from_row
from lib.waiter import MARKER_SEARCH_LINES, handoff_marker
from tests.handoff_store import env_store, serve_handoff, serve_job
from tests.test_waiter import FakeSpawn, enqueue
from tests.test_waiter_remote import _finish_run, make_waiter, remote_machines


def _row(db: Path, job_id: str) -> tuple[str, str | None]:
    with sqlite3.connect(db) as connection:
        return connection.execute(
            "SELECT state, handoff_comment_id FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()


def _job(db: Path, job_id: str):
    with sqlite3.connect(db) as connection:
        connection.row_factory = sqlite3.Row
        return job_from_row(
            connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        )


def _remote(tmp_path: Path, log: list[str] | None = None):
    machines = remote_machines(tmp_path, box_enabled=False)
    spawn = FakeSpawn()
    waiter, queue, db = make_waiter(tmp_path, machines, spawn, log=log)
    job_id = enqueue(queue, "a")
    waiter.step()
    return waiter, spawn, db, job_id


# --- backticks ---------------------------------------------------------------


def test_a_backticked_marker_is_accepted() -> None:
    assert handoff_marker("done\n`ORCH-HANDOFF: 4242`\n") == "4242"
    assert handoff_marker("```\nORCH-HANDOFF: 4242\n```\n") == "4242"
    assert handoff_marker("`ORCH-HANDOFF: failed`") == "failed"
    uuid = "0b8f5c2e-1d2a-4c3b-9e8f-1234567890ab"
    assert handoff_marker(f"`ORCH-HANDOFF: {uuid}`") == uuid


def test_only_matched_backticks_around_the_whole_marker_count() -> None:
    for text in ("`ORCH-HANDOFF: 4242", "ORCH-HANDOFF: 4242`", "ORCH-HANDOFF: `4242`",
                 "``ORCH-HANDOFF: 4242`", "say `ORCH-HANDOFF: 4242`"):
        assert handoff_marker(text) is None, text


def test_a_backticked_marker_finishes_a_remote_run(tmp_path: Path) -> None:
    waiter, spawn, db, job_id = _remote(tmp_path)
    serve_job(db, job_id, "4242")
    _finish_run(tmp_path, job_id, spawn, "working\n`ORCH-HANDOFF: 4242`\n")
    waiter.step()
    assert _row(db, job_id) == ("done", "4242")


# --- the last 20 lines ---------------------------------------------------------


def test_a_marker_15_lines_from_the_end_is_found() -> None:
    text = "ORCH-HANDOFF: 4242\n" + "".join(f"summary line {n}\n" for n in range(14))
    assert len(text.splitlines()) == 15
    assert handoff_marker(text) == "4242"


def test_a_marker_past_the_last_20_lines_is_not_found() -> None:
    assert MARKER_SEARCH_LINES == 20
    found = "ORCH-HANDOFF: 4242\n" + "x\n" * 19
    assert handoff_marker(found) == "4242"
    assert handoff_marker("ORCH-HANDOFF: 4242\n" + "x\n" * 20) is None


def test_blank_and_runner_lines_do_not_use_up_the_20() -> None:
    padding = "\n\norch job run: released pc slot of job x\n" * 30
    assert handoff_marker("ORCH-HANDOFF: 4242\n" + "x\n" * 10 + padding) == "4242"


def test_the_last_marker_in_the_window_decides() -> None:
    assert handoff_marker("ORCH-HANDOFF: 1\nprose\nORCH-HANDOFF: 2\nprose\n") == "2"
    assert handoff_marker("ORCH-HANDOFF: 1\nprose\nORCH-HANDOFF: failed\n") == "failed"
    # A malformed later marker means no completion, as before.
    assert handoff_marker("ORCH-HANDOFF: 1\nORCH-HANDOFF: $(id)\nprose\n") is None
    assert handoff_marker("ORCH-HANDOFF: 1\n`ORCH-HANDOFF: <comment-id>`\n") is None


def test_a_marker_15_lines_from_the_end_finishes_a_remote_run(tmp_path: Path) -> None:
    waiter, spawn, db, job_id = _remote(tmp_path)
    serve_job(db, job_id, "4242")
    summary = "".join(f"summary {n}\n" for n in range(14))
    _finish_run(tmp_path, job_id, spawn, "working\nORCH-HANDOFF: 4242\n" + summary)
    waiter.step()
    assert _row(db, job_id) == ("done", "4242")


# --- T17a's checks on the remote path ----------------------------------------


def test_a_remote_marker_with_a_bad_id_fails_completion(tmp_path: Path) -> None:
    log: list[str] = []
    waiter, spawn, db, job_id = _remote(tmp_path, log)
    # Nothing is served for 4242: the comment does not exist.
    _finish_run(tmp_path, job_id, spawn, "ORCH-HANDOFF: 4242\n")
    waiter.step()
    assert _row(db, job_id) == ("failed", None)
    assert any("does not exist" in line and job_id in line for line in log)
    with sqlite3.connect(db) as connection:
        events = connection.execute(
            "SELECT event, detail FROM job_events WHERE job_id = ?", (job_id,)
        ).fetchall()
        flags = connection.execute("SELECT reason FROM needs_attention").fetchall()
    assert [event for event, _ in events] == ["submitted-invalid"]
    assert "4242" in events[0][1]
    assert flags == [("submitted-invalid",)]


def test_a_remote_marker_naming_a_comment_that_does_not_fit_fails_completion(
    tmp_path: Path,
) -> None:
    log: list[str] = []
    waiter, spawn, db, job_id = _remote(tmp_path, log)
    serve_handoff(env_store(), _job(db, job_id), "4242", **{"from": "codex-bot"})
    _finish_run(tmp_path, job_id, spawn, "ORCH-HANDOFF: 4242\n")
    waiter.step()
    assert _row(db, job_id) == ("failed", None)
    assert any("from=codex-bot is not claude-bot" in line for line in log)


def test_a_remote_marker_from_an_untrusted_login_fails_completion(tmp_path: Path) -> None:
    waiter, spawn, db, job_id = _remote(tmp_path)
    serve_handoff(env_store(), _job(db, job_id), "4242", login="stranger")
    _finish_run(tmp_path, job_id, spawn, "ORCH-HANDOFF: 4242\n")
    waiter.step()
    assert _row(db, job_id) == ("failed", None)


def test_a_github_read_that_fails_does_not_complete_the_job(tmp_path: Path, monkeypatch) -> None:
    log: list[str] = []
    waiter, spawn, db, job_id = _remote(tmp_path, log)
    serve_job(db, job_id, "4242")
    monkeypatch.delenv("FAKE_GH_STORE")  # the fake gh now refuses every read
    _finish_run(tmp_path, job_id, spawn, "ORCH-HANDOFF: 4242\n")
    waiter.step()
    assert _row(db, job_id) == ("failed", None)
    assert any("could not check handoff 4242" in line for line in log)


def test_a_remote_marker_whose_comment_fits_is_done(tmp_path: Path) -> None:
    waiter, spawn, db, job_id = _remote(tmp_path)
    serve_job(db, job_id, "4242")
    _finish_run(tmp_path, job_id, spawn, "ORCH-HANDOFF: 4242\n")
    waiter.step()
    assert _row(db, job_id) == ("done", "4242")
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'job_events'"
        ).fetchone() == (0,) or connection.execute(
            "SELECT COUNT(*) FROM job_events WHERE job_id = ?", (job_id,)
        ).fetchone() == (0,)
