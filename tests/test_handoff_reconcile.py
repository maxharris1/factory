from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path

import pytest

from lib.dispatcher import TrackedTicket
from lib.queue import JobRequest
from tests.test_dispatcher import GO, READY, factory
from tests.test_lifecycle import APPROVE, HEADS, Flow, jobs, outcome, round_of


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


class ExternalFlow(ReviewFlow):
    def snapshot(self) -> TrackedTicket:
        return dataclasses.replace(super().snapshot(), ready_starts_review=True)


REGISTERED = "approve-1/3"


def _no_ac(flow: ReviewFlow) -> None:
    body = APPROVE.format(round="1/3", head=flow.head).replace(" ac=met", "")
    flow.add(REGISTERED, body, "codex-login")


def _open_blocking(flow: ReviewFlow) -> None:
    body = APPROVE.format(round="1/3", head=flow.head) + "\n1. [blocking] f-1. Defect.\n"
    flow.add(REGISTERED, body, "codex-login")


def _edited(flow: ReviewFlow) -> None:
    flow.add(REGISTERED, APPROVE.format(round="1/3", head=flow.head), "codex-login")
    flow.comments[-1] = dataclasses.replace(flow.comments[-1], edited_at=flow.now[0] + 1)


def _over_cap(flow: ReviewFlow) -> None:
    flow.add(REGISTERED, APPROVE.format(round="4/3", head=flow.head), "codex-login")


@pytest.mark.parametrize("post", [_no_ac, _open_blocking, _edited, _over_cap])
def test_a_refused_approve_never_hands_off_a_building_ticket(
    tmp_path: Path, post: Callable[[ReviewFlow], None]
) -> None:
    flow = ReviewFlow(tmp_path)
    flow.reviewed_then_drifted()
    post(flow)

    flow.poll()
    flow.poll()

    assert outcome(flow.queue, REGISTERED).startswith("rejected-")
    assert flow.workflow_state() == "building"


def test_a_ready_at_the_reconciled_head_is_already_shipped(tmp_path: Path) -> None:
    flow = ExternalFlow(tmp_path)
    assert flow.ready(HEADS[1]) == "queued-review"
    flow.finish("codex", "approve-1/3")
    flow.drift_to_building()
    assert flow.approve("1/3") == "handed-off-reconciled"
    reviews = len(jobs(flow.queue, "review"))

    assert flow.post("ready-again", READY.format(head=flow.head), "claude-login") == (
        "ready-already-shipped"
    )
    assert len(jobs(flow.queue, "review")) == reviews


def test_a_reentry_after_a_reconciled_approve_counts_its_round(tmp_path: Path) -> None:
    flow = ExternalFlow(tmp_path)
    assert flow.ready(HEADS[1]) == "queued-review"
    flow.finish("codex", "approve-1/3")
    assert flow.approve("1/3") == "handed-off"
    assert flow.ready(HEADS[2]) == "queued-review"
    flow.finish("codex", "approve-2/3")
    flow.drift_to_building()
    assert flow.approve("2/3") == "handed-off-reconciled"

    assert flow.ready(HEADS[3]) == "queued-review"
    assert round_of(jobs(flow.queue, "review")[-1]) == "3/3"


def test_a_stale_approve_reconciled_by_the_poll_counts_its_round(tmp_path: Path) -> None:
    flow = ReviewFlow(tmp_path)
    flow.reviewed_then_drifted()
    flow.stale_approve()
    flow.poll()
    assert flow.workflow_state() == "handed-off"

    assert flow.post("ready-again", READY.format(head=flow.head), "claude-login") == (
        "ready-already-shipped"
    )
    assert flow.ready(HEADS[2]) == "queued-review"
    assert round_of(jobs(flow.queue, "review")[-1]) == "2/3"
