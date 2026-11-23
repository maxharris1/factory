"""#1421: the stranded #1401 case, end to end through the poller.

PR 1401 sat at waiting-max after a round 3/3 repeat stop with cap_extra=0.
An approver's overrule go queued nothing, and the approver's kind=ok at the
head was read as a hold-ok on a PR no hold applied to. This replays that
thread through Dispatcher.poll_once with the merge queue's merge stubbed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.factory_md import HoldConfig
from lib.orch_yml import approvals
from tests.merge_queue_support import Board, sha
from tests.test_approver_credential import MAX, load, orch_text, trusted
from tests.test_dispatcher import VERDICT
from tests.test_ship_hold import OK
from tests.test_unsatisfiable_criterion import BRIEF

pytestmark = pytest.mark.usefixtures("every_wake_reaches_grok")

OVERRULE = (
    "<!-- orch:v1 kind=go from=grok-bot role=orchestrator risk=high state=building -->\n"
    "Overrule the finding: f-3 (https://github.com/owner/repo/pull/42#issuecomment-revise-3). "
    "The exit code is right as it is.\n"
)
ROUNDS = (("f-1", "f-2"), ("f-3",), ("f-3",))


def stranded(tmp_path: Path) -> Board:
    config, findings = load(tmp_path, orch_text(enabled=True))
    assert config is not None, findings
    board = Board(
        tmp_path,
        hold=HoldConfig(approver=MAX, paths=("src/view/**",)),
        trusted_logins=trusted(config),
        approvals=approvals(config),
    )
    pr = board.open("42", sha(1))
    pr.changed_files = ("lib/cli.py",)
    board.post(pr, "brief-1", BRIEF, "grok-login")
    board.go(pr, "go-1")
    board.poll()
    board.finish("claude")
    for number, ids in enumerate(ROUNDS, start=1):
        board.ready(pr, pr.head, f"ready-{number}")
        board.poll()
        board.finish("codex")
        header = VERDICT.format(round=f"{number}/3", head=pr.head, verdict="REVISE", risk="high")
        board.post(pr, f"revise-{number}", header + "".join(f"- [blocking] {finding}: x.\n" for finding in ids), "codex-login")
        board.poll()
        if number < len(ROUNDS):
            board.finish("claude")
            pr.push(sha(number + 1))
    return board


def ok_flags(board: Board) -> int:
    with board.queue.connect() as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM needs_attention WHERE reason = 'ok-without-approve'"
        ).fetchone()[0]


def test_the_stranded_1401_ticket_ships_after_the_overrule_ok_and_approve(tmp_path: Path) -> None:
    board = stranded(tmp_path)
    pr = board.pr("42")
    head = pr.head
    stop = board.state("42")
    assert board.outcome("revise-3") == "waiting-max-repeat"
    assert (stop["workflow_state"], stop["review_round"], stop["cap_extra"]) == ("waiting-max", 3, 0)
    assert stop["paused_question_id"] == "revise-3"
    assert stop["approved_head"] is None
    reviews = len(board.jobs("review"))

    board.post(pr, "go-overrule", OVERRULE, MAX)
    board.poll()
    board.post(pr, "ok-1", OK.format(head=head), MAX)
    board.poll()

    assert board.outcome("go-overrule") == "overruled-finding"
    assert board.outcome("ok-1") == "ok-no-hold"

    [note] = [body for body in board.actions.notices if "<!-- orch:ok-note ok-1 -->" in body]
    assert "comment:revise-3" in note and "`3/3`" in note
    assert head in note
    assert "`verdict=APPROVE` with `ac=met`" in note
    assert ok_flags(board) == 1

    [review] = board.jobs("review")[reviews:]
    assert (review["source_comment_id"], review["head"]) == ("go-overrule", head)
    detail = json.loads(review["detail"])
    assert detail["round"] == "3/3"
    assert any(decision["choice"].startswith("Overrule the finding: f-3") for decision in detail["decisions"])
    row = board.state("42")
    assert (row["workflow_state"], row["review_round"], row["cap_extra"]) == ("review", 2, 0)
    assert row["paused_question_id"] is None
    assert all(job["source_comment_id"] != "go-overrule" for job in board.jobs("fix"))

    board.finish("codex")
    pr.mergeable, pr.mergeable_state = None, None
    approve = (
        "<!-- orch:v1 kind=verdict from=codex-bot role=reviewer "
        f"round=3/3 head={head} ac=met verdict=APPROVE risk=high -->\nApproved.\n"
    )
    board.post(pr, "approve-3", approve, "codex-login")
    board.poll()

    assert board.outcome("approve-3") == "merge-queue"
    row = board.state("42")
    assert (row["workflow_state"], row["approved_head"], row["cap_extra"]) == ("merge-queue", head, 0)
    assert board.actions.merges == []
    assert ok_flags(board) == 0

    pr.mergeable, pr.mergeable_state = True, "clean"
    board.poll()

    assert board.actions.merges == [(True, head)]
    assert board.state("42")["workflow_state"] == "merged"
    assert len([body for body in board.actions.notices if "orch:ok-note" in body]) == 1
