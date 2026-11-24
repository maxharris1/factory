from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatch.types import CheckRow, CIChange, CIResult, ci_fresh
from lib.dispatcher import TicketRef
from lib.feeder import FEEDER_TABLE
from lib.poll_source import FedPulls, LivePollSource
from tests.merge_queue_support import sha
from tests.test_ci_fresh import AFTER, BEFORE, CHANGE, FreshGh, factory_md, run
from tests.test_ci_fresh_run import FreshRunBoard, actions_row, queued
from tests.test_live_poll import config
from tests.test_poll_source_stack import record
from tests.test_ship_stacked_handoff import PARENT_BRANCH, stacked
from tests.test_stack_retarget_transition import (
    Actions,
    land,
    ready_on,
    rebases,
    wired,
    workflow_state,
)
from tests.test_ticket_base_branch import CHILD_PR, STACK_REPO, child_row, polled_child

RETARGET = CIChange("retarget", CHANGE)


def retargeted_read(tmp_path: Path, started_at: float, *, retarget_at: float | None = CHANGE):
    database = tmp_path / "orch.db"
    record(database, "1450", "12", "github")
    with sqlite3.connect(database) as connection:
        connection.execute(f"UPDATE {FEEDER_TABLE} SET base_branch = 'main', retarget_at = ?", (retarget_at,))
    gh = FreshGh(factory_md(), [run("test", started_at)], [], 0)
    gh.pull = {"state": "open", "base": {"ref": "main", "sha": "b" * 40}}
    source = LivePollSource(config(), gh_runner=gh, fed_pulls=FedPulls(database), wall=lambda: AFTER)
    (ticket,) = source.tracked_tickets((TicketRef("acme/widgets", "12", "github"),))
    return ticket.ci[gh.head]


def test_the_poller_holds_a_green_from_before_the_retarget_at_the_same_head(tmp_path: Path) -> None:
    ci = retargeted_read(tmp_path, BEFORE)

    assert ci.status == "pending"
    assert not ci.clears
    assert ci.pending == ("test (stale after retarget)",)


def test_the_poller_clears_a_green_that_started_after_the_retarget(tmp_path: Path) -> None:
    assert retargeted_read(tmp_path, AFTER).clears


def test_a_child_never_retargeted_keeps_its_green(tmp_path: Path) -> None:
    assert retargeted_read(tmp_path, BEFORE, retarget_at=None).clears


def test_the_retarget_records_its_ci_cut_when_it_moves_the_base(tmp_path: Path) -> None:
    queue, github, dispatcher = wired(tmp_path)
    assert child_row(tmp_path)["retarget_at"] is None
    land(github)

    dispatcher.process(polled_child(tmp_path, github))

    assert child_row(tmp_path)["retarget_at"] == queue.now()


def test_a_retarget_stale_green_in_the_merge_queue_does_not_merge_and_is_not_rerun(
    tmp_path: Path,
) -> None:
    board, pr = queued(tmp_path)
    board.raw[sha(1)] = ci_fresh(CIResult("success", checks=(actions_row("ci-ok", BEFORE),)), (RETARGET,), ("ci-ok",))
    board.poll()
    board.poll()

    assert board.actions.merges == []
    assert board.full_reruns == [] and board.failed_reruns == []
    assert board.state("42")["workflow_state"] == "merge-queue"

    board.raw[sha(1)] = ci_fresh(CIResult("success", checks=(actions_row("ci-ok", AFTER),)), (RETARGET,), ("ci-ok",))
    board.poll()

    assert board.actions.merges == [(True, sha(1))]


def test_a_retarget_stale_green_is_not_handed_off(tmp_path: Path) -> None:
    board = FreshRunBoard(tmp_path)
    board.factory = replace(board.factory, ship="handoff", finish_line=None)
    base = [PARENT_BRANCH]
    stacked(board, base)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr)
    board.raw[sha(1)] = ci_fresh(CIResult("success", checks=(actions_row("ci-ok", BEFORE),)), (RETARGET,), ("ci-ok",))
    base[0] = "main"
    board.poll()
    board.poll()

    assert board.actions.handoffs == []

    board.raw[sha(1)] = ci_fresh(CIResult("success", checks=(actions_row("ci-ok", AFTER),)), (RETARGET,), ("ci-ok",))
    board.poll()
    board.poll()

    assert board.actions.handoffs == [sha(1)]


class Restart(Actions):
    def __init__(self, github, *, crash_after_close: bool = False) -> None:
        super().__init__(github)
        self.crash_after_close = crash_after_close
        self.pull_events: list[str] = []

    def close_pull(self, ticket) -> None:
        self.pull_events.append("close")
        self.github.pulls[(STACK_REPO, CHILD_PR)]["state"] = "closed"
        if self.crash_after_close:
            self.crash_after_close = False
            raise SystemExit("poller died between the close and the reopen")

    def reopen(self, ticket) -> None:
        self.pull_events.append("reopen")
        self.github.pulls[(STACK_REPO, CHILD_PR)]["state"] = "open"


def noop_rebase(tmp_path: Path, **actions):
    queue, github, dispatcher = wired(tmp_path, state="review")
    dispatcher.review_actions = Restart(github, **actions)
    land(github)
    dispatcher.process(polled_child(tmp_path, github))
    (rebase,) = rebases(queue)
    assert rebase["rebase"]["from_head"] == sha(3)
    fix = queue.claim("claude")
    queue.finish(fix.id, owner="claude", handoff_comment_id="ready-9")
    return queue, github, dispatcher


def polled(tmp_path: Path, github):
    ticket = ready_on(tmp_path, github)
    return replace(ticket, closed=github.pulls[(STACK_REPO, CHILD_PR)].get("state") == "closed")


def own_close(tmp_path: Path) -> tuple[str | None, str | None]:
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        return connection.execute("SELECT own_close_head, own_close_step FROM ticket_state").fetchone()


def test_a_rebase_that_pushes_no_head_closes_and_reopens_the_pr_once(tmp_path: Path) -> None:
    queue, github, dispatcher = noop_rebase(tmp_path)

    dispatcher.process(polled(tmp_path, github))
    dispatcher.process(polled(tmp_path, github))

    assert dispatcher.review_actions.pull_events == ["close", "reopen"]
    assert github.pulls[(STACK_REPO, CHILD_PR)]["state"] == "open"
    assert workflow_state(tmp_path) == "review"
    assert own_close(tmp_path) == (sha(3), "reopened")


def test_a_rebase_that_pushed_a_new_head_does_not_close_the_pr(tmp_path: Path) -> None:
    queue, github, dispatcher = noop_rebase(tmp_path)

    dispatcher.process(ready_on(tmp_path, github, sha(8)))

    assert dispatcher.review_actions.pull_events == []
    assert own_close(tmp_path) == (None, None)


def test_a_crash_between_the_close_and_the_reopen_reopens_on_the_next_poll(tmp_path: Path) -> None:
    queue, github, dispatcher = noop_rebase(tmp_path, crash_after_close=True)

    with pytest.raises(SystemExit):
        dispatcher.process(polled(tmp_path, github))
    assert github.pulls[(STACK_REPO, CHILD_PR)]["state"] == "closed"
    assert own_close(tmp_path) == (sha(3), "closing")

    dispatcher.process(polled(tmp_path, github))

    assert dispatcher.review_actions.pull_events == ["close", "reopen"]
    assert github.pulls[(STACK_REPO, CHILD_PR)]["state"] == "open"
    assert workflow_state(tmp_path) != "abandoned"

    dispatcher.process(polled(tmp_path, github))
    dispatcher.process(polled(tmp_path, github))

    assert dispatcher.review_actions.pull_events == ["close", "reopen"]
    assert workflow_state(tmp_path) == "review"
    assert own_close(tmp_path) == (sha(3), "reopened")


def test_a_person_closing_the_pr_after_the_restart_still_abandons_the_ticket(tmp_path: Path) -> None:
    queue, github, dispatcher = noop_rebase(tmp_path)
    dispatcher.process(polled(tmp_path, github))
    github.pulls[(STACK_REPO, CHILD_PR)]["state"] = "closed"

    dispatcher.process(polled(tmp_path, github))

    assert dispatcher.review_actions.pull_events == ["close", "reopen"]
    assert workflow_state(tmp_path) == "abandoned"


def test_ci_fresh_holds_a_pre_retarget_green_and_clears_a_later_one() -> None:
    before = CIResult("success", checks=(CheckRow("test", "completed", "success", started_at=BEFORE),))
    after = CIResult("success", checks=(CheckRow("test", "completed", "success", started_at=AFTER),))

    assert ci_fresh(before, (RETARGET,), ()).stale == (RETARGET,)
    assert ci_fresh(after, (RETARGET,), ()) is after
