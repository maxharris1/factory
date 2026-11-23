"""#1419: say what an approver's kind=ok cannot move.

An ok-no-hold on a ticket with no covered APPROVE posts one note naming the
latest verdict, its round, any stop and the comment that moves the ticket,
and raises ok-without-approve. On a held PR, a covering ok is the merge OK
for its head; an ok at another head records hold-ok-other-head and posts
one note naming both heads.
"""

from dataclasses import replace
from pathlib import Path

import pytest

from lib.factory_md import HoldConfig
from tests.test_approver_credential import MAX, AppFlow
from tests.test_dispatcher import FakeReviewActions
from tests.test_repeat import capped_stop, repeat_stop
from tests.test_ship_hold import (
    HEAD,
    NEW_HEAD,
    OK,
    approve,
    hold_factory,
    ok,
    ok_outcome,
    ready,
    revise,
    setup,
    snapshot,
    workflow_state,
)

HOLD_FILE = ("src/view/Board.tsx",)


def notes(actions: FakeReviewActions, comment_id: str = "ok-1") -> list[str]:
    return [body for body in actions.notices if f"<!-- orch:ok-note {comment_id} -->" in body]


def attention(queue, reason: str = "ok-without-approve") -> list[str]:
    with queue.connect() as connection:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT detail FROM needs_attention WHERE reason = ?", (reason,)
            )
        ]


def unheld(flow: AppFlow) -> AppFlow:
    flow.factory = replace(
        flow.factory, ship="handoff", hold=HoldConfig(approver=MAX, paths=("src/view/**",))
    )
    plain = flow.snapshot
    flow.snapshot = lambda: replace(
        plain(), labels=(), changed_files=("lib/cli.py",), label_events=(),
        factory_md_trust_changed=False,
    )
    return flow


def ok_at(flow: AppFlow, comment_id: str = "ok-1") -> str:
    return flow.post(comment_id, OK.format(head=flow.head), MAX)


def test_an_ok_at_a_repeat_stop_posts_one_note_naming_the_stop_and_flags(tmp_path: Path) -> None:
    flow = unheld(repeat_stop(tmp_path))

    assert ok_at(flow) == "ok-no-hold"
    flow.dispatcher.process(flow.snapshot())

    [note] = notes(flow.dispatcher.review_actions)
    assert "comment:revise-2" in note
    assert "`2/3`" in note
    assert "repeat stop" in note and "f-1" in note
    assert "Overrule the finding" in note and "Another round" in note
    assert flow.head in note
    assert len(attention(flow.queue)) == 1


def test_an_ok_at_a_capped_stop_names_the_cap(tmp_path: Path) -> None:
    flow = unheld(capped_stop(tmp_path))

    assert ok_at(flow) == "ok-no-hold"

    [note] = notes(flow.dispatcher.review_actions)
    assert "comment:revise-3" in note and "`3/3`" in note
    assert "review cap" in note
    assert "Another round" in note
    assert len(attention(flow.queue)) == 1


def test_an_ok_in_review_names_the_reviewers_approve_as_the_next_step(tmp_path: Path) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)
    dispatcher.process(snapshot(revise()))

    dispatcher.process(snapshot(revise(), ok(HEAD)))

    assert ok_outcome(queue) == "ok-no-hold"
    [note] = notes(actions)
    assert "comment:revise-1" in note and "`1/3`" in note
    assert "Stop: none" in note
    assert "Moves it forward:" in note
    assert attention(queue)


@pytest.mark.parametrize("state", ["awaiting-ok", "merge-queue", "updating", "handed-off"])
def test_an_ok_at_a_covered_state_posts_nothing(tmp_path: Path, state: str) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)
    with queue.connect() as connection:
        connection.execute(
            "UPDATE ticket_state SET workflow_state = ? WHERE ticket_id = '42'", (state,)
        )
        connection.commit()

    dispatcher.process(snapshot(ok(HEAD), factory=hold_factory(ship="handoff")))

    assert ok_outcome(queue) == "ok-no-hold"
    assert notes(actions) == []
    assert attention(queue) == []


def test_the_flag_clears_once_an_approve_covers_the_head(tmp_path: Path) -> None:
    factory = replace(hold_factory(), finish_line="merge")
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)
    dispatcher.process(snapshot(ok(HEAD), factory=factory))
    assert attention(queue)

    dispatcher.process(snapshot(ok(HEAD), approve(), factory=factory))

    assert workflow_state(queue) == "merge-queue"
    assert attention(queue) == []
    assert len(notes(actions)) == 1


def test_a_held_prs_ok_before_the_approve_is_its_merge_ok(tmp_path: Path) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)

    dispatcher.process(snapshot(ok(HEAD), files=HOLD_FILE))
    assert ok_outcome(queue) == "hold-ok"
    assert actions.merges == []

    dispatcher.process(snapshot(ok(HEAD), approve(), files=HOLD_FILE))

    assert actions.merges == [(True, HEAD)]
    assert workflow_state(queue) == "merged"
    assert notes(actions) == []
    assert attention(queue) == []


def test_an_ok_at_another_head_posts_one_note_naming_both_heads(tmp_path: Path) -> None:
    actions = FakeReviewActions({HEAD: "1" * 40, NEW_HEAD: "2" * 40})
    dispatcher, queue, actions = setup(tmp_path, actions)
    ready(dispatcher, queue, NEW_HEAD)

    dispatcher.process(snapshot(ok(HEAD), head=NEW_HEAD, files=HOLD_FILE))
    dispatcher.process(snapshot(ok(HEAD), head=NEW_HEAD, files=HOLD_FILE))

    assert ok_outcome(queue) == "hold-ok-other-head"
    [note] = notes(actions)
    assert HEAD in note and NEW_HEAD in note
    assert "orch:v1" not in note

    dispatcher.process(snapshot(ok(HEAD), approve(NEW_HEAD), head=NEW_HEAD, files=HOLD_FILE))
    assert actions.merges == []
    assert workflow_state(queue) == "awaiting-ok"


def test_an_ok_whose_head_carries_by_patch_id_is_hold_ok(tmp_path: Path) -> None:
    actions = FakeReviewActions({HEAD: "1" * 40, NEW_HEAD: "1" * 40})
    dispatcher, queue, actions = setup(tmp_path, actions)
    ready(dispatcher, queue, NEW_HEAD)

    dispatcher.process(snapshot(ok(HEAD), head=NEW_HEAD, files=HOLD_FILE))

    assert ok_outcome(queue) == "hold-ok"
    assert notes(actions) == []


def test_an_ok_from_another_login_posts_no_note(tmp_path: Path) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)

    dispatcher.process(snapshot(ok(HEAD, author="claude-login")))

    assert ok_outcome(queue) == "rejected-hold-ok-author"
    assert notes(actions) == []
