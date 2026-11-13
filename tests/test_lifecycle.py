"""#108: a ticket's lifecycle ends cleanly.

merged is absorbing and clears everything that keeps a ticket on a person's
screen; a handed-off ticket is still watched and may re-enter review inside
the #105 cap; and the thread's own orch:v1 markers stop a replayed comment
from queueing work that is already done. Every test drives the real
Dispatcher over a temp database with test_dispatcher's fakes.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from lib.dispatcher import CIResult, Comment, TrackedTicket
from lib.queue import Job, JobRequest
from tests.test_dispatcher import (
    GO,
    READY,
    VERDICT,
    FakeGrokDelivery,
    FakeReviewActions,
    comment,
    factory,
    setup,
    ticket,
    track,
)

HEADS = [f"{n:x}" * 40 for n in range(1, 10)]
APPROVE = (
    "<!-- orch:v1 kind=verdict from=codex-bot role=reviewer "
    "round={round} head={head} verdict=APPROVE risk=high -->\nApproved.\n"
)
REPO, TICKET = "owner/repo", "42"


class Flow:
    """One ticket's thread on a ship: handoff repo, processed after every post."""

    def __init__(self, tmp_path: Path, *, ship: str = "handoff", rounds_high: int = 3):
        self.now = [100.0]
        self.actions = FakeReviewActions()
        self.grok = FakeGrokDelivery()
        self.dispatcher, self.queue = setup(
            tmp_path, self.now, review_actions=self.actions, grok_delivery=self.grok
        )
        track(self.queue)
        self.comments: list[Comment] = []
        self.head = HEADS[0]
        self.ci = "success"
        self.factory = factory(ship=ship, rounds_high=rounds_high)

    def snapshot(self) -> TrackedTicket:
        return ticket(
            *self.comments,
            head=self.head,
            ci={self.head: CIResult(self.ci)},
            factory_config=self.factory,
        )

    def add(self, comment_id: str, body: str, author: str) -> None:
        self.now[0] += 10
        self.comments.append(comment(comment_id, body, author=author, created_at=self.now[0]))

    def post(self, comment_id: str, body: str, author: str) -> str | None:
        self.add(comment_id, body, author)
        self.dispatcher.process(self.snapshot())
        return outcome(self.queue, comment_id)

    def go(self, comment_id: str = "go-1", author: str = "grok-login") -> str | None:
        return self.post(comment_id, GO.format(state="building"), author)

    def ready(self, head: str, author: str = "claude-login") -> str | None:
        self.head = head
        return self.post(f"ready-{head[:6]}", READY.format(head=head), author)

    def approve(self, round_text: str) -> str | None:
        return self.post(
            f"approve-{round_text}",
            APPROVE.format(round=round_text, head=self.head),
            "codex-login",
        )

    def revise(self, round_text: str) -> str | None:
        return self.post(
            f"revise-{round_text}",
            VERDICT.format(round=round_text, head=self.head, verdict="REVISE", risk="high"),
            "codex-login",
        )

    def finish(self, recipient: str) -> Job:
        job = self.queue.claim(recipient)
        assert job is not None
        self.queue.finish(job.id, owner=recipient, handoff_comment_id=f"handoff-{job.id}")
        return job

    def state(self) -> sqlite3.Row:
        return state_row(self.queue)

    def hand_off_at(self, rounds: int) -> None:
        """Build, REVISE until the last round, then APPROVE: handed off."""
        assert self.go() == "queued-build"
        self.finish("claude")
        for number in range(1, rounds + 1):
            assert self.ready(HEADS[number]) == "queued-review"
            self.finish("codex")
            if number < rounds:
                assert self.revise(f"{number}/3") == "queued-review-fix"
                self.finish("claude")
        assert self.approve(f"{rounds}/3") == "handed-off"
        assert self.state()["workflow_state"] == "handed-off"


def outcome(queue, comment_id: str) -> str | None:
    with queue.connect() as connection:
        row = connection.execute(
            "SELECT outcome FROM handled_comments WHERE comment_id = ?", (comment_id,)
        ).fetchone()
    return None if row is None else str(row["outcome"])


def state_row(queue, ticket_id: str = TICKET) -> sqlite3.Row:
    with queue.connect() as connection:
        row = connection.execute(
            "SELECT * FROM ticket_state WHERE repo = ? AND ticket_id = ?",
            (REPO, ticket_id),
        ).fetchone()
    assert row is not None
    return row


def attention(queue, ticket_id: str = TICKET) -> list[str]:
    with queue.connect() as connection:
        return [
            str(row["reason"])
            for row in connection.execute(
                "SELECT reason FROM needs_attention WHERE ticket_id = ? ORDER BY reason",
                (ticket_id,),
            )
        ]


def jobs(queue, action: str | None = None) -> list[sqlite3.Row]:
    with queue.connect() as connection:
        return connection.execute(
            "SELECT * FROM jobs WHERE (? IS NULL OR action = ?) ORDER BY created_at, rowid",
            (action, action),
        ).fetchall()


def wakes(queue, reason: str) -> int:
    return len(jobs(queue, reason))


def round_of(job: Job | sqlite3.Row) -> str | None:
    detail = job.request.detail if isinstance(job, Job) else job["detail"]
    return json.loads(detail or "{}").get("round")


# --- Unit 2: one merged transition (AC 1) -------------------------------------------


def test_merge_clears_stuck_paused_and_attention(tmp_path: Path) -> None:
    """#87, #88 and #98: merged by hand while stuck, paused or flagged."""
    dispatcher, queue = setup(tmp_path)
    with queue.connect() as connection:
        connection.executemany(
            """
            INSERT INTO ticket_state (
                repo, ticket_id, gate_count, stuck, stuck_reason,
                stuck_source_comment_id, workflow_state, paused_action,
                paused_recipient, paused_head, paused_generation, paused_question_id
            ) VALUES (?, ?, 3, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (REPO, "87", 1, "ci-red-3", "ready-9", "review",
                 None, None, None, None, None),
                (REPO, "88", 0, None, None, "waiting-max",
                 "fix", "claude", HEADS[0], 1, "questions-1"),
                (REPO, "98", 0, None, None, "handed-off",
                 None, None, None, None, None),
            ],
        )
    dispatcher.flag_attention(REPO, "98", "ship-failed", "handoff ship failed")
    dispatcher.flag_attention(REPO, "88", "dedupe-dropped", "dropped")

    for ticket_id in ("87", "88", "98"):
        dispatcher.record_terminal(REPO, ticket_id, "merged")

    for ticket_id in ("87", "88", "98"):
        row = state_row(queue, ticket_id)
        assert (row["workflow_state"], row["stuck"], row["stuck_reason"]) == ("merged", 0, None)
        assert row["stuck_source_comment_id"] is None
        assert [row[f"paused_{name}"] for name in (
            "action", "recipient", "head", "generation", "question_id"
        )] == [None] * 5
        assert attention(queue, ticket_id) == []


def test_merge_cancels_queued_harness_jobs_only(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)

    def enqueue(action: str, recipient: str) -> Job:
        return queue.enqueue(
            JobRequest(
                repo=REPO,
                ticket_id=TICKET,
                action=action,
                recipient=recipient,
                source_comment_id=f"c-{action}",
                timeout_seconds=3600,
            ),
            dedupe_key=f"k-{action}",
        )

    fix = enqueue("fix", "claude")
    assert queue.claim("claude") is not None
    review = enqueue("review", "codex")
    done_wake = enqueue("done", "grok")

    dispatcher.record_terminal(REPO, TICKET, "merged")

    states = {row["id"]: row["state"] for row in jobs(queue)}
    assert states == {fix.id: "running", review.id: "cancelled", done_wake.id: "queued"}
    with queue.connect() as connection:
        detail = connection.execute(
            "SELECT detail FROM job_events WHERE job_id = ? AND event = 'cancelled'",
            (review.id,),
        ).fetchone()[0]
    assert detail == "merged:observed"


def test_merged_is_absorbing(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.hand_off_at(1)
    flow.dispatcher.record_terminal(REPO, TICKET, "merged")
    with flow.queue.connect() as connection:
        connection.execute("DELETE FROM handled_comments WHERE comment_id = 'approve-1/3'")
    flow.actions.states.clear()

    flow.dispatcher.process(flow.snapshot())

    assert outcome(flow.queue, "approve-1/3") == "ship-already-completed-handoff"
    assert flow.state()["workflow_state"] == "merged"
    assert flow.actions.states == ["merged"]


def test_a_comment_on_a_merged_ticket_does_nothing(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.hand_off_at(1)
    flow.dispatcher.record_terminal(REPO, TICKET, "merged")
    before = len(jobs(flow.queue))

    assert flow.go("go-late") == "after-merge"
    assert flow.ready(HEADS[5]) == "after-merge"
    assert flow.revise("2/3") == "after-merge"

    assert len(jobs(flow.queue)) == before
    assert flow.state()["workflow_state"] == "merged"


def test_a_merge_ship_enters_merged_through_the_one_transition(tmp_path: Path) -> None:
    flow = Flow(tmp_path, ship="merge")
    assert flow.go() == "queued-build"
    flow.finish("claude")
    assert flow.ready(HEADS[1]) == "queued-review"
    flow.finish("codex")
    flow.dispatcher.flag_attention(REPO, TICKET, "dedupe-dropped", "stale flag")

    assert flow.approve("1/3") == "merged"

    assert flow.state()["workflow_state"] == "merged"
    assert attention(flow.queue) == []
