"""#503: T6 never settles a failed builder job with a ready its pre_ready refused.

A refused ready is recorded as a `pre-ready-failed` job event whose detail
starts `handoff <comment-id>:`. Without the skip, the job's failure would let
T6 mark it done from that ready and T2 would register it for review. Real
Dispatcher over a temp database, with test_dispatcher's fakes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.dispatcher import CIResult, TrackedTicket
from lib.queue import PRE_READY_FAILED, Job, JobRequest, SQLiteJobQueue
from tests.test_dispatcher import READY, comment, handled_outcome, setup, ticket

pytestmark = pytest.mark.job_bound

HEAD = "a" * 40


def _failed_build(queue: SQLiteJobQueue, *, refused: str | None) -> Job:
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
    queue.finish(build.id, owner="claude", handoff_comment_id=None)
    return build


def _ready(comment_id: str) -> TrackedTicket:
    return ticket(
        comment(comment_id, READY.format(head=HEAD), author="claude-login", created_at=200),
        head=HEAD,
        ci={HEAD: CIResult("success")},
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
