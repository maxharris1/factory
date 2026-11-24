"""#1464: a device that cannot run `ci.local_gate` leaves the ticket waiting.

A gate that exits 75 or cannot start is unavailable: the job finishes with its
ready, no invalid-handoff slot is used, and `local-gate-unavailable` is
recorded at the head. T2 holds the ready as `local-gate-waiting` ("gate
unavailable") and never passes it. The waiter retries at most every 600
seconds: a pass lets review queue, and a failure queues a CI fix.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatch.constants import CI_WAIT_REASONS
from lib.dispatcher import CIResult, TrackedTicket
from lib.factory_md import CheckCommand
from lib.queue import (
    INVALID_HANDOFF_EVENTS,
    LOCAL_GATE_FAILED,
    LOCAL_GATE_PASSED,
    LOCAL_GATE_UNAVAILABLE,
    PRE_READY_FAILED,
    JobRequest,
    SQLiteJobQueue,
)
from lib.waiter import LOCAL_GATE_RETRY_SECONDS
from tests.handoff_store import env_store
from tests.test_local_gate import (
    COMMENT,
    FEATURE,
    Box,
    _events,
    _factory_text,
    _remote_ready,
    remote_waiter,
    _serve_factory_md,
    _state,
    box,
    origin,
)

__all__ = ["box", "origin"]

GATE_EVENTS = (LOCAL_GATE_PASSED, LOCAL_GATE_FAILED, LOCAL_GATE_UNAVAILABLE)
HEAD = "a" * 40


def _gate(db: Path, job_id: str) -> list[tuple[str, str]]:
    return [item for item in _events(db, job_id) if item[0] in GATE_EVENTS]


def _invalid(db: Path, job_id: str) -> int:
    return sum(1 for event, _ in _events(db, job_id) if event in INVALID_HANDOFF_EVENTS)


def test_waiting_on_the_gate_is_a_ci_wait_reason() -> None:
    assert "local-gate-waiting" in CI_WAIT_REASONS


def test_exit_75_finishes_the_job_and_the_ticket_waits(box: Box, capsys) -> None:
    box.ready(_factory_text("exit 75"))

    assert box.done() == 0

    err = capsys.readouterr().err
    assert f"local_gate unavailable {COMMENT}: exit 75" in err
    assert "the ticket waits" in err
    assert box.state() == ("done", COMMENT)
    assert _gate(box.db, box.job.id) == [
        (LOCAL_GATE_UNAVAILABLE, f"handoff {COMMENT}: at {box.head}: exit 75")
    ]
    assert _invalid(box.db, box.job.id) == 0


def test_a_gate_that_cannot_start_is_unavailable(
    box: Box, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    box.ready(_factory_text("true"))

    spawn = subprocess.Popen

    def refuse(argv, *args, **kwargs):
        if argv[:2] == ["/bin/sh", "-c"]:
            raise OSError("no shell")
        return spawn(argv, *args, **kwargs)

    monkeypatch.setattr("lib.pre_ready.subprocess.Popen", refuse)

    assert box.done() == 0

    assert "local_gate unavailable" in capsys.readouterr().err
    assert box.state() == ("done", COMMENT)
    assert [event for event, _ in _gate(box.db, box.job.id)] == [LOCAL_GATE_UNAVAILABLE]
    assert _invalid(box.db, box.job.id) == 0


@pytest.mark.parametrize(("command", "reason"), [("exit 127", "exit 127"), ("exit 1", "exit 1")])
def test_other_exits_still_fail(box: Box, capsys, command: str, reason: str) -> None:
    box.ready(_factory_text(command))

    assert box.done() == 1

    assert f"local_gate failed: {reason}" in capsys.readouterr().err
    assert [event for event, _ in _gate(box.db, box.job.id)] == [LOCAL_GATE_FAILED]


def test_a_pre_ready_that_exits_75_still_fails(box: Box) -> None:
    box.ready(_factory_text("true", pre_ready="exit 75"))

    assert box.done() == 1

    assert box.state() == ("running", None)
    assert [event for event, _ in _events(box.db, box.job.id)] == [PRE_READY_FAILED]


def test_the_waiter_finishes_a_remote_ready_whose_gate_exits_75(tmp_path: Path, origin: str) -> None:
    db, job_id, log = _remote_ready(tmp_path, origin, _factory_text("exit 75"), FEATURE)

    assert _state(db, job_id) == ("done", COMMENT)
    assert _gate(db, job_id) == [(LOCAL_GATE_UNAVAILABLE, f"handoff {COMMENT}: at {origin}: exit 75")]
    assert _invalid(db, job_id) == 0
    assert any(f"local_gate unavailable {COMMENT}: exit 75" in line for line in log)


def _build(queue: SQLiteJobQueue, comment_id: str) -> str:
    queue.enqueue(
        JobRequest(
            repo="owner/repo",
            ticket_id="42",
            action="build",
            recipient="claude",
            source_comment_id="build-source",
            timeout_seconds=5400,
            detail="{}",
        ),
        dedupe_key="build-seed",
    )
    build = queue.claim("claude")
    assert build is not None
    queue.record_head_check(
        build.id, event=LOCAL_GATE_UNAVAILABLE, comment_id=comment_id, head=HEAD, reason="exit 75"
    )
    queue.finish(build.id, owner="claude", handoff_comment_id=comment_id)
    return build.id


def _gated(comment_id: str, base: str = FEATURE) -> TrackedTicket:
    from tests.test_dispatcher import READY, comment, ticket

    snapshot = ticket(
        comment(comment_id, READY.format(head=HEAD), author="claude-login", created_at=200),
        head=HEAD,
        ci={HEAD: CIResult("not-run", reported=False)},
    )
    factory = replace(
        snapshot.factory,
        base_branch=base,
        ci_branches=("main",),
        ci_local_gate=CheckCommand("make gate", 60),
    )
    return replace(snapshot, factory=factory, pr_base=base)


def _actions(queue: SQLiteJobQueue) -> list[str]:
    with queue.connect() as connection:
        return [
            row[0]
            for row in connection.execute(
                "SELECT action FROM jobs WHERE action != 'build' ORDER BY rowid"
            )
        ]


def _wait(queue: SQLiteJobQueue) -> tuple[str, str, list[str]] | None:
    with queue.connect() as connection:
        row = connection.execute("SELECT comment_id, reason, checks FROM ci_waits").fetchone()
    return None if row is None else (row[0], row[1], json.loads(row[2]))


@pytest.mark.job_bound
def test_t2_holds_an_unavailable_gate_as_gate_unavailable(tmp_path: Path) -> None:
    from tests.test_dispatcher import setup

    dispatcher, queue = setup(tmp_path)
    build = _build(queue, "ready-1")

    dispatcher.process(_gated("ready-1"))

    assert _actions(queue) == []
    assert _wait(queue) == ("ready-1", "local-gate-waiting", ["gate unavailable"])
    assert LOCAL_GATE_PASSED not in [event for event, _ in _gate(tmp_path / "d.db", build)]


@pytest.mark.job_bound
def test_t2_queues_review_once_a_retry_passes(tmp_path: Path) -> None:
    from tests.test_dispatcher import setup

    dispatcher, queue = setup(tmp_path)
    build = _build(queue, "ready-1")
    dispatcher.process(_gated("ready-1"))
    queue.record_head_check(build, event=LOCAL_GATE_PASSED, comment_id="ready-1", head=HEAD)

    dispatcher.process(_gated("ready-1"))

    assert _actions(queue) == ["review"]
    assert _wait(queue) is None


@pytest.mark.job_bound
def test_a_pass_is_not_undone_by_a_later_unavailable(tmp_path: Path) -> None:
    from tests.test_dispatcher import setup

    dispatcher, queue = setup(tmp_path)
    build = _build(queue, "ready-1")
    queue.record_head_check(build, event=LOCAL_GATE_PASSED, comment_id="ready-1", head=HEAD)
    queue.record_head_check(
        build, event=LOCAL_GATE_UNAVAILABLE, comment_id="ready-1", head=HEAD, reason="exit 75"
    )

    dispatcher.process(_gated("ready-1"))

    assert _actions(queue) == ["review"]


@pytest.mark.job_bound
def test_t2_queues_a_ci_fix_once_a_retry_fails(tmp_path: Path) -> None:
    from tests.test_dispatcher import setup

    dispatcher, queue = setup(tmp_path)
    build = _build(queue, "ready-1")
    dispatcher.process(_gated("ready-1"))
    queue.record_head_check(
        build, event=LOCAL_GATE_FAILED, comment_id="ready-1", head=HEAD,
        reason="local_gate failed: exit 1",
    )

    dispatcher.process(_gated("ready-1"))

    assert _actions(queue) == ["fix"]
    assert _wait(queue) is None
    detail = next(job.request.detail for job in queue.outstanding() if job.request.action == "fix")
    assert "local_gate failed: exit 1" in detail


@pytest.mark.job_bound
def test_a_pr_retargeted_onto_a_ci_branch_stops_waiting(tmp_path: Path) -> None:
    from tests.test_dispatcher import setup

    dispatcher, queue = setup(tmp_path)
    _build(queue, "ready-1")
    dispatcher.process(_gated("ready-1"))

    dispatcher.process(_gated("ready-1", base="main"))

    assert _actions(queue) == ["review"]


def _waiting(db: Path, comment_id: str, head: str) -> None:
    with sqlite3.connect(db) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ci_waits (
                repo TEXT NOT NULL, ticket_id TEXT NOT NULL, comment_id TEXT NOT NULL,
                head TEXT NOT NULL, reason TEXT NOT NULL, checks TEXT NOT NULL,
                since REAL NOT NULL, next_round INTEGER, PRIMARY KEY (repo, ticket_id)
            )
            """
        )
        connection.execute(
            "INSERT INTO ci_waits VALUES ('owner/repo', '42', ?, ?, 'local-gate-waiting', "
            "'[\"gate unavailable\"]', 0, 1)",
            (comment_id, head),
        )


def _age_events(db: Path, seconds: float) -> None:
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE job_events SET recorded_at = strftime('%s','now') - ?", (seconds,)
        )


def _unavailable_then(tmp_path: Path, origin: str, retry: str):
    waiter, db, job_id, _log = remote_waiter(tmp_path, origin, _factory_text("exit 75"), FEATURE)
    _waiting(db, COMMENT, origin)
    _serve_factory_md(env_store(), _factory_text(retry))
    return waiter, db, job_id


def test_the_waiter_retries_no_sooner_than_every_600_seconds(tmp_path: Path, origin: str) -> None:
    marker = tmp_path / "retried"
    waiter, db, job_id = _unavailable_then(tmp_path, origin, f"touch {marker}")
    assert LOCAL_GATE_RETRY_SECONDS == 600

    _age_events(db, 590)
    waiter.step()
    assert not marker.exists()

    _age_events(db, 601)
    waiter.step()
    assert marker.exists()
    assert _gate(db, job_id)[-1] == (LOCAL_GATE_PASSED, f"handoff {COMMENT}: at {origin}")

    marker.unlink()
    _age_events(db, 5000)
    waiter.step()
    assert not marker.exists()


def test_a_retry_that_fails_records_the_failure(tmp_path: Path, origin: str) -> None:
    waiter, db, job_id = _unavailable_then(tmp_path, origin, "exit 1")

    _age_events(db, 601)
    waiter.step()

    assert _gate(db, job_id)[-1] == (
        LOCAL_GATE_FAILED, f"handoff {COMMENT}: at {origin}: local_gate failed: exit 1"
    )
    assert _state(db, job_id) == ("done", COMMENT)


def test_a_retry_still_unavailable_waits_another_600_seconds(tmp_path: Path, origin: str) -> None:
    waiter, db, job_id = _unavailable_then(tmp_path, origin, "exit 75")

    _age_events(db, 601)
    waiter.step()
    assert [event for event, _ in _gate(db, job_id)] == [LOCAL_GATE_UNAVAILABLE] * 2

    waiter.step()
    assert [event for event, _ in _gate(db, job_id)] == [LOCAL_GATE_UNAVAILABLE] * 2


def test_the_waiter_retries_nothing_once_the_wait_is_gone(tmp_path: Path, origin: str) -> None:
    marker = tmp_path / "retried"
    waiter, db, _job_id = _unavailable_then(tmp_path, origin, f"touch {marker}")
    with sqlite3.connect(db) as connection:
        connection.execute("DELETE FROM ci_waits")

    _age_events(db, 601)
    waiter.step()

    assert not marker.exists()
