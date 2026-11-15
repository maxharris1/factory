"""#108: a ticket's lifecycle ends cleanly.

merged is absorbing and clears everything that keeps a ticket on a person's
screen; a handed-off ticket is still watched and may re-enter review inside
the #105 cap; and the thread's own orch:v1 markers stop a replayed comment
from queueing work that is already done. Every test drives the real
Dispatcher over a temp database with test_dispatcher's fakes.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from lib.device_monitor import DeviceMonitor
from lib.device_pool import DevicePool
from lib.dispatcher import CIResult, Comment, TrackedTicket
from lib.job_state import REQUEUED_EARLY_EXIT, requeue_unstarted
from lib.queue import Job, JobRequest
from lib.state_backup import backup, restore
from tests.test_dispatcher import (
    ANSWERS,
    GO,
    PLAN,
    QUESTIONS,
    READY,
    VERDICT,
    FakeGrokDelivery,
    FakeReviewActions,
    FakeSource,
    comment,
    factory,
    setup,
    ticket,
    track,
)
from tests.test_job_state import Poller
from tests.test_remote_start import FakeTransport, config_of, machines_file

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

    def finish(self, recipient: str, handoff: str | None = None) -> Job:
        job = self.queue.claim(recipient)
        assert job is not None
        self.queue.finish(
            job.id, owner=recipient, handoff_comment_id=handoff or f"handoff-{job.id}"
        )
        return job

    def state(self) -> sqlite3.Row:
        return state_row(self.queue)

    def hand_off_at(self, rounds: int) -> None:
        """Build, REVISE until the last round, then APPROVE: handed off."""
        # #18: each job's handoff is the comment that answers it, so the
        # handoff check on the ticket read verifies it.
        assert self.go() == "queued-build"
        self.finish("claude", f"ready-{HEADS[1][:6]}")
        for number in range(1, rounds + 1):
            assert self.ready(HEADS[number]) == "queued-review"
            verdict = f"revise-{number}/3" if number < rounds else f"approve-{rounds}/3"
            self.finish("codex", verdict)
            if number < rounds:
                assert self.revise(f"{number}/3") == "queued-review-fix"
                self.finish("claude", f"ready-{HEADS[number + 1][:6]}")
        assert self.approve(f"{rounds}/3") == "handed-off"
        assert self.state()["workflow_state"] == "handed-off"


class _FreshSource(FakeSource):
    """A poll whose cached read predates a push; the fresh read sees it."""

    def __init__(self, cached: TrackedTicket, *, fresh: TrackedTicket):
        super().__init__(cached)
        self.fresh = fresh

    def fresh_ticket(self, ref) -> TrackedTicket:
        del ref
        return self.fresh


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


# --- Unit 3: every tracked ticket's merge is seen; stopped states read slowly ---------


class MergingSource:
    """FakeSource with LivePollSource's merge contract: a merged PR goes to
    the terminal sink and is dropped unless its ref allows a merged read."""

    def __init__(self, dispatcher, *tickets: TrackedTicket):
        self.dispatcher = dispatcher
        self.tickets = {ticket.ticket_id: ticket for ticket in tickets}
        self.merged: set[str] = set()
        self.refs: list[tuple] = []

    def github_candidates(self, label_prefix: str, *, exclude=frozenset()) -> tuple:
        return ()

    def linear_candidates(self, query, *, exclude, reserve) -> tuple:
        return ()

    def tracked_tickets(self, refs) -> tuple[TrackedTicket, ...]:
        self.refs.append(tuple(ref.ticket_id for ref in refs))
        found = []
        for ref in refs:
            if ref.ticket_id not in self.tickets:
                continue
            if ref.ticket_id in self.merged:
                self.dispatcher.record_terminal(ref.repo, ref.ticket_id, "merged")
                if not ref.allow_merged:
                    continue
            found.append(self.tickets[ref.ticket_id])
        return tuple(found)


def test_handed_off_ticket_moves_to_merged_when_merged_by_hand(tmp_path: Path) -> None:
    """#89, #95 and #97: handed off, then merged by hand, stayed handed-off."""
    flow = Flow(tmp_path)
    flow.hand_off_at(1)
    flow.dispatcher.flag_attention(REPO, TICKET, "ready-after-cap", "stale flag")
    source = MergingSource(flow.dispatcher, flow.snapshot())
    source.merged.add(TICKET)

    for _ in range(10):
        flow.dispatcher.poll_once(source)

    assert flow.state()["workflow_state"] == "merged"
    assert attention(flow.queue) == []
    reads = [refs for refs in source.refs if TICKET in refs]
    assert len(reads) == 1
    for _ in range(10):
        flow.dispatcher.poll_once(source)
    assert sum(TICKET in refs for refs in source.refs) == 1  # merged: never read again


def test_a_handed_off_ticket_with_a_replay_is_read_every_cycle(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.hand_off_at(1)
    with flow.queue.connect() as connection:
        connection.execute("DELETE FROM handled_comments WHERE comment_id = 'approve-1/3'")

    refs = flow.dispatcher._tracked_refs(flow.now[0])
    assert [(ref.ticket_id, ref.allow_merged) for ref in refs] == [(TICKET, True)]


# --- Unit 4: the shipper reads a merged PR (#42 N3) ----------------------------------


def test_review_shipper_reads_a_merged_pr(tmp_path: Path) -> None:
    from lib.dispatcher import ReviewShipper, ShipRequest

    dispatcher, queue = setup(tmp_path)
    with queue.connect() as connection:
        connection.execute("INSERT INTO tracked_tickets VALUES (?, ?, 'github', 0)", (REPO, TICKET))
    refs = []

    class NoFreshSource:
        def tracked_tickets(self, wanted):
            refs.extend(wanted)
            return tuple(
                ticket(head=HEADS[0], factory_config=factory(ship="after:max"))
                for ref in wanted
                if ref.allow_merged  # LivePollSource drops a merged PR otherwise
            )

    request = ShipRequest(REPO, TICKET, HEADS[0], "max", "ok-1", "approve-1")
    found = ReviewShipper(dispatcher, NoFreshSource())._ticket(request)

    assert found.ticket_id == TICKET
    assert [ref.allow_merged for ref in refs] == [True]


# --- Unit 5: re-entry after handoff (AC 2) ----------------------------------------


def _reviews(queue) -> list[sqlite3.Row]:
    return jobs(queue, "review")


def test_ready_after_handoff_reenters_review_at_the_next_round(tmp_path: Path) -> None:
    """#110's shape: APPROVE at 2/3, handed off, then a ready at a new head."""
    flow = Flow(tmp_path)
    flow.hand_off_at(2)
    flow.actions.states.clear()

    assert flow.ready(HEADS[5]) == "queued-review"

    review = _reviews(flow.queue)[-1]
    assert (review["head"], review["state"], round_of(review)) == (HEADS[5], "queued", "3/3")
    assert (flow.state()["workflow_state"], flow.state()["review_round"]) == ("review", 2)
    assert flow.actions.states == ["review"]

    flow.finish("codex")
    assert flow.approve("3/3") == "handed-off"
    assert flow.ready(HEADS[6]) == "ready-after-cap"
    assert attention(flow.queue) == ["ready-after-cap"]
    assert len(_reviews(flow.queue)) == 3


def test_ready_after_handoff_past_the_cap_flags_and_queues_nothing(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.hand_off_at(3)
    before = len(jobs(flow.queue))

    assert flow.ready(HEADS[5]) == "ready-after-cap"

    assert len(jobs(flow.queue)) == before
    assert attention(flow.queue) == ["ready-after-cap"]
    assert flow.state()["workflow_state"] == "handed-off"


def test_ready_after_handoff_with_an_extra_round_reenters(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.hand_off_at(3)
    with flow.queue.connect() as connection:
        connection.execute("UPDATE ticket_state SET cap_extra = 1")

    assert flow.ready(HEADS[5]) == "queued-review"

    assert round_of(_reviews(flow.queue)[-1]) == "4/4"


def test_ready_at_the_shipped_head_is_a_no_op(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.hand_off_at(1)
    before = len(jobs(flow.queue))

    body = READY.format(head=HEADS[1])
    assert flow.post("ready-again", body, "claude-login") == "ready-already-shipped"

    assert len(jobs(flow.queue)) == before
    assert flow.state()["workflow_state"] == "handed-off"


def test_untrusted_ready_after_handoff_is_rejected(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.hand_off_at(1)
    before = len(_reviews(flow.queue))

    assert flow.ready(HEADS[5], author="codex-login") == "rejected-ready-author"

    assert len(_reviews(flow.queue)) == before
    assert flow.state()["workflow_state"] == "handed-off"


def test_ready_after_handoff_waits_for_ci_with_no_write(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.hand_off_at(1)
    flow.ci = "pending"

    assert flow.ready(HEADS[5]) is None  # not handled: read again next poll

    assert (flow.state()["workflow_state"], flow.state()["review_round"]) == ("handed-off", 0)
    flow.ci = "success"
    flow.dispatcher.process(flow.snapshot())
    assert outcome(flow.queue, f"ready-{HEADS[5][:6]}") == "queued-review"


def test_red_ci_after_handoff_queues_the_fix_for_the_next_round(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.hand_off_at(1)
    flow.ci = "failure"

    assert flow.ready(HEADS[5]) == "queued-ci-fix"

    fix = jobs(flow.queue, "fix")[-1]
    assert round_of(fix) == "2/3"
    assert (flow.state()["workflow_state"], flow.state()["review_round"]) == ("fixing", 1)


def test_verdict_after_handoff_without_reentry_is_silent(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.hand_off_at(1)
    grok_before = [row["action"] for row in jobs(flow.queue) if row["recipient"] == "grok"]

    assert flow.revise("2/3") == "after-handoff"
    assert flow.approve("2/3") == "after-handoff"

    grok_after = [row["action"] for row in jobs(flow.queue) if row["recipient"] == "grok"]
    assert grok_after == grok_before == ["done"]
    assert flow.state()["workflow_state"] == "handed-off"


def test_go_after_handoff_changes_nothing(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.hand_off_at(1)
    before = len(jobs(flow.queue))
    flow.actions.states.clear()

    assert flow.go("go-2") == "go-after-handoff"

    assert len(jobs(flow.queue)) == before
    assert flow.state()["workflow_state"] == "handed-off"
    assert flow.actions.states == []


# --- Unit 6: the thread's markers are checked before a job is queued (#66) -----------


def _harness_jobs(queue) -> list[tuple[str, str]]:
    return [
        (str(row["action"]), str(row["state"]))
        for row in jobs(queue)
        if row["recipient"] != "grok"
    ]


def test_go_with_a_later_trusted_ready_queues_no_build(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.add("go-1", GO.format(state="building"), "grok-login")
    flow.head = HEADS[1]
    flow.ci = "pending"  # the ready waits; only the go is decided here
    flow.add("ready-1", READY.format(head=HEADS[1]), "claude-login")

    flow.dispatcher.process(flow.snapshot())

    assert outcome(flow.queue, "go-1") == "answered-build"
    assert _harness_jobs(flow.queue) == [("build", "cancelled")]
    assert flow.state()["workflow_state"] == "building"
    with flow.queue.connect() as connection:
        detail = connection.execute(
            "SELECT detail FROM job_events WHERE event = 'cancelled'"
        ).fetchone()[0]
    assert detail == "answered-by:ready-1"


def test_untrusted_later_ready_does_not_suppress_the_build(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.add("go-1", GO.format(state="building"), "grok-login")
    flow.add("ready-forged", READY.format(head=HEADS[1]), "grok-login")

    flow.dispatcher.process(flow.snapshot())

    assert outcome(flow.queue, "go-1") == "queued-build"
    assert _harness_jobs(flow.queue) == [("build", "queued")]


def test_ready_with_a_later_verdict_queues_no_review(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    assert flow.go() == "queued-build"
    flow.finish("claude")
    flow.head = HEADS[1]
    flow.add("ready-1", READY.format(head=HEADS[1]), "claude-login")
    flow.add(
        "verdict-1",
        VERDICT.format(round="1/3", head=HEADS[1], verdict="REVISE", risk="high"),
        "codex-login",
    )

    flow.dispatcher.process(flow.snapshot())

    assert outcome(flow.queue, "ready-1") == "answered-review"
    assert ("review", "cancelled") in _harness_jobs(flow.queue)
    assert outcome(flow.queue, "verdict-1") == "queued-review-fix"
    assert (flow.state()["workflow_state"], flow.state()["review_round"]) == ("fixing", 1)


def test_revise_with_a_later_ready_queues_no_fix(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    assert flow.go() == "queued-build"
    flow.finish("claude")
    assert flow.ready(HEADS[1]) == "queued-review"
    flow.finish("codex")
    flow.add(
        "revise-1",
        VERDICT.format(round="1/3", head=HEADS[1], verdict="REVISE", risk="high"),
        "codex-login",
    )
    flow.add("ready-2", READY.format(head=HEADS[2]), "claude-login")
    cached = flow.snapshot()
    flow.head, flow.ci = HEADS[2], "pending"  # the push the poll's read missed
    source = _FreshSource(cached, fresh=flow.snapshot())

    flow.dispatcher.poll_once(source)

    assert outcome(flow.queue, "revise-1") == "answered-fix"
    assert outcome(flow.queue, "ready-2") is None  # waits for CI at HEADS[2]
    assert _harness_jobs(flow.queue)[-1] == ("fix", "cancelled")
    assert (flow.state()["workflow_state"], flow.state()["review_round"]) == ("fixing", 1)


def test_fresh_db_replay_queues_no_build_for_a_finished_ticket(tmp_path: Path) -> None:
    """AUT-2284 and AUT-2286 after #66: the state dir was lost, and a fresh
    database re-read a finished thread (go, ready, APPROVE, handed off)."""
    flow = Flow(tmp_path)
    flow.add("go-1", GO.format(state="building"), "grok-login")
    flow.head = HEADS[1]
    flow.add("ready-1", READY.format(head=HEADS[1]), "claude-login")
    flow.add("approve-1", APPROVE.format(round="1/3", head=HEADS[1]), "codex-login")

    flow.dispatcher.process(flow.snapshot())

    assert [outcome(flow.queue, c) for c in ("go-1", "ready-1", "approve-1")] == [
        "answered-build",
        "answered-review",
        "handed-off",
    ]
    assert _harness_jobs(flow.queue) == [("build", "cancelled"), ("review", "cancelled")]
    assert flow.actions.handoffs == [HEADS[1]]
    assert flow.state()["workflow_state"] == "handed-off"


def test_a_verdict_for_another_round_does_not_answer_the_review(tmp_path: Path) -> None:
    """A fresh database replays a thread whose head moved: ready H1 is stale,
    so the review queued for ready H2 is round 1. APPROVE 2/3 is not that
    review's answer (T4 calls it stale), so it must not cancel it."""
    flow = Flow(tmp_path)
    flow.add("go-1", GO.format(state="building"), "grok-login")
    flow.add("ready-1", READY.format(head=HEADS[1]), "claude-login")
    flow.add(
        "revise-1",
        VERDICT.format(round="1/3", head=HEADS[1], verdict="REVISE", risk="high"),
        "codex-login",
    )
    flow.add("ready-2", READY.format(head=HEADS[2]), "claude-login")
    flow.add("approve-2", APPROVE.format(round="2/3", head=HEADS[2]), "codex-login")
    flow.head = HEADS[2]

    flow.dispatcher.process(flow.snapshot())
    flow.dispatcher.process(flow.snapshot())

    assert outcome(flow.queue, "ready-2") == "queued-review"
    review = flow.queue.claim("codex")
    assert review is not None
    assert (review.request.head, round_of(review)) == (HEADS[2], "1/3")


@pytest.mark.parametrize(
    ("state", "answer", "action"),
    [
        ("planning", READY.format(head=HEADS[0]), "plan"),
        ("building", PLAN.format(round="1", ac="met"), "build"),
    ],
    ids=["ready-for-plan", "plan-for-build"],
)
def test_an_answer_of_another_kind_does_not_answer_the_step(
    tmp_path: Path, state: str, answer: str, action: str
) -> None:
    """A ready never answers a plan job, nor a plan a build job: each is
    rejected when handled, so the step it would cancel must stay queued."""
    flow = Flow(tmp_path)
    flow.add("go-1", GO.format(state=state), "grok-login")
    flow.add("answer-1", answer, "claude-login")

    flow.dispatcher.process(flow.snapshot())

    assert outcome(flow.queue, "go-1") == f"queued-{action}"
    assert outcome(flow.queue, "answer-1").startswith("rejected-")
    assert _harness_jobs(flow.queue) == [(action, "queued")]


@pytest.mark.parametrize(
    ("state", "answer", "action", "rejected"),
    [
        ("planning", PLAN.format(round="2", ac="met"), "plan", "stale-plan"),
        ("building", READY.format(head=HEADS[1]), "build", "stale-ready-head"),
    ],
    ids=["plan-of-another-round", "ready-at-another-head"],
)
def test_an_answer_rejected_when_handled_leaves_the_step_runnable(
    tmp_path: Path, state: str, answer: str, action: str, rejected: str
) -> None:
    """A fresh database replays a go and a trusted answer of the right kind
    that its own row then rejects. The step it was taken to answer is
    queued again, so the ticket is not left with no runnable work."""
    flow = Flow(tmp_path)
    flow.add("go-1", GO.format(state=state), "grok-login")
    flow.add("answer-1", answer, "claude-login")

    for _ in range(2):
        flow.dispatcher.process(flow.snapshot())

    assert outcome(flow.queue, "answer-1") == rejected
    assert _harness_jobs(flow.queue) == [(action, "queued")]
    job = flow.queue.claim("claude")
    assert job is not None and job.request.action == action


def test_a_rejected_answer_with_a_later_accepted_one_keeps_the_step_cancelled(
    tmp_path: Path,
) -> None:
    """Ready H1 names a head the PR never had; ready H0 is the build's real
    answer. The build stays cancelled throughout: it is never queued again
    between the two readies."""
    flow = Flow(tmp_path)
    flow.add("go-1", GO.format(state="building"), "grok-login")
    flow.add("ready-stale", READY.format(head=HEADS[1]), "claude-login")
    flow.add("ready-1", READY.format(head=HEADS[0]), "claude-login")

    flow.dispatcher.process(flow.snapshot())

    assert outcome(flow.queue, "ready-stale") == "stale-ready-head"
    assert outcome(flow.queue, "ready-1") == "queued-review"
    assert _harness_jobs(flow.queue) == [("build", "cancelled"), ("review", "queued")]
    with flow.queue.connect() as connection:
        events = [
            tuple(row)
            for row in connection.execute(
                "SELECT event, detail FROM job_events WHERE action = 'build'"
            )
        ]
    assert events == [("cancelled", "answered-by:ready-stale")]


def test_an_answer_missing_from_a_later_read_does_not_requeue_the_step(
    tmp_path: Path,
) -> None:
    """Only an answer handled as rejected requeues: one the next read no
    longer shows (deleted, say) leaves the answered build cancelled."""
    flow = Flow(tmp_path)
    flow.add("go-1", GO.format(state="building"), "grok-login")
    flow.ci = "pending"
    flow.add("ready-1", READY.format(head=HEADS[0]), "claude-login")
    flow.dispatcher.process(flow.snapshot())
    flow.comments.pop()

    flow.dispatcher.process(flow.snapshot())

    assert _harness_jobs(flow.queue) == [("build", "cancelled")]


def test_a_restored_job_answered_after_the_backup_is_cancelled(tmp_path: Path) -> None:
    """The cycle backup holds the build queued. The builder then finished it
    and posted its ready, and the state was lost before the next backup.
    The restored go is already handled and never replayed, so the ready
    alone must retire the build."""
    state = tmp_path / "state"
    dispatcher, queue = setup(state)
    go = comment("go-1", GO.format(state="building"), author="grok-login", created_at=1)
    ready = comment(
        "ready-1", READY.format(head=HEADS[0]), author="claude-login", created_at=101
    )
    dispatcher.process(ticket(go, head=HEADS[0]))
    saved = tmp_path / "backup" / "orch.db"
    backup(queue.path, saved)
    build = queue.claim("claude")
    assert build is not None
    queue.finish(build.id, owner="claude", handoff_comment_id=ready.id)
    shutil.rmtree(state)
    restore(saved, state / "d.db")

    dispatcher, queue = setup(state)
    dispatcher.process(ticket(go, ready, head=HEADS[0], ci={HEADS[0]: CIResult("success")}))

    assert _harness_jobs(queue) == [("build", "cancelled"), ("review", "queued")]
    assert queue.claim("claude") is None


@pytest.mark.parametrize("attempt", [1, 2])
def test_a_job_failing_after_the_merge_is_neither_retried_nor_stuck(
    tmp_path: Path, attempt: int
) -> None:
    """The merge leaves a running job alone. When it then fails, T6 queues
    no retry (attempt 1) and marks no stuck with no stuck wake (attempt 2)."""
    flow = Flow(tmp_path)
    assert flow.go() == "queued-build"
    if attempt == 2:
        first = flow.queue.claim("claude")
        assert first is not None
        flow.queue.finish(first.id, owner="claude", handoff_comment_id=None)
        flow.dispatcher.reconcile_jobs()
    running = flow.queue.claim("claude")
    assert running is not None and running.request.attempt == attempt
    flow.dispatcher.record_terminal(REPO, TICKET, "merged")

    flow.queue.finish(running.id, owner="claude", handoff_comment_id=None)
    flow.dispatcher.reconcile_jobs()

    assert [(row["state"], row["attempt"]) for row in jobs(flow.queue, "build")][-1] == (
        "failed",
        attempt,
    )
    assert flow.queue.claim("claude") is None
    assert (flow.state()["workflow_state"], flow.state()["stuck"]) == ("merged", 0)
    assert wakes(flow.queue, "stuck") == 0


def test_a_finished_ticket_is_never_marked_stuck(tmp_path: Path) -> None:
    """Every stuck path (CI red, device offline, declined, job failed) goes
    through one write; on a merged or done ticket it writes nothing."""
    dispatcher, queue = setup(tmp_path)
    dispatcher.record_terminal(REPO, TICKET, "merged")

    dispatcher._mark_stuck_and_wake(
        repo=REPO,
        ticket_id=TICKET,
        source_comment_id="c-1",
        timeout_seconds=3600,
        reason="device-offline",
        detail="{}",
    )

    assert state_row(queue)["stuck"] == 0
    assert wakes(queue, "stuck") == 0


# --- Round 4: no claimable work after the merge, none for an answered step ---------


def _released_claim(poller: Poller, job_id: str) -> None:
    poller.at(5 * 60 + 1)


def _released_unstarted(poller: Poller, job_id: str) -> None:
    poller.pool.attach_runner(job_id, owner="codex", pid=11, start=None, alive=lambda *_: False)
    poller.at(5 * 60 + 1)


def _early_exit(poller: Poller, job_id: str) -> None:
    poller.pool.attach_runner(job_id, owner="codex", pid=11, start=None, alive=lambda *_: True)
    with poller.queue.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        requeue_unstarted(
            connection,
            job_id,
            now=poller.now[0],
            event=REQUEUED_EARLY_EXIT,
            reason="its command ended without reaching the harness",
            slot_guard="harness = ? AND runner_pid IS ?",
            slot_params=("codex", 11),
        )
        connection.commit()


def _start_failed(poller: Poller, job_id: str) -> None:
    poller.pool.attach_runner(job_id, owner="codex", pid=11, start=None, alive=lambda *_: True)
    assert poller.pool.requeue_unstarted(
        job_id, owner="codex", runner=11, reason="the checkout failed"
    )


def _released_by_its_bot(poller: Poller, job_id: str) -> None:
    poller.queue.release(job_id, owner="codex")


def _runner_died_unstarted(poller: Poller, job_id: str) -> None:
    poller.pool.attach_runner(job_id, owner="codex", pid=11, start=None, alive=lambda *_: True)
    poller.now[0] += 20
    poller.queue.heartbeat(job_id, owner="codex")
    poller.at(90)


@pytest.mark.parametrize(
    "recover",
    [
        _released_claim,
        _released_unstarted,
        _early_exit,
        _start_failed,
        _released_by_its_bot,
        _runner_died_unstarted,
    ],
)
def test_recovering_a_job_after_the_merge_retires_it(tmp_path: Path, recover) -> None:
    """The merge leaves a claimed job alone. Every path that would put it
    back in the queue once its worker is known absent cancels it instead:
    a finished ticket has no claimable harness work."""
    poller = Poller(tmp_path)
    job_id = poller.review()
    poller.claim()
    poller.dispatcher.record_terminal("maxharris1/factory", "72", "merged")

    recover(poller, job_id)

    assert poller.row(job_id)["state"] == "cancelled"
    assert poller.pool.placement(job_id) is None
    assert poller.pool.claim("codex") is None
    assert poller.events()[-1] == (job_id, "cancelled")


def test_a_job_released_on_an_open_ticket_is_still_requeued(tmp_path: Path) -> None:
    poller = Poller(tmp_path, box=False)
    job_id = poller.review()
    poller.claim()
    poller.at(301)
    assert poller.row(job_id)["state"] == "queued"


def test_an_answer_rejected_after_the_merge_requeues_nothing(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.add("go-1", GO.format(state="building"), "grok-login")
    flow.ci = "pending"
    flow.add("ready-1", READY.format(head=HEADS[0]), "claude-login")
    flow.dispatcher.process(flow.snapshot())
    (build,) = jobs(flow.queue, "build")
    flow.dispatcher.record_terminal(REPO, TICKET, "merged")

    with flow.queue.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        assert not flow.queue.requeue(build["id"], "answer-rejected:ready-1", connection=connection)
        connection.commit()

    assert _harness_jobs(flow.queue) == [("build", "cancelled")]
    assert flow.queue.claim("claude") is None


class _Waiter:
    """`orch wait` for every harness, claiming at each label write of a pass."""

    def __init__(self, flow: Flow):
        self.claimed: list[str] = []
        write = flow.actions.set_state

        def set_state(ticket: TrackedTicket, state: str) -> None:
            write(ticket, state)
            for harness in ("claude", "codex"):
                job = flow.queue.claim(harness)
                if job is not None:
                    self.claimed.append(job.request.action)

        flow.actions.set_state = set_state


def _restored(tmp_path: Path) -> Flow:
    """A cycle backup holds the build queued; the builder then finished it,
    and the state directory was lost and restored from that backup."""
    state = tmp_path / "state"
    flow = Flow(state)
    assert flow.go() == "queued-build"
    saved = tmp_path / "backup" / "orch.db"
    backup(flow.queue.path, saved)
    flow.finish("claude")
    shutil.rmtree(state)
    restore(saved, state / "d.db")
    flow.dispatcher, flow.queue = setup(
        state, flow.now, review_actions=flow.actions, grok_delivery=flow.grok
    )
    return flow


def test_a_restored_job_is_unclaimable_until_a_pass_reads_its_thread(
    tmp_path: Path,
) -> None:
    flow = _restored(tmp_path)

    assert flow.queue.claim("claude") is None


def test_a_restored_answered_build_is_never_claimable_during_the_pass(
    tmp_path: Path,
) -> None:
    flow = _restored(tmp_path)
    flow.add("ready-1", READY.format(head=HEADS[0]), "claude-login")
    waiter = _Waiter(flow)

    flow.dispatcher.process(flow.snapshot())

    assert "build" not in waiter.claimed
    assert outcome(flow.queue, "ready-1") == "queued-review"
    assert ("build", "cancelled") in _harness_jobs(flow.queue)


def test_a_restored_unanswered_build_is_queued_again_by_the_pass(tmp_path: Path) -> None:
    flow = _restored(tmp_path)

    flow.dispatcher.process(flow.snapshot())

    assert _harness_jobs(flow.queue) == [("build", "queued")]
    rerun = flow.queue.claim("claude")
    assert rerun is not None and rerun.request.action == "build"


def test_a_replayed_plan_gap_is_never_claimable_once_answered(tmp_path: Path) -> None:
    """A fresh database replays go, a round-1 plan with gaps, the round-2
    plan and the ready. Plan round 2 and the build are answered in the read,
    so no label write of the pass exposes either to a waiter."""
    flow = Flow(tmp_path)
    flow.add("go-1", GO.format(state="planning"), "grok-login")
    flow.add("plan-1", PLAN.format(round=1, ac="unmet"), "claude-login")
    flow.add("plan-2", PLAN.format(round=2, ac="met"), "claude-login")
    flow.add("ready-1", READY.format(head=HEADS[0]), "claude-login")
    waiter = _Waiter(flow)

    flow.dispatcher.process(flow.snapshot())

    assert [outcome(flow.queue, c) for c in ("plan-1", "plan-2", "ready-1")] == [
        "queued-plan-gap",
        "queued-plan-build",
        "queued-review",
    ]
    assert not {"plan", "build"} & set(waiter.claimed)
    assert [job for job in _harness_jobs(flow.queue) if job[0] != "review"] == [
        ("plan", "cancelled"),
        ("plan", "cancelled"),
        ("build", "cancelled"),
    ]


def test_a_replayed_answers_resume_is_never_claimable_once_answered(
    tmp_path: Path,
) -> None:
    """The builder asked, Grok answered and the builder's ready came after:
    replaying the answers resumes a build the ready already answered."""
    flow = Flow(tmp_path)
    flow.add("go-1", GO.format(state="building"), "grok-login")
    flow.add("questions-1", QUESTIONS, "claude-login")
    flow.add("answers-1", ANSWERS, "grok-login")
    flow.ci = "pending"
    flow.add("ready-1", READY.format(head=HEADS[0]), "claude-login")
    waiter = _Waiter(flow)

    flow.dispatcher.process(flow.snapshot())

    assert "build" not in waiter.claimed
    assert {state for action, state in _harness_jobs(flow.queue)} == {"cancelled"}


def _restored_claim(tmp_path: Path, release: str):
    """A cycle backup holds the build claimed (#108 round 5); the builder
    then finished it, and the state directory was lost and restored."""
    state = tmp_path / "state"
    flow = Flow(state)
    flow.go()
    config = config_of(machines_file(state))
    pool = DevicePool(flow.queue, config)
    claimed = pool.claim("claude")
    assert claimed is not None
    build = claimed[0]
    if release != "claimed":
        pool.attach_runner(build.id, owner="claude", pid=11, start=None, alive=lambda *_: False)
    saved = tmp_path / "backup" / "orch.db"
    backup(flow.queue.path, saved)
    flow.queue.finish(build.id, owner="claude", handoff_comment_id="ready-1")
    shutil.rmtree(state)
    restore(saved, state / "d.db")
    flow.now[0] += 400
    flow.dispatcher, flow.queue = setup(
        state, flow.now, review_actions=flow.actions, grok_delivery=flow.grok
    )
    pool = DevicePool(flow.queue, config)
    flow.dispatcher.devices = DeviceMonitor(
        pool, config, FakeTransport(), alive=lambda *_: False
    )
    return flow, pool, build


def _release(flow: Flow, pool: DevicePool, build: Job, release: str) -> None:
    if release == "start-failed":
        assert pool.requeue_unstarted(build.id, owner="claude", runner=11, reason="start failed")
    else:
        flow.dispatcher.poll_once(FakeSource(flow.snapshot()))


RELEASES = ("claimed", "unstarted", "start-failed")


@pytest.mark.parametrize("release", RELEASES)
def test_a_restored_claim_answered_after_the_backup_is_never_claimable(
    tmp_path: Path, release: str
) -> None:
    flow, pool, build = _restored_claim(tmp_path, release)
    flow.head = HEADS[1]
    flow.add("ready-1", READY.format(head=flow.head), "claude-login")
    flow.dispatcher.process(flow.snapshot())

    _release(flow, pool, build, release)

    assert pool.claim("claude") is None
    flow.dispatcher.poll_once(FakeSource(flow.snapshot()))
    assert pool.claim("claude") is None
    assert ("build", "cancelled") in _harness_jobs(flow.queue)


@pytest.mark.parametrize("release", RELEASES)
def test_a_restored_unanswered_claim_is_queued_again_by_the_next_pass(
    tmp_path: Path, release: str
) -> None:
    flow, pool, build = _restored_claim(tmp_path, release)

    _release(flow, pool, build, release)
    flow.dispatcher.poll_once(FakeSource(flow.snapshot()))

    rerun = pool.claim("claude")
    assert rerun is not None and rerun[0].id == build.id


def _restored_dead(tmp_path: Path, action: str):
    """A cycle backup holds a plan or build whose worker had started (#108
    round 6); the job then finished, the state directory was lost and
    restored, and its runner and worker are gone, so T6 takes it."""
    state = tmp_path / "state"
    flow = Flow(state)
    flow.post("go-1", GO.format(state="planning" if action == "plan" else "building"), "grok-login")
    config = config_of(machines_file(state))
    pool = DevicePool(flow.queue, config)
    claimed = pool.claim("claude")
    assert claimed is not None and claimed[0].request.action == action
    job = claimed[0]
    pool.attach_runner(job.id, owner="claude", pid=11, start=None, alive=lambda *_: False)
    pool.attach_worker(job.id, owner="claude", pid=22, start=None)
    saved = tmp_path / "backup" / "orch.db"
    backup(flow.queue.path, saved)
    flow.queue.finish(job.id, owner="claude", handoff_comment_id="answer-1")
    shutil.rmtree(state)
    restore(saved, state / "d.db")
    flow.now[0] += 400
    flow.dispatcher, flow.queue = setup(
        state, flow.now, review_actions=flow.actions, grok_delivery=flow.grok
    )
    pool = DevicePool(flow.queue, config)
    flow.dispatcher.devices = DeviceMonitor(
        pool, config, FakeTransport(), alive=lambda *_: False
    )
    return flow, pool


def _answer(flow: Flow, action: str) -> None:
    flow.head = HEADS[1]
    body = PLAN.format(round=1, ac="met") if action == "plan" else READY.format(head=flow.head)
    flow.add("answer-1", body, "claude-login")


def _claims(pool: DevicePool) -> list[tuple[str, int]]:
    claimed = []
    while (value := pool.claim("claude")) is not None:
        claimed.append((value[0].request.action, value[0].request.attempt))
    return claimed


@pytest.mark.parametrize("read", ("full", "deferred"))
@pytest.mark.parametrize("action", ("plan", "build"))
def test_a_restored_dead_job_answered_after_the_backup_is_never_retried(
    tmp_path: Path, action: str, read: str
) -> None:
    flow, pool = _restored_dead(tmp_path, action)
    _answer(flow, action)

    flow.dispatcher.poll_once(FakeSource(flow.snapshot()) if read == "full" else FakeSource())

    assert (action, 2) not in _claims(pool)
    flow.dispatcher.poll_once(FakeSource(flow.snapshot()))
    assert (action, 2) not in _claims(pool)
    assert (action, "queued") not in _harness_jobs(flow.queue)


@pytest.mark.parametrize("action", ("plan", "build"))
def test_a_restored_dead_unanswered_job_is_retried_after_a_pass(
    tmp_path: Path, action: str
) -> None:
    flow, pool = _restored_dead(tmp_path, action)

    flow.dispatcher.poll_once(FakeSource())
    assert _claims(pool) == []
    flow.dispatcher.poll_once(FakeSource(flow.snapshot()))

    assert _claims(pool) == [(action, 2)]


def _restored_failed(tmp_path: Path, action: str, attempt: int):
    """A cycle backup holds a plan or build that already failed with T6 still
    to handle it (#108 round 7): its runner posted the answer and exited
    without returning the comment ID after the poll's reconcile. The state
    directory was then lost and restored."""
    state = tmp_path / "state"
    flow = Flow(state)
    flow.post("go-1", GO.format(state="planning" if action == "plan" else "building"), "grok-login")
    config = config_of(machines_file(state))
    pool = DevicePool(flow.queue, config)
    job, _ = pool.claim("claude")
    if attempt == 2:
        flow.queue.finish(job.id, owner="claude", handoff_comment_id=None)
        flow.dispatcher.reconcile_jobs()
        job, _ = pool.claim("claude")
        assert job.request.attempt == 2
    flow.queue.finish(job.id, owner="claude", handoff_comment_id=None)
    saved = tmp_path / "backup" / "orch.db"
    backup(flow.queue.path, saved)
    shutil.rmtree(state)
    restore(saved, state / "d.db")
    flow.dispatcher, flow.queue = setup(
        state, flow.now, review_actions=flow.actions, grok_delivery=flow.grok
    )
    return flow, DevicePool(flow.queue, config)


@pytest.mark.parametrize("attempt", (1, 2))
@pytest.mark.parametrize("read", ("full", "deferred"))
@pytest.mark.parametrize("action", ("plan", "build"))
def test_a_restored_failed_job_answered_before_the_backup_is_settled_by_its_answer(
    tmp_path: Path, action: str, read: str, attempt: int
) -> None:
    flow, pool = _restored_failed(tmp_path, action, attempt)
    _answer(flow, action)

    flow.dispatcher.poll_once(FakeSource(flow.snapshot()) if read == "full" else FakeSource())

    assert (action, 2) not in _claims(pool)
    assert not flow.state()["stuck"]
    flow.dispatcher.poll_once(FakeSource(flow.snapshot()))
    assert (action, 2) not in _claims(pool)
    assert (action, "queued") not in _harness_jobs(flow.queue)
    assert not flow.state()["stuck"]
    assert outcome(flow.queue, "answer-1") is not None


@pytest.mark.parametrize("action", ("plan", "build"))
def test_a_restored_failed_unanswered_job_is_retried_after_a_read(
    tmp_path: Path, action: str
) -> None:
    flow, pool = _restored_failed(tmp_path, action, 1)

    flow.dispatcher.poll_once(FakeSource())
    assert _claims(pool) == []
    flow.dispatcher.poll_once(FakeSource(flow.snapshot()))

    assert _claims(pool) == [(action, 2)]


@pytest.mark.parametrize("action", ("plan", "build"))
def test_a_restored_second_failure_unanswered_goes_stuck_only_after_a_read(
    tmp_path: Path, action: str
) -> None:
    flow, pool = _restored_failed(tmp_path, action, 2)

    flow.dispatcher.poll_once(FakeSource())
    assert not flow.state()["stuck"]
    flow.dispatcher.poll_once(FakeSource(flow.snapshot()))

    assert flow.state()["stuck"]
    assert _claims(pool) == []



def _thread(flow: Flow, rounds: int) -> list:
    steps = [flow.go, lambda: flow.finish("claude")]
    for number in range(1, rounds + 1):
        steps += [
            lambda n=number: flow.ready(HEADS[n]),
            lambda: flow.finish("codex"),
        ]
        if number < rounds:
            steps += [
                lambda n=number: flow.revise(f"{n}/3"),
                lambda: flow.finish("claude"),
            ]
    steps.append(lambda: flow.approve(f"{rounds}/3"))
    return steps


def _last_head_backups(rounds: int) -> list[tuple[int, int]]:
    first = 0 if rounds == 1 else 4 * (rounds - 1)
    return [(rounds, index) for index in range(first, 4 * rounds + 1)]


@pytest.mark.parametrize(
    ("rounds", "saved_after"),
    [point for rounds in (1, 2, 3) for point in _last_head_backups(rounds)],
)
def test_a_restore_before_the_last_round_repeats_no_work(
    tmp_path: Path, rounds: int, saved_after: int
) -> None:
    """A backup taken in the last round, or after the round before it queued
    its fix, of a thread that later handed off: the restored database holds
    earlier rounds' review jobs, and the pass still replays the rest as
    answered, keeps the handoff, and leaves no completed step claimable."""
    state = tmp_path / "state"
    flow = Flow(state)
    saved = tmp_path / "backup" / "orch.db"
    for index, step in enumerate(_thread(flow, rounds)):
        step()
        if index == saved_after:
            backup(flow.queue.path, saved)
    assert flow.state()["workflow_state"] == "handed-off"
    shutil.rmtree(state)
    restore(saved, state / "d.db")
    flow.dispatcher, flow.queue = setup(
        state, flow.now, review_actions=flow.actions, grok_delivery=flow.grok
    )
    pool = DevicePool(flow.queue, config_of(machines_file(state)))

    flow.dispatcher.poll_once(FakeSource(flow.snapshot()))

    assert flow.state()["workflow_state"] == "handed-off"
    assert pool.claim("claude") is None
    assert pool.claim("codex") is None
    assert [state for _, state in _harness_jobs(flow.queue) if state == "queued"] == []
