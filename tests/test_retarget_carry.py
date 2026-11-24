"""#1522: an approved stacked child whose own change survived the retarget rebase
re-hands-off with no review."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatch.types import CIResult
from lib.queue import JobRequest
from lib.update_check import RebaseScope, RetargetDelta
from tests.merge_queue_support import sha
from tests.test_dispatcher import VERDICT, comment
from tests.test_lifecycle import outcome
from tests.test_retarget_review import FROM_HEAD, NEW_BASE, NEW_HEAD, Delta, reviews
from tests.test_stack_retarget_transition import land, ready_on, rebases, wired
from tests.test_ticket_base_branch import STACK_REPO, polled_child

UNCHANGED = RetargetDelta("same", "same", frozenset({"lib/a.py"}), frozenset({"lib/b.py"}))
EDITED = RetargetDelta("before", "after", frozenset({"lib/a.py"}), frozenset({"lib/b.py"}))
OVERLAP = RetargetDelta("same", "same", frozenset({"lib/a.py"}), frozenset({"lib/a.py"}))
APPROVAL = {"approved_head": FROM_HEAD, "approved_at": 5.0, "approve_comment_id": "approve-1"}
APPROVE = comment(
    "approve-1",
    VERDICT.format(round="2/3", head=FROM_HEAD, verdict="APPROVE", risk="low"),
    author="maxharris1",
    created_at=10**11,
)
GREEN = CIResult("success")


class Carry:
    def __init__(self, tmp_path: Path, state: str, delta: RetargetDelta | Exception) -> None:
        self.tmp_path = tmp_path
        self.held = state == "review"
        self.queue, self.github, self.dispatcher = wired(tmp_path, state=state)
        with self.queue.connect() as connection:
            if state != "review":
                connection.execute(
                    "UPDATE ticket_state SET approved_head = ?, approved_at = ?, "
                    "approve_comment_id = ?",
                    tuple(APPROVAL.values()),
                )
            connection.commit()
        self.queue.enqueue(
            JobRequest(
                repo=STACK_REPO, ticket_id="501", action="review", recipient="codex",
                source_comment_id="ready-1", timeout_seconds=600, head=FROM_HEAD,
                detail=json.dumps({"action": "review", "round": "2/3"}),
            ),
            dedupe_key="review-r2",
        )
        review = self.queue.claim("codex")
        self.queue.finish(review.id, owner="codex", handoff_comment_id="approve-1")
        self.dispatcher.rebase_classifier = lambda repo, base, last, head: RebaseScope(
            last, sha(5), NEW_BASE
        )
        self.dispatcher.retarget_delta = Delta(delta)
        land(self.github)
        self.dispatcher.process(self._shipped(polled_child(tmp_path, self.github)))
        (rebase,) = rebases(self.queue)
        assert rebase["rebase"]["from_state"] == state
        fix = self.queue.claim("claude")
        self.queue.finish(fix.id, owner="claude", handoff_comment_id="ready-9")

    def _shipped(self, ticket):
        comments = (APPROVE, *ticket.comments) if self.held else ticket.comments
        return replace(
            ticket,
            comments=comments,
            factory=replace(ticket.factory, ship="handoff"),
        )

    def ready(self, ci: CIResult = GREEN) -> None:
        ticket = self._shipped(ready_on(self.tmp_path, self.github, NEW_HEAD))
        self.dispatcher.process(replace(ticket, ci={NEW_HEAD: ci}))

    def state(self) -> sqlite3.Row:
        with self.queue.connect() as connection:
            return connection.execute("SELECT * FROM ticket_state").fetchone()

    def carries(self) -> list[tuple[str, str]]:
        with self.queue.connect() as connection:
            return [
                tuple(row)
                for row in connection.execute("SELECT from_head, to_head FROM approval_carries")
            ]

    def source(self) -> str:
        with self.queue.connect() as connection:
            return connection.execute(
                "SELECT source FROM ticket_transitions ORDER BY rowid DESC LIMIT 1"
            ).fetchone()["source"]

    def ticket(self):
        return replace(ready_on(self.tmp_path, self.github, NEW_HEAD), ci={NEW_HEAD: GREEN})


@pytest.mark.parametrize("state", ["merge-queue", "handed-off"])
def test_an_unchanged_child_goes_back_to_its_approved_state_with_no_review(
    tmp_path: Path, state: str
) -> None:
    carry = Carry(tmp_path, state, UNCHANGED)

    carry.ready()
    carry.ready()

    assert outcome(carry.queue, "ready-9") == "retarget-carry"
    assert reviews(carry.queue) == []
    row = carry.state()
    assert row["workflow_state"] == state
    assert {column: row[column] for column in APPROVAL} == APPROVAL
    assert carry.carries() == [(FROM_HEAD, NEW_HEAD)]
    assert carry.source() == "retarget-carry"
    assert carry.dispatcher.review_actions.states[-1] == state
    assert carry.dispatcher._approval_carries(carry.ticket(), FROM_HEAD, NEW_HEAD)
    assert not carry.dispatcher._approval_covers(carry.ticket(), FROM_HEAD, NEW_HEAD)


def test_a_held_approve_hands_off_the_rebased_head_with_no_review(tmp_path: Path) -> None:
    carry = Carry(tmp_path, "review", UNCHANGED)
    assert outcome(carry.queue, "approve-1") is None, "the APPROVE waits for the carry"

    carry.ready()
    carry.ready()

    assert outcome(carry.queue, "ready-9") == "retarget-carry"
    assert outcome(carry.queue, "approve-1") == "handed-off"
    assert carry.dispatcher.review_actions.handoffs == [NEW_HEAD]
    assert reviews(carry.queue) == []
    assert carry.carries() == [(FROM_HEAD, NEW_HEAD)]


@pytest.mark.parametrize(
    "delta", [EDITED, OVERLAP, ValueError("gone")], ids=["edited", "overlap", "unreadable"]
)
def test_a_refused_carry_queues_one_scoped_review_and_settles_the_held_approve(
    tmp_path: Path, delta
) -> None:
    carry = Carry(tmp_path, "review", delta)

    carry.ready()
    carry.ready()

    assert outcome(carry.queue, "ready-9") == "queued-review"
    assert outcome(carry.queue, "approve-1") == "stale-approve"
    (review,) = reviews(carry.queue)
    assert json.loads(review.request.detail)["source"] == "rebase"
    assert carry.carries() == []
    assert carry.dispatcher.review_actions.handoffs == []
    assert len(carry.dispatcher.retarget_delta.calls) == 1


def test_pending_ci_on_a_carry_candidate_waits_with_no_review(tmp_path: Path) -> None:
    carry = Carry(tmp_path, "merge-queue", UNCHANGED)

    carry.ready(CIResult("pending"))
    carry.ready(CIResult("pending"))

    assert outcome(carry.queue, "ready-9") is None
    assert reviews(carry.queue) == []
    assert carry.state()["workflow_state"] == "fixing"
    assert carry.carries() == []

    carry.ready()

    assert outcome(carry.queue, "ready-9") == "retarget-carry"
    assert carry.state()["workflow_state"] == "merge-queue"
    assert reviews(carry.queue) == []


def test_red_ci_on_a_carry_candidate_takes_the_ci_fix_path(tmp_path: Path) -> None:
    carry = Carry(tmp_path, "merge-queue", UNCHANGED)

    carry.ready(CIResult("failure", log="pytest failed"))
    carry.ready(CIResult("failure", log="pytest failed"))

    assert outcome(carry.queue, "ready-9") == "queued-ci-fix"
    assert reviews(carry.queue) == []
    assert carry.carries() == []
    assert carry.state()["workflow_state"] == "fixing"
    assert carry.state()["approved_head"] is None
