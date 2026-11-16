from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatcher import RetryableShipError, ShipError
from lib.factory_md import HoldConfig
from tests.merge_queue_support import PR, Board, sha
from tests.test_hold_merge_queue import APPROVER, OK, relabel
from tests.test_review_before_ci import board_with, built_with_ci_pending


def approved_on_red_ci(tmp_path: Path, *, hold: bool = False) -> tuple[Board, PR]:
    board = board_with(tmp_path, on=True)
    if hold:
        board.factory = replace(board.factory, hold=HoldConfig(approver=APPROVER))
    pr = built_with_ci_pending(board, sha(1))
    board.ready(pr, pr.head, "ready-1")
    board.poll()
    board.finish("codex")
    pr.ci[pr.head] = "failure"
    approve = board.approve(pr, "1/3", "approve-red")
    board.poll()
    assert board.outcome(approve) == "approve-ci-red-fix"
    board.finish("claude")
    return board, pr


def round_and_cap(board: Board, pr: PR) -> tuple[int, int, int]:
    state = board.state("42")
    ticket = board.snapshot(pr)
    return (
        state["review_round"],
        state["cap_extra"],
        board.dispatcher._effective_cap(ticket),
    )


def test_a_no_change_ready_after_a_red_approve_ships_with_one_review(tmp_path: Path) -> None:
    board, pr = approved_on_red_ci(tmp_path)
    pr.ci[pr.head] = "success"
    ready = board.ready(pr, pr.head, "ready-no-change")
    board.poll()

    assert board.outcome(ready) == "approval-covers:merge-queue"
    assert len(board.jobs("review")) == 1
    assert board.actions.merges == [(True, pr.head)]
    assert board.state("42")["workflow_state"] == "merged"


def test_a_ready_at_a_new_head_with_the_approved_patch_id_ships(tmp_path: Path) -> None:
    board, pr = approved_on_red_ci(tmp_path)
    board.actions.patch_ids.update({sha(1): "a" * 40, sha(2): "a" * 40})
    pr.push(sha(2), ci="success")
    ready = board.ready(pr, sha(2), "ready-same-patch")
    board.poll()

    assert board.outcome(ready) == "approval-covers:merge-queue"
    assert len(board.jobs("review")) == 1
    assert board.actions.merges == [(True, sha(2))]


def test_a_patch_id_read_error_reviews_the_new_head(tmp_path: Path) -> None:
    board, pr = approved_on_red_ci(tmp_path)
    board.actions.patch_ids[sha(1)] = "a" * 40
    board.actions.patch_errors.add(sha(2))
    pr.push(sha(2), ci="success")
    ready = board.ready(pr, sha(2), "ready-unreadable-patch")
    board.poll()

    assert board.outcome(ready) == "queued-review"
    assert len(board.jobs("review")) == 2
    assert board.actions.merges == []


def test_a_covered_ready_with_pending_ci_waits_then_ships_on_green(tmp_path: Path) -> None:
    board, pr = approved_on_red_ci(tmp_path)
    pr.ci[pr.head] = "pending"
    ready = board.ready(pr, pr.head, "ready-pending")
    board.poll()
    board.poll()

    assert board.outcome(ready) is None
    assert len(board.jobs("review")) == 1
    assert board.actions.merges == []

    pr.ci[pr.head] = "success"
    board.poll()
    assert board.outcome(ready) == "approval-covers:merge-queue"
    assert len(board.jobs("review")) == 1
    assert board.actions.merges == [(True, pr.head)]


def test_a_covered_ready_on_red_ci_queues_the_ci_fix_and_no_review(tmp_path: Path) -> None:
    board, pr = approved_on_red_ci(tmp_path)
    board.actions.patch_ids.update({sha(1): "a" * 40, sha(2): "a" * 40})
    pr.push(sha(2), ci="failure")
    ready = board.ready(pr, sha(2), "ready-still-red")
    board.poll()

    assert board.outcome(ready) == "queued-ci-fix"
    assert len(board.jobs("review")) == 1
    assert [job["head"] for job in board.jobs("fix")][-1] == sha(2)
    board.finish("claude")

    pr.ci[sha(2)] = "success"
    board.poll()
    assert board.outcome(ready) == "approval-covers:merge-queue"
    assert len(board.jobs("review")) == 1
    assert board.actions.merges == [(True, sha(2))]


def test_three_red_covered_heads_mark_the_ticket_stuck(tmp_path: Path) -> None:
    board, pr = approved_on_red_ci(tmp_path)
    board.actions.patch_ids.update({sha(n): "a" * 40 for n in (1, 2, 3)})
    for n in (2, 3):
        pr.push(sha(n), ci="failure")
        board.ready(pr, sha(n), f"ready-red-{n}")
        board.poll()
        if n == 2:
            board.finish("claude")

    assert board.outcome("ready-red-3") == "stuck-ci-red-3"
    assert board.state("42")["stuck"] == 1
    assert len(board.jobs("review")) == 1
    assert board.actions.merges == []


def test_a_ready_at_a_differing_patch_after_a_red_approve_is_reviewed_again(
    tmp_path: Path,
) -> None:
    board, pr = approved_on_red_ci(tmp_path)
    pr.push(sha(2), ci="success")
    ready = board.ready(pr, sha(2), "ready-new-patch")
    board.poll()

    assert board.outcome(ready) == "queued-review"
    [_, review] = board.jobs("review")
    assert review["head"] == sha(2)
    assert json.loads(review["detail"])["round"].startswith("1/")
    assert board.actions.merges == []

    pr.push(sha(1), ci="success")
    back = board.ready(pr, sha(1), "ready-back-at-the-approved-head")
    board.poll()
    assert board.outcome(back) != "approval-covers:merge-queue"
    assert board.actions.merges == []


@pytest.mark.parametrize("ci", ["pending", "failure", "success"])
def test_a_ready_that_queues_no_review_keeps_the_round_and_cap(
    tmp_path: Path, ci: str
) -> None:
    board, pr = approved_on_red_ci(tmp_path)
    before = round_and_cap(board, pr)
    board.actions.patch_ids.update({sha(1): "a" * 40, sha(2): "a" * 40})
    pr.push(sha(2), ci=ci)
    board.ready(pr, sha(2), f"ready-{ci}")
    board.poll()

    assert len(board.jobs("review")) == 1
    assert round_and_cap(board, pr) == before


def test_a_shipped_covered_approval_keeps_its_round_for_reentry(tmp_path: Path) -> None:
    board, pr = approved_on_red_ci(tmp_path)
    pr.ci[pr.head] = "success"
    pr.mergeable, pr.mergeable_state = None, None
    board.ready(pr, pr.head, "ready-no-change")
    board.poll()
    assert board.state("42")["workflow_state"] == "merge-queue"

    pr.push(sha(2), ci="pending")
    ready = board.ready(pr, sha(2), "ready-after-ship")
    board.poll()

    assert board.outcome(ready) == "queued-review"
    assert json.loads(board.jobs("review")[-1]["detail"])["round"].startswith("2/")



def test_a_held_covered_ready_waits_for_the_ok_then_ships_with_one_review(
    tmp_path: Path,
) -> None:
    board, pr = approved_on_red_ci(tmp_path, hold=True)
    pr.labels = ("factory:hold",)
    pr.label_events = (relabel(board, "e1"),)
    pr.ci[pr.head] = "success"
    ready = board.ready(pr, pr.head, "ready-held")
    board.poll()
    board.poll()

    assert board.state("42")["workflow_state"] == "awaiting-ok"
    assert board.outcome(ready) is None
    assert len(board.jobs("review")) == 1

    board.post(pr, "ok-1", OK.format(head=pr.head), APPROVER)
    board.poll()

    assert board.outcome(ready) == "approval-covers:merge-queue"
    assert len(board.jobs("review")) == 1
    assert board.actions.merges == [(True, pr.head)]


def ship_intents(board: Board) -> list[tuple[str, str, str | None]]:
    with board.queue.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute("SELECT comment_id, status, redrive FROM ship_intents")
        ]


def test_a_crash_after_a_covered_handoff_reconciles_the_approves_intent(
    tmp_path: Path,
) -> None:
    board, pr = approved_on_red_ci(tmp_path)
    pr.ci[pr.head] = "success"
    ready = board.ready(pr, pr.head, "ready-crash")
    board.actions.crash_after_handoff = True
    with pytest.raises(SystemExit):
        board.poll()
    board.actions.crash_after_handoff = False
    board.poll()

    assert board.outcome(ready) == "approval-covers:ship-unconfirmed"
    assert ship_intents(board) == [("approve-red", "unconfirmed", None)]
    assert len(board.jobs("review")) == 1


def test_a_failed_covered_handoff_is_redriven_once_under_the_approve(tmp_path: Path) -> None:
    board, pr = approved_on_red_ci(tmp_path)
    pr.ci[pr.head] = "success"
    ready = board.ready(pr, pr.head, "ready-error")
    handoff = board.actions.handoff
    calls: list[str] = []

    def fail_once(ticket, head):
        calls.append(head)
        if len(calls) == 1:
            error = ShipError("handoff failed before write")
            error.write_confirmed = False
            raise error
        return handoff(ticket, head)

    board.actions.handoff = fail_once
    board.poll()
    assert board.outcome(ready) == "approval-covers:failed-ShipError"
    assert ship_intents(board) == [("approve-red", "failed", "eligible")]

    board.poll()
    assert ship_intents(board) == [("approve-red", "completed", "spent")]
    assert len(board.jobs("review")) == 1


def test_a_retryable_covered_ship_error_retries_the_approve_on_the_next_poll(
    tmp_path: Path,
) -> None:
    board, pr = approved_on_red_ci(tmp_path)
    pr.ci[pr.head] = "success"
    ready = board.ready(pr, pr.head, "ready-retry")
    handoff = board.actions.handoff
    calls: list[str] = []

    def fail_once(ticket, head):
        calls.append(head)
        if len(calls) == 1:
            raise RetryableShipError("temporary poll budget exhausted")
        return handoff(ticket, head)

    board.actions.handoff = fail_once
    board.poll()
    assert board.state("42")["workflow_state"] == "fixing"
    assert ship_intents(board) == []

    board.poll()
    assert board.outcome(ready) == "approval-covers:merge-queue"
    assert ship_intents(board) == [("approve-red", "completed", None)]
    assert len(board.jobs("review")) == 1
