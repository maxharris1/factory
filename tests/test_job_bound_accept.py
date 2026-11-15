from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatcher import CIResult, Dispatcher, TrackedTicket
from lib.queue import JobRequest, SQLiteJobQueue
from tests.test_dispatcher import (
    FakeReviewActions,
    READY,
    VERDICT,
    comment,
    handled_outcome,
    setup,
    ticket,
)

pytestmark = pytest.mark.job_bound

HEAD = "a" * 40
OLD_HEAD = "b" * 40


def _enqueue(queue: SQLiteJobQueue, action: str, recipient: str, head: str | None = None) -> None:
    queue.enqueue(
        JobRequest(
            repo="owner/repo",
            ticket_id="42",
            action=action,
            recipient=recipient,
            source_comment_id=f"{action}-source",
            timeout_seconds=5400,
            head=head,
            detail='{"round": "1/3"}' if action == "review" else "{}",
        ),
        dedupe_key=f"{action}-seed",
    )


def _claim(queue: SQLiteJobQueue, recipient: str):
    job = queue.claim(recipient)
    assert job is not None
    return job


def _jobs(queue: SQLiteJobQueue, action: str) -> int:
    with queue.connect() as connection:
        return connection.execute(
            "SELECT count(*) FROM jobs WHERE action = ?", (action,)
        ).fetchone()[0]


def _wakes(queue: SQLiteJobQueue) -> list[str]:
    with queue.connect() as connection:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT detail FROM jobs WHERE recipient = 'grok' ORDER BY rowid"
            )
        ]


def _ready(comment_id: str, head: str = HEAD, created_at: float = 200) -> TrackedTicket:
    return ticket(
        comment(comment_id, READY.format(head=head), author="claude-login", created_at=created_at),
        head=HEAD,
        ci={HEAD: CIResult("success")},
    )


def _reviewing(tmp_path: Path) -> tuple[Dispatcher, SQLiteJobQueue, FakeReviewActions]:
    actions = FakeReviewActions()
    dispatcher, queue = setup(tmp_path, review_actions=actions)
    _enqueue(queue, "build", "claude")
    build = _claim(queue, "claude")
    queue.finish(build.id, owner="claude", handoff_comment_id="ready-1")
    dispatcher.process(_ready("ready-1"))
    assert _jobs(queue, "review") == 1
    return dispatcher, queue, actions


def _verdict_ticket(comment_id: str, verdict: str) -> TrackedTicket:
    body = VERDICT.format(round="1/3", head=HEAD, verdict=verdict, risk="high")
    return ticket(
        comment("ready-1", READY.format(head=HEAD), author="claude-login", created_at=200),
        comment(comment_id, body, author="codex-login", created_at=300),
        head=HEAD,
        ci={HEAD: CIResult("success")},
    )


def test_unregistered_verdict_is_ignored_with_one_wake(tmp_path: Path) -> None:
    for verdict in ("APPROVE", "REVISE"):
        dispatcher, queue, actions = _reviewing(tmp_path / verdict)
        review = _claim(queue, "codex")
        queue.finish(review.id, owner="codex", handoff_comment_id="verdict-real")
        snapshot = _verdict_ticket("verdict-stray", verdict)

        dispatcher.process(snapshot)
        dispatcher.reconcile_jobs()
        dispatcher.process(snapshot)
        dispatcher.reconcile_jobs()

        assert handled_outcome(queue, "verdict-stray") == "rejected-unregistered-verdict"
        assert actions.merges == []
        assert actions.handoffs == []
        assert _jobs(queue, "fix") == 0
        wakes = [detail for detail in _wakes(queue) if "verdict-stray" in detail]
        assert len(wakes) == 1, _wakes(queue)
        assert "rejected-unregistered-verdict" in wakes[0]


def test_verdict_waits_for_its_running_job_to_register_it(tmp_path: Path) -> None:
    dispatcher, queue, actions = _reviewing(tmp_path)
    review = _claim(queue, "codex")
    snapshot = _verdict_ticket("verdict-1", "APPROVE")

    dispatcher.process(snapshot)

    with queue.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM handled_comments WHERE comment_id = 'verdict-1'"
        ).fetchone()[0] == 0
    assert not [detail for detail in _wakes(queue) if "verdict-1" in detail]

    queue.finish(review.id, owner="codex", handoff_comment_id="verdict-1")
    dispatcher.process(snapshot)

    assert handled_outcome(queue, "verdict-1") == "merged"
    assert len(actions.merges) == 1


def test_registered_ready_at_the_head_queues_review(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    _enqueue(queue, "build", "claude")
    build = _claim(queue, "claude")
    queue.finish(build.id, owner="claude", handoff_comment_id="ready-1")

    dispatcher.process(_ready("ready-1"))

    assert handled_outcome(queue, "ready-1") == "queued-review"
    assert _jobs(queue, "review") == 1


def test_unregistered_ready_is_ignored_with_one_wake(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    _enqueue(queue, "build", "claude")
    build = _claim(queue, "claude")
    queue.finish(build.id, owner="claude", handoff_comment_id="ready-real")
    snapshot = _ready("ready-stray")

    dispatcher.process(snapshot)
    dispatcher.process(snapshot)

    assert handled_outcome(queue, "ready-stray") == "rejected-unregistered-ready"
    assert _jobs(queue, "review") == 0
    assert len([detail for detail in _wakes(queue) if "ready-stray" in detail]) == 1


def test_ready_posted_before_its_job_failed_is_registered_by_t6(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    _enqueue(queue, "build", "claude")
    build = _claim(queue, "claude")
    queue.finish(build.id, owner="claude", handoff_comment_id=None)
    snapshot = _ready("ready-1")

    dispatcher.process(snapshot)
    assert _jobs(queue, "review") == 0
    dispatcher.reconcile_jobs()
    dispatcher.process(snapshot)

    with queue.connect() as connection:
        assert connection.execute(
            "SELECT handoff_comment_id FROM jobs WHERE id = ?", (build.id,)
        ).fetchone()[0] == "ready-1"
    assert handled_outcome(queue, "ready-1") == "queued-review"
    assert _jobs(queue, "review") == 1


def test_failed_jobs_ready_at_another_head_still_goes_stale(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    _enqueue(queue, "build", "claude")
    build = _claim(queue, "claude")
    queue.finish(build.id, owner="claude", handoff_comment_id=None)
    snapshot = _ready("ready-old", head=OLD_HEAD)

    dispatcher.process(snapshot)
    dispatcher.reconcile_jobs()

    assert handled_outcome(queue, "ready-old") == "stale-ready-head"
    assert _jobs(queue, "review") == 0


def test_ready_starts_review_without_a_builder_job(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    snapshot = _ready("ready-1")
    snapshot = replace(snapshot, ready_starts_review=True)

    dispatcher.process(snapshot)

    assert handled_outcome(queue, "ready-1") == "queued-review"
    assert _jobs(queue, "review") == 1


def test_stray_verdict_after_a_ready_leaves_the_ready_routed(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    _enqueue(queue, "build", "claude")
    build = _claim(queue, "claude")
    queue.finish(build.id, owner="claude", handoff_comment_id="ready-1")

    dispatcher.process(_verdict_ticket("verdict-stray", "APPROVE"))

    assert handled_outcome(queue, "ready-1") == "queued-review"
    assert handled_outcome(queue, "verdict-stray") == "rejected-unregistered-verdict"
