"""#130 AC1: a watched PR that closes unmerged is abandoned before any
comment on it acts. No review, carried approval, merge or base write
follows the close, whether the poll's read or only a fresh read shows it."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatcher import TrackedTicket
from lib.queue import Job
from lib.update_check import UpdateClass
from tests.merge_queue_support import PR, Board, BoardActions, sha
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


def a_second_ready(board: Board, pr: PR) -> str:
    return board.ready(pr, pr.head, "second-ready")


def a_go(board: Board, pr: PR) -> str:
    board.go(pr, "late-go")
    return "late-go"


def a_revise(board: Board, pr: PR) -> str:
    board.revise(pr, "2/3")
    return "revise-42-2/3"


def an_approve(board: Board, pr: PR) -> str:
    return board.approve(pr, "2/3", "late-approve")


def an_update_ready(board: Board, pr: PR) -> tuple[str, str]:
    board.build_and_approve(pr, mergeable=False)
    board.finish("claude")
    pr.push(sha(2))
    return board.ready(pr, pr.head, "first-ready"), sha(2)


def a_queued_builder_ready(board: Board, pr: PR) -> tuple[str, str]:
    board.build_and_approve(pr, mergeable=None)
    pr.push(sha(2))
    return board.ready(pr, pr.head, "first-ready"), sha(1)


@pytest.mark.parametrize("first", [an_update_ready, a_queued_builder_ready])
@pytest.mark.parametrize("later", [a_second_ready, a_go, a_revise, an_approve])
def test_no_later_comment_in_the_pass_acts_after_a_fresh_read_abandons(
    tmp_path: Path, first, later
) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    abandoning, cached_head = first(board, pr)
    follower = later(board, pr)
    stale_open_on_the_first_read(board, cached_head)
    pr.closed = True
    board.poll()
    board.poll()

    assert board.outcome(abandoning) == "ready-on-closed-pr"
    assert board.outcome(follower) == "ticket-abandoned"
    assert_abandoned(board)
    assert board.queue.claim("codex") is None
    assert board.queue.claim("claude") is None


@pytest.mark.parametrize("stale", [False, True])
def test_a_shippers_fresh_closed_read_abandons_before_a_later_go(
    tmp_path: Path, stale: bool
) -> None:
    board = after_board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr)
    board.post(pr, "ok", OK.format(head=pr.head), "tracy-login")
    board.go(pr, "late-go")
    if stale:
        stale_open_on_the_first_read(board, pr.head)
    pr.closed = True
    board.poll()
    board.poll()

    assert board.outcome("late-go") in (None, "ticket-abandoned")
    assert_abandoned(board)
    assert board.queue.claim("claude") is None
    assert board.queue.claim("codex") is None


QUESTIONS = "<!-- orch:v1 kind=questions from=claude-bot role=builder -->\nWhich side?"


def test_closing_an_update_paused_on_a_question_ends_the_watch(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=False)
    job = board.claim("claude")
    board.post(pr, "questions-1", QUESTIONS, "claude-login")
    board.queue.finish(job.id, owner="claude", handoff_comment_id="questions-1")
    board.poll()
    assert board.state("42")["workflow_state"] == "waiting-max"
    pr.closed = True
    board.poll()

    assert_abandoned(board)
    assert board.actions.states[-1] == "abandoned"


BASE = sha(90)
BRANCH = "factory/issue-42"


class EmptiedBranch(BoardActions):
    def __init__(self, board: Board, pr: PR):
        super().__init__(board)
        self.pr = pr
        self.tip = BASE
        self.reopened: list[str] = []
        pr.head, pr.closed = BASE, True
        snapshot = board.snapshot
        board.snapshot = lambda pr, *, fresh=False: replace(
            snapshot(pr, fresh=fresh),
            git_branch_name=BRANCH,
            pr_base="main",
            pr_commits=0 if pr.head == BASE else 1,
        )
        board.actions = board.dispatcher.review_actions = self

    def branch_tip(self, ticket: TrackedTicket, branch: str) -> str:
        return self.tip

    def commits_ahead(self, ticket: TrackedTicket, *, base: str, head: str) -> int:
        return 0 if head == BASE else 1

    def reopen(self, ticket: TrackedTicket) -> None:
        self.reopened.append(ticket.ticket_id)
        self.pr.closed = False
        self.pr.head = self.tip


def test_a_branch_emptied_by_a_running_update_is_reopened_once_rebuilt(
    tmp_path: Path,
) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    an_update_builder_running(board, pr)
    actions = EmptiedBranch(board, pr)
    board.poll()
    assert board.state("42")["workflow_state"] == "updating"

    actions.tip = sha(2)
    board.poll()

    assert actions.reopened == ["42"]
    assert board.state("42")["workflow_state"] == "updating"
    assert "42" in [ref.ticket_id for ref in board.last_refs]


def test_a_branch_left_empty_after_the_update_ends_is_abandoned(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    update = an_update_builder_running(board, pr)
    actions = EmptiedBranch(board, pr)
    board.poll()
    board.queue.finish(update.id, owner="claude", handoff_comment_id=None)
    board.poll()

    assert actions.reopened == []
    assert_abandoned(board)
