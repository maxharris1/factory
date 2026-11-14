"""#130 AC1: a watched PR that closes unmerged is abandoned before any
comment on it acts. No review, carried approval, merge or base write
follows the close, whether the poll's read or only a fresh read shows it."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from lib.queue import Job
from lib.update_check import UpdateClass
from tests.merge_queue_support import PR, Board, sha
from tests.test_merge_queue_gate import after_board

OK = "<!-- orch:v1 kind=ok from=tracy-site role=approver head={head} -->"


def closed_only_on_fresh_reads(board: Board) -> None:
    read = board.fresh_ticket
    board.fresh_ticket = lambda ref: replace(read(ref), closed=True)


def assert_abandoned(board: Board) -> None:
    assert board.state("42")["workflow_state"] == "abandoned"
    assert [job for job in board.jobs() if job["state"] == "queued"] == []
    assert board.actions.merges == []
    assert board.actions.fast_forwards == []


@pytest.mark.parametrize("trivial", [True, False])
@pytest.mark.parametrize("race", [False, True])
def test_an_update_ready_on_a_closed_pr_starts_nothing(
    tmp_path: Path, trivial: bool, race: bool
) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=False)
    board.finish("claude")
    pr.push(sha(2))
    board.verdict = UpdateClass(trivial, "the update check")
    board.ready(pr, pr.head)
    if race:
        closed_only_on_fresh_reads(board)
    else:
        pr.closed = True
    pr.mergeable, pr.mergeable_state = True, "clean"
    board.poll()
    board.poll()

    assert_abandoned(board)
    assert board.state("42")["approved_head"] == sha(1)


@pytest.mark.parametrize("race", [False, True])
def test_an_ok_on_a_closed_approved_pr_never_ships(tmp_path: Path, race: bool) -> None:
    board = after_board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr)
    if race:
        closed_only_on_fresh_reads(board)
    else:
        pr.closed = True
    board.post(pr, "ok", OK.format(head=pr.head), "tracy-login")
    board.poll()
    board.poll()

    assert_abandoned(board)
    assert board.outcome("ok") != "shipped"


def stale_open_on_the_first_read(board: Board, head: str) -> None:
    tracked = board.tracked_tickets
    first = [True]

    def read(refs):
        tickets = tracked(refs)
        if first[0]:
            first[0] = False
            return tuple(replace(t, current_head=head, closed=False) for t in tickets)
        return tickets

    board.tracked_tickets = read


@pytest.mark.parametrize("race", [False, True])
def test_a_builder_ready_on_a_closed_queued_pr_starts_no_review(
    tmp_path: Path, race: bool
) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=None)
    assert board.state("42")["workflow_state"] == "merge-queue"
    pr.push(sha(2))
    ready = board.ready(pr, pr.head)
    if race:
        stale_open_on_the_first_read(board, sha(1))
    pr.closed = True
    board.poll()
    board.poll()

    assert_abandoned(board)
    assert board.outcome(ready) != "queued-review"


def a_reviewer_running_past_its_approve(board: Board, pr: PR) -> Job:
    pr.mergeable, pr.mergeable_state = None, None
    board.go(pr, "go-42")
    board.poll()
    board.finish("claude")
    board.ready(pr, pr.head)
    board.poll()
    reviewer = board.claim("codex")
    board.approve(pr)
    board.poll()
    return reviewer


def an_update_builder_running(board: Board, pr: PR) -> Job:
    board.build_and_approve(pr, mergeable=False)
    assert board.state("42")["workflow_state"] == "updating"
    return board.claim("claude")


@pytest.mark.parametrize(
    "start", [a_reviewer_running_past_its_approve, an_update_builder_running]
)
def test_a_job_failing_after_the_close_is_not_retried(tmp_path: Path, start) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    held = start(board, pr)
    assert board.state("42")["approved_head"] == sha(1)
    pr.closed = True
    board.poll()
    assert board.state("42")["workflow_state"] == "abandoned"
    board.queue.finish(held.id, owner=held.request.recipient, handoff_comment_id=None)
    board.poll()

    assert_abandoned(board)
    assert board.queue.claim(held.request.recipient) is None
