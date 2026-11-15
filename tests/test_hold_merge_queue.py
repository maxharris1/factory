"""#323: a held PR on a ship: merge, finish_line: merge repo merges from
the merge queue once the approver's ok covers its head."""

from __future__ import annotations

from pathlib import Path

from lib.dispatcher import LabelEvent, TrackedTicket
from lib.factory_md import HoldConfig
from lib.github_actions import GitHubReviewActions
from tests.merge_queue_support import Board, BoardActions, sha
from tests.test_github_actions import (
    HEAD,
    MARK_READY,
    MERGE,
    PULL,
    adapter,
    draft_pull,
    served,
)

APPROVER = "grok-login"
OK = "<!-- orch:v1 kind=ok head={head} -->\nOK after UI QA.\n"


def held_board(tmp_path: Path) -> Board:
    return Board(
        tmp_path,
        ship="merge",
        hold=HoldConfig(approver=APPROVER, paths=("src/**/*.tsx",)),
    )


def open_held(board: Board, head: str = sha(1)):
    pr = board.open("42", head)
    pr.changed_files = ("src/view/Button.tsx",)
    board.build_and_approve(pr)
    return pr


def test_a_paths_hold_with_an_ok_merges_from_the_queue(tmp_path: Path) -> None:
    board = held_board(tmp_path)
    pr = open_held(board)
    assert board.state("42")["workflow_state"] == "awaiting-ok"
    assert board.actions.merges == []

    board.post(pr, "ok-1", OK.format(head=pr.head), APPROVER)
    board.poll()

    assert board.actions.merges == [(True, sha(1))]
    assert board.state("42")["workflow_state"] == "merged"


def test_a_paths_hold_without_an_ok_stays_held(tmp_path: Path) -> None:
    board = held_board(tmp_path)
    open_held(board)

    for _ in range(3):
        board.poll()

    assert board.actions.merges == []
    assert board.state("42")["workflow_state"] == "awaiting-ok"


def test_a_label_hold_with_an_ok_merges_and_a_label_put_back_after_the_ok_holds_again(
    tmp_path: Path,
) -> None:
    board = Board(tmp_path, ship="merge", hold=HoldConfig(approver=APPROVER))
    pr = board.open("42", sha(1))
    pr.labels = ("factory:hold",)
    pr.label_events = (relabel(board, "e1"),)
    board.build_and_approve(pr, mergeable=None)
    assert board.state("42")["workflow_state"] == "awaiting-ok"

    board.post(pr, "ok-1", OK.format(head=pr.head), APPROVER)
    board.poll()
    assert board.state("42")["workflow_state"] == "merge-queue"
    pr.label_events += (relabel(board, "e2"),)
    pr.mergeable, pr.mergeable_state = True, "clean"
    for _ in range(2):
        board.poll()
    assert board.actions.merges == []
    assert board.log[-1] == f"merge-queue: owner/repo#42 waits at {sha(1)[:12]}: held (label)"

    board.post(pr, "ok-2", OK.format(head=pr.head), APPROVER)
    board.poll()
    assert board.actions.merges == [(True, sha(1))]


def test_the_queue_logs_each_wait_reason_once_per_head(tmp_path: Path) -> None:
    board = Board(tmp_path, ship="merge", hold=HoldConfig(approver=APPROVER))
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=None)
    board.poll()
    assert board.state("42")["workflow_state"] == "merge-queue"
    pr.labels = ("factory:hold",)
    pr.mergeable, pr.mergeable_state = True, "clean"

    for _ in range(3):
        board.poll()
    pr.push(sha(2))
    for _ in range(3):
        board.poll()

    assert board.actions.merges == []
    assert [line for line in board.log if " waits at " in line] == [
        f"merge-queue: owner/repo#42 waits at {sha(1)[:12]}: not mergeable (pending)",
        f"merge-queue: owner/repo#42 waits at {sha(1)[:12]}: held (label)",
        f"merge-queue: owner/repo#42 waits at {sha(2)[:12]}: "
        f"approval at {sha(1)[:12]} does not cover {sha(2)[:12]}",
    ]


class GitHubMergeActions(BoardActions):
    """The board's fake, except merge sends the real adapter's gh calls."""

    def __init__(self, board: Board, github: GitHubReviewActions):
        super().__init__(board)
        self.github = github

    def merge(self, ticket: TrackedTicket, *, squash: bool, match_head_commit: str) -> None:
        self.github.merge(ticket, squash=squash, match_head_commit=match_head_commit)
        super().merge(ticket, squash=squash, match_head_commit=match_head_commit)


def test_a_draft_queue_head_is_marked_ready_then_merged_at_the_head(tmp_path: Path) -> None:
    board = held_board(tmp_path)
    pr = open_held(board, head=HEAD)
    github, gh = adapter()
    gh.route(PULL, draft_pull())
    gh.route(MARK_READY, served("graphql-mark-ready.json"))
    gh.route(MERGE, served("merge-squash-200.json"))
    board.actions = board.dispatcher.review_actions = GitHubMergeActions(board, github)

    board.post(pr, "ok-1", OK.format(head=HEAD), APPROVER)
    board.poll()

    assert gh.keys() == [PULL, MARK_READY, MERGE]
    assert f"sha={HEAD}" in gh.calls[-1]
    assert board.state("42")["workflow_state"] == "merged"


def relabel(board: Board, event_id: str) -> LabelEvent:
    board.now[0] += 1
    return LabelEvent(
        id=event_id,
        action="labeled",
        label="factory:hold",
        actor="codex-login",
        created_at=board.now[0],
    )
