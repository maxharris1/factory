from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatcher import CIResult, TrackedTicket
from lib.factory_md import CheckCommand
from lib.queue import PRE_READY_FAILED, PRE_READY_PASSED, Job, JobRequest, SQLiteJobQueue
from tests.test_dispatcher import READY, comment, handled_outcome, setup, ticket

pytestmark = pytest.mark.job_bound

HEAD = "a" * 40


def _failed_build(
    queue: SQLiteJobQueue, *, refused: str | None, passed: str | None = None
) -> Job:
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
    if refused is not None:
        queue.record_invalid_handoff(
            build.id,
            owner="claude",
            comment_id=refused,
            detail="pre_ready failed: exit 1",
            event=PRE_READY_FAILED,
        )
    if passed is not None:
        queue.record_head_check(build.id, event=PRE_READY_PASSED, comment_id=passed, head=HEAD)
    queue.finish(build.id, owner="claude", handoff_comment_id=None)
    return build


def _ready(comment_id: str) -> TrackedTicket:
    return ticket(
        comment(comment_id, READY.format(head=HEAD), author="claude-login", created_at=200),
        head=HEAD,
        ci={HEAD: CIResult("success")},
    )


def _with_pre_ready(snapshot: TrackedTicket) -> TrackedTicket:
    return replace(
        snapshot, factory=replace(snapshot.factory, pre_ready=CheckCommand("make check", 60))
    )


def _state(queue: SQLiteJobQueue, job_id: str) -> tuple[str, str | None]:
    with queue.connect() as connection:
        row = connection.execute(
            "SELECT state, handoff_comment_id FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    return str(row["state"]), row["handoff_comment_id"]


def _reviews(queue: SQLiteJobQueue) -> int:
    with queue.connect() as connection:
        return connection.execute("SELECT count(*) FROM jobs WHERE action = 'review'").fetchone()[0]


def _settle(dispatcher, snapshot: TrackedTicket) -> None:
    dispatcher.process(snapshot)
    dispatcher.reconcile_jobs()
    dispatcher.process(snapshot)


def test_a_failed_build_whose_ready_pre_ready_refused_is_not_done_from_it(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    build = _failed_build(queue, refused="ready-1")
    snapshot = _ready("ready-1")

    _settle(dispatcher, snapshot)

    assert dispatcher._handoff_candidate(build, snapshot) is None
    assert _state(queue, build.id)[1] is None
    assert handled_outcome(queue, "ready-1") == "rejected-unregistered-ready"
    assert _reviews(queue) == 0


def test_a_failed_build_with_the_same_ready_unrefused_is_done_from_it(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    build = _failed_build(queue, refused=None)
    snapshot = _ready("ready-1")

    _settle(dispatcher, snapshot)

    assert _state(queue, build.id) == ("done", "ready-1")
    assert handled_outcome(queue, "ready-1") == "queued-review"
    assert _reviews(queue) == 1


def test_a_refusal_of_another_ready_does_not_hide_this_one(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    build = _failed_build(queue, refused="ready-10")
    snapshot = _ready("ready-1")

    _settle(dispatcher, snapshot)

    assert _state(queue, build.id) == ("done", "ready-1")
    assert handled_outcome(queue, "ready-1") == "queued-review"


def test_with_pre_ready_set_a_ready_never_checked_does_not_settle(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    build = _failed_build(queue, refused=None)
    snapshot = _with_pre_ready(_ready("ready-1"))

    _settle(dispatcher, snapshot)

    assert _state(queue, build.id)[1] is None
    assert _reviews(queue) == 0


def test_with_pre_ready_set_a_ready_that_passed_settles(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    build = _failed_build(queue, refused=None, passed="ready-1")
    snapshot = _with_pre_ready(_ready("ready-1"))

    _settle(dispatcher, snapshot)

    assert _state(queue, build.id) == ("done", "ready-1")
    assert handled_outcome(queue, "ready-1") == "queued-review"
    assert _reviews(queue) == 1


def test_with_pre_ready_set_a_pass_for_another_ready_does_not_settle_this_one(
    tmp_path: Path,
) -> None:
    dispatcher, queue = setup(tmp_path)
    build = _failed_build(queue, refused=None, passed="ready-10")
    snapshot = _with_pre_ready(_ready("ready-1"))

    _settle(dispatcher, snapshot)

    assert _state(queue, build.id)[1] is None
    assert _reviews(queue) == 0
