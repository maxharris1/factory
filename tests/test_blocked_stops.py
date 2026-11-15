"""#188 (T16b): a reviewer's BLOCKED stops the ticket.

BLOCKED is checked like REVISE and APPROVE, stores state blocked, and wakes
Factory once with the verdict link. Only an approver's go leaves blocked,
and it queues a fresh review at the same round. Every test drives the real
Dispatcher over a temp database with test_dispatcher's fakes.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatcher import Dispatcher
from lib.hook_t7 import T7Hook
from lib.orch_yml import Approvals
from tests.test_approver_credential import APP_BOT, MAX, AppFlow
from tests.test_dispatcher import (
    ANSWERS,
    READY,
    VERDICT,
    FakeGrokDelivery,
    FakeReviewActions,
    comment,
    handled_outcome,
    job_count,
)
from tests.test_job_bound_accept import _claim, _reviewing, _verdict_ticket
from tests.test_rounds_and_caps import APPROVE, HEADS, Flow, round_of


def with_t7_hook(dispatcher: Dispatcher) -> Dispatcher:
    return Dispatcher(
        dispatcher.state_path,
        dispatcher.queue,
        trusted_logins=dispatcher.trusted_logins,
        review_actions=FakeReviewActions(),
        grok_delivery=FakeGrokDelivery(),
        approvals=dispatcher.approvals,
        hooks=(T7Hook(),),
    )


def link(comment_id: str) -> str:
    return f"https://github.com/owner/repo/pull/42#issuecomment-{comment_id}"


class Blocked(AppFlow):
    """AppFlow with the production T7 hook, whose comments carry URLs."""

    def __init__(self, tmp_path: Path, *, enabled: bool | None):
        super().__init__(tmp_path, enabled=enabled)
        self.dispatcher = with_t7_hook(self.dispatcher)

    def post(self, comment_id: str, body: str, author: str) -> str:
        self.now[0] += 10
        self.comments.append(
            comment(
                comment_id,
                body,
                author=author,
                created_at=self.now[0],
                url=link(comment_id),
            )
        )
        self.dispatcher.process(self.snapshot())
        return self.outcome(comment_id)

    def outcome(self, comment_id: str) -> str | None:
        with self.queue.connect() as connection:
            row = connection.execute(
                "SELECT outcome FROM handled_comments WHERE comment_id = ?",
                (comment_id,),
            ).fetchone()
        return None if row is None else str(row["outcome"])

    def to_review(self) -> None:
        assert self.go("go-1") == "queued-build"
        self.finish("claude")
        assert self.ready(HEADS[1]) == "queued-review"
        assert round_of(self.finish("codex")) == "1/3"

    def verdict(
        self,
        comment_id: str,
        verdict: str,
        *,
        round_text: str = "1/3",
        head: str | None = None,
        author: str = "codex-login",
    ) -> str | None:
        return self.post(
            comment_id,
            VERDICT.format(
                round=round_text,
                head=head or self.head,
                verdict=verdict,
                risk="high",
            ),
            author,
        )

    def approve(self, comment_id: str, round_text: str = "1/3") -> str | None:
        return self.post(
            comment_id,
            APPROVE.format(round=round_text, head=self.head),
            "codex-login",
        )

    def wakes(self, action: str | None = None) -> list[sqlite3.Row]:
        with self.queue.connect() as connection:
            return connection.execute(
                """
                SELECT action, source_comment_id, detail FROM jobs
                WHERE recipient = 'grok' AND (? IS NULL OR action = ?)
                ORDER BY created_at, id
                """,
                (action, action),
            ).fetchall()


def blocked_flow(tmp_path: Path, *, enabled: bool | None = True) -> Blocked:
    flow = Blocked(tmp_path, enabled=enabled)
    flow.to_review()
    return flow


def shipped(flow: Flow) -> bool:
    actions = flow.dispatcher.review_actions
    assert isinstance(actions, FakeReviewActions)
    with flow.queue.connect() as connection:
        intents = connection.execute("SELECT COUNT(*) FROM ship_intents").fetchone()[0]
    return bool(intents or actions.handoffs or actions.merges)


# --- AC1, AC4: Codex's C02 probe ---------------------------------------------------


@pytest.mark.parametrize("start_before_ci", [False, True])
def test_c02_blocked_then_approve_at_the_same_head_and_round_does_not_ship(
    tmp_path: Path, start_before_ci: bool
) -> None:
    flow = blocked_flow(tmp_path)
    flow.factory = replace(flow.factory, start_before_ci=start_before_ci)

    assert flow.verdict("blocked-1", "BLOCKED") == "blocked-review"
    assert flow.approve("approve-1") == "verdict-while-blocked"
    flow.dispatcher.process(flow.snapshot())

    assert not shipped(flow)
    assert flow.state()["workflow_state"] == "blocked"
    assert flow.state()["review_round"] == 0


def test_revise_and_ready_after_blocked_change_nothing(tmp_path: Path) -> None:
    flow = blocked_flow(tmp_path)
    assert flow.verdict("blocked-1", "BLOCKED") == "blocked-review"
    fixes, reviews = job_count(flow.queue, "fix"), job_count(flow.queue, "review")

    assert flow.verdict("revise-1", "REVISE") == "verdict-while-blocked"
    assert flow.verdict("blocked-2", "BLOCKED") == "verdict-while-blocked"
    assert flow.ready(HEADS[2]) == "blocked-ready"

    assert (job_count(flow.queue, "fix"), job_count(flow.queue, "review")) == (fixes, reviews)
    assert flow.state()["workflow_state"] == "blocked"


# --- AC2: one wake, with the link --------------------------------------------------


def test_one_blocked_wake_carries_the_verdict_link(tmp_path: Path) -> None:
    flow = blocked_flow(tmp_path)
    assert flow.verdict("blocked-1", "BLOCKED") == "blocked-review"
    flow.approve("approve-1")
    flow.verdict("revise-1", "REVISE")
    flow.dispatcher.process(flow.snapshot())

    with_t7_hook(flow.dispatcher).process(flow.snapshot())

    wakes = flow.wakes()
    assert [(row["action"], row["source_comment_id"]) for row in wakes] == [
        ("blocked", "blocked-1")
    ]
    assert link("blocked-1") in wakes[0]["detail"]


# --- AC3: only an approver's go leaves blocked -------------------------------------


@pytest.mark.parametrize("slash", [False, True])
def test_approver_go_queues_a_fresh_review_that_can_ship(
    tmp_path: Path, slash: bool
) -> None:
    flow = blocked_flow(tmp_path)
    assert flow.verdict("blocked-1", "BLOCKED") == "blocked-review"
    flow.approve("approve-1")
    reviews = job_count(flow.queue, "review")

    resumed = (
        flow.post("go-2", "/go", MAX) if slash else flow.go("go-2", author=MAX)
    )

    assert resumed == "queued-blocked-review"
    assert flow.state()["workflow_state"] == "review"
    assert job_count(flow.queue, "review") == reviews + 1
    review = flow.queue.claim("codex")
    assert review is not None
    assert review.request.source_comment_id == "go-2"
    assert review.request.head == flow.head
    assert round_of(review) == "1/3"
    assert link("blocked-1") in (review.request.detail or "")
    flow.queue.finish(review.id, owner="codex", handoff_comment_id="handoff-r")

    assert flow.approve("approve-2") not in ("verdict-while-blocked", "stale-approve")
    assert shipped(flow)


def test_a_second_blocked_after_the_fresh_review_wakes_again(tmp_path: Path) -> None:
    flow = blocked_flow(tmp_path)
    flow.verdict("blocked-1", "BLOCKED")
    assert flow.go("go-2", author=MAX) == "queued-blocked-review"
    flow.finish("codex")

    assert flow.verdict("blocked-2", "BLOCKED") == "blocked-review"

    assert [row["source_comment_id"] for row in flow.wakes("blocked")] == [
        "blocked-1",
        "blocked-2",
    ]


def test_replayed_go_queues_no_second_review(tmp_path: Path) -> None:
    flow = blocked_flow(tmp_path)
    flow.verdict("blocked-1", "BLOCKED")
    assert flow.go("go-2", author=MAX) == "queued-blocked-review"
    reviews = job_count(flow.queue, "review")

    with flow.queue.connect() as connection:
        connection.execute("DELETE FROM handled_comments WHERE comment_id = 'go-2'")
    flow.dispatcher.process(flow.snapshot())

    assert flow.outcome("go-2") == "queued-blocked-review"
    assert job_count(flow.queue, "review") == reviews


@pytest.mark.parametrize("slash", [False, True])
def test_bot_go_on_a_blocked_ticket_is_refused(tmp_path: Path, slash: bool) -> None:
    flow = blocked_flow(tmp_path)
    flow.verdict("blocked-1", "BLOCKED")
    reviews, builds = job_count(flow.queue, "review"), job_count(flow.queue, "build")

    refused = (
        flow.post("go-2", "/go", APP_BOT) if slash else flow.go("go-2", author=APP_BOT)
    )

    assert refused == "go-not-from-approver"
    assert flow.state()["workflow_state"] == "blocked"
    assert (job_count(flow.queue, "review"), job_count(flow.queue, "build")) == (
        reviews,
        builds,
    )
    assert [row["source_comment_id"] for row in flow.wakes("go-not-from-approver")] == [
        "go-2"
    ]


@pytest.mark.parametrize("enabled", [None, False])
def test_switch_off_go_from_the_single_login_resumes(
    tmp_path: Path, enabled: bool | None
) -> None:
    flow = blocked_flow(tmp_path, enabled=enabled)
    flow.verdict("blocked-1", "BLOCKED")
    assert flow.go("go-2", author=APP_BOT) == "queued-blocked-review"


# --- Validation: checked like REVISE and APPROVE -----------------------------------


def test_blocked_from_a_non_reviewer_is_rejected(tmp_path: Path) -> None:
    flow = blocked_flow(tmp_path)
    assert flow.verdict("blocked-1", "BLOCKED", author="claude-login") == (
        "rejected-blocked-author"
    )
    assert flow.state()["workflow_state"] == "review"
    assert flow.wakes("blocked") == []


@pytest.mark.parametrize(
    ("round_text", "head_index"),
    [("2/3", 1), ("1/3", 3)],
)
def test_blocked_at_the_wrong_round_or_head_is_stale(
    tmp_path: Path, round_text: str, head_index: int
) -> None:
    flow = blocked_flow(tmp_path)
    assert (
        flow.verdict("blocked-1", "BLOCKED", round_text=round_text, head=HEADS[head_index])
        == "stale-blocked"
    )
    assert flow.state()["workflow_state"] == "review"
    assert flow.wakes("blocked") == []
    assert [row["source_comment_id"] for row in flow.wakes("stale")] == ["blocked-1"]



def _blocked_review(tmp_path: Path):
    dispatcher, queue, actions = _reviewing(tmp_path)
    dispatcher.approvals = Approvals(app_trust=True, logins=frozenset({"max-login"}))
    snapshot = _verdict_ticket("blocked-1", "BLOCKED")
    review = _claim(queue, "codex")
    queue.finish(review.id, owner="codex", handoff_comment_id="blocked-1")
    dispatcher.process(snapshot)
    assert handled_outcome(queue, "blocked-1") == "blocked-review"
    return dispatcher, queue, actions, snapshot


def _grok_wakes(queue) -> list[tuple[str, str]]:
    with queue.connect() as connection:
        return [
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT action, source_comment_id FROM jobs "
                "WHERE recipient = 'grok' ORDER BY rowid"
            )
        ]


@pytest.mark.job_bound
def test_questions_and_answers_on_a_blocked_ticket_do_not_release_it(
    tmp_path: Path,
) -> None:
    dispatcher, queue, actions, snapshot = _blocked_review(tmp_path)
    questions = comment(
        "questions-1",
        "<!-- orch:v1 kind=questions from=codex-bot role=reviewer state=waiting-max -->\n"
        "Can the review continue?",
        author="codex-login",
        created_at=400,
    )
    answers = comment("answers-1", ANSWERS, author="grok-login", created_at=500)
    for item in (questions, answers):
        snapshot = replace(snapshot, comments=(*snapshot.comments, item))
        dispatcher.process(snapshot)
    assert handled_outcome(queue, "questions-1") == "questions-while-blocked"
    assert job_count(queue, action="review") == 1
    approve = replace(_verdict_ticket("approve-2", "APPROVE").comments[-1], created_at=600)
    dispatcher.process(replace(snapshot, comments=(*snapshot.comments, approve)))

    assert handled_outcome(queue, "approve-2") == "verdict-while-blocked"
    assert actions.merges == []
    assert _grok_wakes(queue)[0] == ("blocked", "blocked-1")


@pytest.mark.job_bound
@pytest.mark.parametrize("verdict", ["APPROVE", "REVISE", "BLOCKED"])
def test_an_unregistered_verdict_after_blocked_adds_no_wake(
    tmp_path: Path, verdict: str
) -> None:
    dispatcher, queue, actions, snapshot = _blocked_review(tmp_path)
    late = replace(_verdict_ticket("late-1", verdict).comments[-1], created_at=400)
    dispatcher.process(replace(snapshot, comments=(*snapshot.comments, late)))
    dispatcher.process(replace(snapshot, comments=(*snapshot.comments, late)))

    assert handled_outcome(queue, "late-1") == "verdict-while-blocked"
    assert _grok_wakes(queue) == [("blocked", "blocked-1")]
    assert actions.merges == []
