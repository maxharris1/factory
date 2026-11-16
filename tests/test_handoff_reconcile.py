"""#587: a ship: handoff ticket already in external review, its PR out of
draft and no factory work open, is handed-off, never left building."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from lib.dispatcher import TrackedTicket
from lib.queue import JobRequest
from tests.test_dispatcher import GO, READY, factory
from tests.test_lifecycle import HEADS, Flow, jobs, outcome


class ReviewFlow(Flow):
    def __init__(self, tmp_path: Path, **kwargs) -> None:
        super().__init__(tmp_path)
        self.factory = factory(**{"ship": "handoff", **kwargs})
        self.draft: bool | None = False
        self.phase: str | None = None

    def snapshot(self) -> TrackedTicket:
        return dataclasses.replace(
            super().snapshot(), draft=self.draft, linear_state_phase=self.phase
        )

    def poll(self) -> None:
        self.dispatcher.process(self.snapshot())

    def workflow_state(self) -> str:
        return str(self.state()["workflow_state"])

    def drift_to_building(self) -> None:
        with self.queue.connect() as connection:
            connection.execute(
                "UPDATE ticket_state SET workflow_state = 'building' "
                "WHERE repo = 'owner/repo' AND ticket_id = '42'"
            )
            connection.commit()

    def reviewed_then_drifted(self) -> None:
        """Built, reviewed with APPROVE 1/3, then the row drifted back to building."""
        assert self.go() == "queued-build"
        self.finish("claude", f"ready-{HEADS[1][:6]}")
        assert self.ready(HEADS[1]) == "queued-review"
        self.finish("codex", "approve-1/3")
        self.drift_to_building()

    def stale_approve(self) -> None:
        draft, self.draft = self.draft, None
        assert self.approve("1/3") == "stale-approve"
        self.draft = draft
        assert self.workflow_state() == "building"


def harness_jobs(flow: Flow) -> int:
    return sum(1 for row in jobs(flow.queue) if row["recipient"] != "grok")


def test_a_stale_approve_on_a_building_ticket_is_reconciled_to_handed_off(
    tmp_path: Path,
) -> None:
    flow = ReviewFlow(tmp_path)
    flow.reviewed_then_drifted()
    flow.stale_approve()
    before = harness_jobs(flow)

    flow.poll()

    assert flow.workflow_state() == "handed-off"
    assert flow.actions.states[-1] == "handed-off"
    assert harness_jobs(flow) == before


def test_a_linear_ticket_in_ready_for_review_is_reconciled_with_no_approve(
    tmp_path: Path,
) -> None:
    flow = ReviewFlow(tmp_path, tickets="linear")
    flow.phase = "ready-for-review"
    assert flow.go() == "queued-build"
    assert flow.workflow_state() == "building"
    flow.finish("claude", f"ready-{HEADS[5][:6]}")
    before = harness_jobs(flow)

    flow.add(f"ready-{HEADS[5][:6]}", READY.format(head=HEADS[5]), "claude-login")
    flow.poll()

    assert outcome(flow.queue, f"ready-{HEADS[5][:6]}") == "stale-ready-head"
    assert flow.workflow_state() == "handed-off"
    assert harness_jobs(flow) == before


def test_a_reconciled_ticket_stays_handed_off_when_the_head_moves(
    tmp_path: Path,
) -> None:
    flow = ReviewFlow(tmp_path)
    flow.reviewed_then_drifted()
    flow.stale_approve()
    flow.poll()
    assert flow.workflow_state() == "handed-off"
    before = harness_jobs(flow)

    flow.head = HEADS[2]
    flow.poll()

    assert flow.workflow_state() == "handed-off"
    assert harness_jobs(flow) == before


def test_a_builder_ready_after_the_reconcile_still_reenters_review(
    tmp_path: Path,
) -> None:
    flow = ReviewFlow(tmp_path)
    flow.reviewed_then_drifted()
    flow.stale_approve()
    flow.poll()
    reviews = len(jobs(flow.queue, "review"))

    assert flow.ready(HEADS[2]) == "queued-review"

    assert flow.workflow_state() == "review"
    assert len(jobs(flow.queue, "review")) == reviews + 1


def test_an_approve_on_a_building_ticket_in_review_is_handed_off_not_stale(
    tmp_path: Path,
) -> None:
    flow = ReviewFlow(tmp_path)
    flow.reviewed_then_drifted()
    before, stale = harness_jobs(flow), len(jobs(flow.queue, "stale"))

    assert flow.approve("1/3") == "handed-off-reconciled"

    assert flow.workflow_state() == "handed-off"
    assert harness_jobs(flow) == before
    assert len(jobs(flow.queue, "stale")) == stale


def test_a_queued_or_running_build_keeps_the_ticket_building(tmp_path: Path) -> None:
    flow = ReviewFlow(tmp_path)
    flow.reviewed_then_drifted()
    flow.stale_approve()
    flow.queue.enqueue(
        JobRequest(
            repo="owner/repo",
            ticket_id="42",
            action="build",
            recipient="claude",
            source_comment_id="go-2",
            timeout_seconds=5400,
        ),
        dedupe_key="t1:owner/repo#42:build:g2",
    )

    flow.poll()
    assert flow.workflow_state() == "building"

    assert flow.queue.claim("claude") is not None
    flow.poll()
    assert flow.workflow_state() == "building"


def test_a_queued_review_keeps_a_drifted_ticket_building(tmp_path: Path) -> None:
    flow = ReviewFlow(tmp_path)
    flow.reviewed_then_drifted()
    flow.stale_approve()
    flow.draft = None
    assert flow.ready(HEADS[2]) == "queued-review"
    flow.drift_to_building()
    flow.draft = False

    flow.poll()

    assert flow.workflow_state() == "building"


def test_a_draft_or_unread_draft_pr_stays_building(tmp_path: Path) -> None:
    for draft in (True, None):
        flow = ReviewFlow(tmp_path / str(draft))
        flow.draft = draft
        flow.reviewed_then_drifted()
        assert flow.approve("1/3") == "stale-approve"
        flow.poll()
        assert flow.workflow_state() == "building"


def test_a_merge_ship_or_merge_finish_line_stays_building(tmp_path: Path) -> None:
    for name, config in (
        ("merge", {"ship": "merge"}),
        ("finish-merge", {"finish_line": "merge"}),
    ):
        flow = ReviewFlow(tmp_path / name, **config)
        flow.reviewed_then_drifted()
        assert flow.approve("1/3") == "stale-approve"
        flow.poll()
        assert flow.workflow_state() == "building"


def test_a_finished_build_whose_ready_is_unread_stays_building(tmp_path: Path) -> None:
    flow = ReviewFlow(tmp_path, tickets="linear")
    flow.phase = "ready-for-review"
    assert flow.post("go-1", GO.format(state="building"), "grok-login") == "queued-build"
    flow.finish("claude", "ready-unread")

    flow.poll()

    assert flow.workflow_state() == "building"


def test_an_unhandled_failed_build_keeps_the_ticket_building(tmp_path: Path) -> None:
    flow = ReviewFlow(tmp_path, tickets="linear")
    flow.phase = "ready-for-review"
    assert flow.go() == "queued-build"
    job = flow.queue.claim("claude")
    assert job is not None
    flow.queue.finish(job.id, owner="claude", handoff_comment_id=None)

    flow.poll()

    assert flow.workflow_state() == "building"
