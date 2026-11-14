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

from lib.dispatcher import CIResult, Comment, TrackedTicket
from lib.queue import Job, JobRequest
from lib.state_backup import backup, restore
from tests.test_job_state import Poller
from tests.test_dispatcher import (
    GO,
    PLAN,
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

    for _ in range(10):  # one STOPPED_POLL_SECONDS at the default 60 s poll
        flow.dispatcher.poll_once(source)

    assert flow.state()["workflow_state"] == "merged"
    # Flow's handoff ids are not real comments, so #18 flags them.
    assert attention(flow.queue) == ["unverified-handoff"]
    reads = [refs for refs in source.refs if TICKET in refs]
    assert len(reads) == 1
    for _ in range(10):
        flow.dispatcher.poll_once(source)
    assert sum(TICKET in refs for refs in source.refs) == 1  # merged: never read again


def _seed_states(queue, states: dict[str, tuple[str | None, int]]) -> None:
    with queue.connect() as connection:
        for ticket_id, (state, stuck) in states.items():
            connection.execute(
                "INSERT INTO tracked_tickets VALUES (?, ?, 'github', 0)", (REPO, ticket_id)
            )
            connection.execute(
                """
                INSERT INTO ticket_state (
                    repo, ticket_id, gate_count, stuck, workflow_state, timeout_seconds
                ) VALUES (?, ?, 0, ?, ?, 3600)
                """,
                (REPO, ticket_id, stuck, state),
            )


class Config:
    poll_seconds = 30


class RefSource(MergingSource):
    """Reads every ref it is offered, as the live source does on GitHub."""

    config = Config()

    def tracked_tickets(self, refs) -> tuple[TrackedTicket, ...]:
        self.refs.append(tuple(ref.ticket_id for ref in refs))
        return tuple(ticket(ticket_id=ref.ticket_id) for ref in refs)


def _reads(tmp_path: Path, states: dict[str, tuple[str | None, int]], cycles: int):
    dispatcher, queue = setup(tmp_path)
    _seed_states(queue, states)
    source = RefSource(dispatcher)
    for _ in range(cycles):
        dispatcher.poll_once(source)
    return {
        ticket_id: [cycle for cycle, refs in enumerate(source.refs) if ticket_id in refs]
        for ticket_id in states
    }


def test_stopped_states_are_read_every_300_seconds(tmp_path: Path) -> None:
    stopped = {
        "1": ("handed-off", 0),
        "2": ("capped", 0),
        "3": ("blocked", 0),
        "4": ("refused", 0),
        "5": ("review", 1),  # stuck
    }
    reads = _reads(tmp_path, stopped, 30)  # 900 s at poll_seconds: 30
    for ticket_id, cycles in reads.items():
        assert len(cycles) == 3, ticket_id
        assert [later - earlier for earlier, later in zip(cycles, cycles[1:])] == [10, 10]


def test_active_states_are_read_every_cycle(tmp_path: Path) -> None:
    active = {
        "1": (None, 0),
        "2": ("building", 0),
        "3": ("review", 0),
        "4": ("fixing", 0),
        "5": ("awaiting-ok", 0),
        "6": ("waiting-max", 0),
        "7": ("waiting-external", 0),
    }
    reads = _reads(tmp_path, active, 5)
    assert all(cycles == [0, 1, 2, 3, 4] for cycles in reads.values())


def test_stopped_reads_are_spread_across_cycles(tmp_path: Path) -> None:
    stopped = {str(number): ("handed-off", 0) for number in range(40)}
    reads = _reads(tmp_path, stopped, 10)
    first = {cycles[0] for cycles in reads.values()}
    assert all(len(cycles) == 1 for cycles in reads.values())
    assert len(first) >= 5  # not one burst every tenth cycle


def test_a_handed_off_ticket_with_a_replay_is_read_every_cycle(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.hand_off_at(1)
    with flow.queue.connect() as connection:
        connection.execute("DELETE FROM handled_comments WHERE comment_id = 'approve-1/3'")

    refs = flow.dispatcher._tracked_refs(cycle=1)
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
    flow.ci = "pending"  # keep the later ready unhandled

    flow.dispatcher.process(flow.snapshot())

    assert outcome(flow.queue, "revise-1") == "answered-fix"
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


def _release_claimed(poller) -> None:
    poller.at(301)


def _release_by_its_bot(poller) -> None:
    poller.queue.release(poller.job_id, owner="codex")


def _start_failed(poller) -> None:
    poller.pool.attach_runner(poller.job_id, owner="codex", pid=11, start=None, alive=lambda *_: True)
    poller.pool.requeue_unstarted(poller.job_id, owner="codex", runner=11, reason="preflight")


def _runner_died_unstarted(poller) -> None:
    poller.pool.attach_runner(poller.job_id, owner="codex", pid=11, start=None, alive=lambda *_: True)
    poller.now[0] += 20
    poller.queue.heartbeat(poller.job_id, owner="codex")
    poller.at(90)


@pytest.mark.parametrize(
    "release", [_release_claimed, _release_by_its_bot, _start_failed, _runner_died_unstarted]
)
def test_a_job_released_after_the_merge_is_cancelled_not_requeued(tmp_path: Path, release) -> None:
    """A claimed job the merge left alone never runs: whichever path puts it
    back in the queue cancels it instead, so no waiter can claim it."""
    poller = Poller(tmp_path, box=False)
    poller.job_id = poller.review()
    poller.claim()
    poller.dispatcher.record_terminal("maxharris1/factory", "72", "merged")

    release(poller)

    assert poller.row(poller.job_id)["state"] == "cancelled"
    assert poller.pool.claim("codex") is None


def test_a_job_released_on_an_open_ticket_is_still_requeued(tmp_path: Path) -> None:
    poller = Poller(tmp_path, box=False)
    job_id = poller.review()
    poller.claim()
    poller.at(301)
    assert poller.row(job_id)["state"] == "queued"
