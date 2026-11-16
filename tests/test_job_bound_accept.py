from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatch.types import ShipError
from lib.dispatcher import CIResult, Dispatcher, TrackedTicket
from lib.factory_md import HoldConfig
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
                "SELECT detail FROM jobs WHERE recipient = 'grok'"
                " UNION ALL SELECT body FROM skipped_wakes"
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


def _registered_approval(tmp_path: Path):
    dispatcher, queue, actions = _reviewing(tmp_path)
    review = _claim(queue, "codex")
    queue.finish(review.id, owner="codex", handoff_comment_id="approve-1")
    return dispatcher, queue, actions, _verdict_ticket("approve-1", "APPROVE")


def _with_stray_revise(snapshot: TrackedTicket) -> TrackedTicket:
    body = VERDICT.format(round="1/3", head=HEAD, verdict="REVISE", risk="high")
    stray = comment("stray", body, author="codex-login", created_at=400)
    return replace(snapshot, comments=(*snapshot.comments, stray))


def test_stray_revise_does_not_supersede_a_held_registered_approve(tmp_path: Path) -> None:
    dispatcher, queue, actions, snapshot = _registered_approval(tmp_path)
    snapshot = replace(
        snapshot,
        factory=replace(snapshot.factory, hold=HoldConfig()),
        labels=("factory:hold",),
        changed_files=(),
        label_events=(),
    )
    dispatcher.process(snapshot)
    snapshot = _with_stray_revise(snapshot)
    dispatcher.process(snapshot)
    dispatcher.process(replace(snapshot, labels=()))

    assert handled_outcome(queue, "stray") == "rejected-unregistered-verdict"
    assert handled_outcome(queue, "approve-1") == "merged"
    assert actions.merges == [(True, HEAD)]


def test_stray_revise_does_not_spend_a_registered_approves_retry(tmp_path: Path) -> None:
    dispatcher, queue, actions, snapshot = _registered_approval(tmp_path)
    snapshot = replace(snapshot, factory=replace(snapshot.factory, ship="handoff"))
    handoff = actions.handoff
    error = ShipError("temporary failure")
    error.write_confirmed = False

    def fail(*args):
        raise error

    actions.handoff = fail
    dispatcher.process(snapshot)
    actions.handoff = handoff
    dispatcher.process(_with_stray_revise(snapshot))

    with queue.connect() as connection:
        intent = tuple(connection.execute("SELECT status, redrive FROM ship_intents").fetchone())
    assert intent == ("completed", "spent")
    assert actions.handoffs == [HEAD]


def test_review_at_another_head_leaves_a_stray_verdict_unregistered(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    _enqueue(queue, "review", "codex", OLD_HEAD)
    _claim(queue, "codex")
    _enqueue(queue, "build", "claude")
    build = _claim(queue, "claude")
    queue.finish(build.id, owner="claude", handoff_comment_id="ready-1")
    dispatcher.process(_ready("ready-1"))
    snapshot = _verdict_ticket("stray", "APPROVE")
    dispatcher.process(snapshot)
    dispatcher.process(snapshot)

    with queue.connect() as connection:
        reviews = [
            tuple(row)
            for row in connection.execute(
                "SELECT state, head FROM jobs WHERE action = 'review' ORDER BY rowid"
            )
        ]
    assert reviews == [("running", OLD_HEAD), ("queued", HEAD)]
    assert handled_outcome(queue, "stray") == "rejected-unregistered-verdict"
    assert len(_wakes(queue)) == 1


def test_stray_revise_does_not_stale_a_registered_fix_ready(tmp_path: Path) -> None:
    dispatcher, queue, _, _ = _registered_approval(tmp_path)
    snapshot = _verdict_ticket("approve-1", "REVISE")
    _enqueue(queue, "fix", "claude", HEAD)
    fix = _claim(queue, "claude")
    queue.finish(fix.id, owner="claude", handoff_comment_id="ready-2")
    ready = comment("ready-2", READY.format(head=HEAD), author="claude-login", created_at=350)
    body = VERDICT.format(round="2/3", head=HEAD, verdict="REVISE", risk="high")
    stray = comment("stray", body, author="codex-login", created_at=400)
    dispatcher.process(replace(snapshot, comments=(*snapshot.comments, ready, stray)))

    assert handled_outcome(queue, "stray") == "rejected-unregistered-verdict"
    assert handled_outcome(queue, "ready-2") == "queued-review"


def _with_round(snapshot: TrackedTicket, round_field: str) -> TrackedTicket:
    last = snapshot.comments[-1]
    stray = replace(last, body=last.body.replace("round=1/3", f"round={round_field}"))
    return replace(snapshot, comments=(*snapshot.comments[:-1], stray))


def test_running_review_leaves_a_verdict_for_another_round_cap_unregistered(
    tmp_path: Path,
) -> None:
    dispatcher, queue, actions = _reviewing(tmp_path)
    _claim(queue, "codex")
    snapshot = _with_round(_verdict_ticket("stray", "APPROVE"), "1/9")

    dispatcher.process(snapshot)
    dispatcher.process(snapshot)

    assert handled_outcome(queue, "stray") == "rejected-unregistered-verdict"
    assert actions.merges == []
    assert len([detail for detail in _wakes(queue) if "stray" in detail]) == 1


def test_failed_review_does_not_take_a_verdict_for_another_round_cap(tmp_path: Path) -> None:
    dispatcher, queue, actions = _reviewing(tmp_path)
    review = _claim(queue, "codex")
    queue.finish(review.id, owner="codex", handoff_comment_id=None)
    snapshot = _with_round(_verdict_ticket("stray", "APPROVE"), "1/9")

    dispatcher.process(snapshot)
    dispatcher.reconcile_jobs()
    dispatcher.process(snapshot)

    with queue.connect() as connection:
        assert connection.execute(
            "SELECT handoff_comment_id FROM jobs WHERE id = ?", (review.id,)
        ).fetchone()[0] is None
    assert handled_outcome(queue, "stray") == "rejected-unregistered-verdict"
    assert actions.merges == []


def _ci_fixes_after(tmp_path: Path, stray: bool) -> list[tuple[str, str]]:
    dispatcher, queue = setup(tmp_path)

    def snapshot(comments: list, status: str) -> TrackedTicket:
        value = ticket(*comments, head=HEAD, ci={HEAD: CIResult(status)})
        return replace(value, factory=replace(value.factory, start_before_ci=True))

    def revise(comment_id: str, created_at: float):
        body = VERDICT.format(round="1/3", head=HEAD, verdict="REVISE", risk="high")
        return comment(comment_id, body, author="codex-login", created_at=created_at)

    _enqueue(queue, "build", "claude")
    queue.finish(_claim(queue, "claude").id, owner="claude", handoff_comment_id="ready-1")
    comments = [comment("ready-1", READY.format(head=HEAD), author="claude-login", created_at=200)]
    dispatcher.process(snapshot(comments, "pending"))
    queue.finish(_claim(queue, "codex").id, owner="codex", handoff_comment_id="verdict-real")
    if stray:
        comments.append(revise("stray", 300))
        dispatcher.process(snapshot(comments, "pending"))
        assert handled_outcome(queue, "stray") == "rejected-unregistered-verdict"
    queue.enqueue(
        JobRequest(
            repo="owner/repo",
            ticket_id="42",
            action="build",
            recipient="claude",
            source_comment_id="ready-2-source",
            timeout_seconds=5400,
        ),
        dedupe_key="build-2",
    )
    queue.finish(_claim(queue, "claude").id, owner="claude", handoff_comment_id="ready-2")
    comments.append(comment("ready-2", READY.format(head=HEAD), author="claude-login", created_at=400))
    dispatcher.process(snapshot(comments, "failure"))
    comments.append(revise("verdict-real", 250))
    dispatcher.process(snapshot(comments, "failure"))
    with queue.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT state, source_comment_id FROM jobs WHERE action = 'fix' ORDER BY rowid"
            )
        ]


def test_rejected_stray_verdict_does_not_change_ci_fix_routing(tmp_path: Path) -> None:
    control = _ci_fixes_after(tmp_path / "control", stray=False)

    assert control == [("queued", "verdict-real")]
    assert _ci_fixes_after(tmp_path / "stray", stray=True) == control
