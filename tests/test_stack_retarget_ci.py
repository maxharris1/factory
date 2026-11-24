from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatch.constants import OWN_CLOSE_SETTLE_SECONDS
from lib.dispatch.types import CheckRow, CIChange, CIResult, FailedCheck, ci_fresh
from lib.dispatcher import TicketRef
from lib.feeder import FEEDER_TABLE
from lib.github_actions import GhResult
from lib.intake_live import OPEN_PULLS_TTL, REF_STATUS_CACHE
from lib.poll_source import FedPulls, LivePollSource, _cache_ttl
from tests.merge_queue_support import sha
from tests.test_ci_fresh import AFTER, BEFORE, CHANGE, FreshGh, factory_md, iso, run
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


def old_run(started_at: float, created_at: float = BEFORE, conclusion: str = "success") -> CheckRow:
    return replace(
        actions_row("ci-ok", started_at, run_id="777"),
        conclusion=conclusion,
        run_created_at=created_at,
    )


def new_base(created_at: float) -> CIResult:
    row = replace(actions_row("ci-ok", created_at, run_id="888"), run_created_at=created_at)
    return ci_fresh(CIResult("success", checks=(row,)), (RETARGET,), ("ci-ok",))


def old_base_red() -> CIResult:
    red = CIResult(
        "failure",
        "failed: ci-ok",
        failed=(FailedCheck("ci-ok", "777", "1"),),
        checks=(old_run(BEFORE - 1, conclusion="failure"),),
    )
    return ci_fresh(red, (RETARGET,), ("ci-ok",))


def rerun_green() -> CIResult:
    return ci_fresh(CIResult("success", checks=(old_run(AFTER),)), (RETARGET,), ("ci-ok",))


def test_a_rerun_or_delayed_job_of_a_pre_retarget_run_is_not_green_on_the_new_base() -> None:
    rerun = CIResult("success", checks=(old_run(AFTER),))
    new_run = CIResult("success", checks=(old_run(AFTER, created_at=AFTER),))

    assert ci_fresh(rerun, (RETARGET,), ()).pending == ("ci-ok (stale after retarget)",)
    assert ci_fresh(new_run, (RETARGET,), ()) is new_run


def test_an_actions_row_with_no_run_creation_time_is_not_green_on_the_new_base() -> None:
    unread = CIResult("success", checks=(old_run(AFTER, created_at=None),))

    assert not ci_fresh(unread, (RETARGET,), ()).clears


def test_a_red_from_before_the_retarget_waits_for_new_base_ci_instead_of_failing() -> None:
    held = old_base_red()

    assert held.status == "pending"
    assert held.failed == ()
    assert held.pending == ("ci-ok (stale after retarget)",)


def test_a_red_on_the_new_base_still_fails_and_names_only_its_own_run() -> None:
    new_red = replace(old_run(AFTER, created_at=AFTER, conclusion="failure"), name="lint", run_id="888")
    red = CIResult(
        "failure",
        failed=(FailedCheck("ci-ok", "777", "1"), FailedCheck("lint", "888", "2")),
        checks=(old_run(BEFORE - 1, conclusion="failure"), new_red),
    )

    result = ci_fresh(red, (RETARGET,), ())

    assert result.status == "failure"
    assert result.failed == (FailedCheck("lint", "888", "2"),)


def test_an_old_base_red_is_not_rerun_and_its_rerun_does_not_merge(tmp_path: Path) -> None:
    board, pr = queued(tmp_path)
    board.raw[sha(1)] = old_base_red()
    board.poll()
    board.raw[sha(1)] = rerun_green()
    board.poll()
    board.poll()

    assert board.failed_reruns == [] and board.full_reruns == []
    assert board.actions.merges == []

    board.raw[sha(1)] = new_base(AFTER)
    board.poll()

    assert board.actions.merges == [(True, sha(1))]


def test_a_ready_on_an_old_base_red_requests_no_rerun_and_its_rerun_is_not_handed_off(
    tmp_path: Path,
) -> None:
    board = FreshRunBoard(tmp_path)
    board.factory = replace(board.factory, ship="handoff", finish_line=None)
    stacked(board, ["main"])
    pr = board.open("42", sha(1))
    board.raw[sha(1)] = old_base_red()
    board.go(pr, "go-42")
    board.poll()
    board.finish("claude")
    board.ready(pr, pr.head)
    board.poll()
    board.raw[sha(1)] = rerun_green()
    board.poll()

    assert board.failed_reruns == [] and board.full_reruns == []
    assert board.queue.claim("codex") is None

    board.raw[sha(1)] = new_base(AFTER)
    board.poll()
    board.finish("codex")
    board.approve(pr)
    board.poll()
    board.poll()

    assert board.actions.handoffs == [sha(1)]


class RunGh(FreshGh):
    def __init__(self, started_at: float, created_at: float) -> None:
        job = "https://github.com/acme/widgets/actions/runs/777/job/1"
        super().__init__(factory_md(), [{**run("test", started_at), "details_url": job}], [], 0)
        self.created_at = created_at

    def __call__(self, argv, timeout):
        if any(part.endswith("/actions/runs/777") for part in argv):
            self.calls.append(list(argv))
            return GhResult(0, json.dumps({"id": 777, "created_at": iso(self.created_at)}), "")
        return super().__call__(argv, timeout)


def run_read(tmp_path: Path, gh: RunGh) -> CIResult:
    database = tmp_path / "orch.db"
    record(database, "1450", "12", "github")
    with sqlite3.connect(database) as connection:
        connection.execute(f"UPDATE {FEEDER_TABLE} SET base_branch = 'main', retarget_at = ?", (CHANGE,))
    gh.pull = {"state": "open", "base": {"ref": "main", "sha": "b" * 40}}
    source = LivePollSource(config(), gh_runner=gh, fed_pulls=FedPulls(database), wall=lambda: AFTER)
    (ticket,) = source.tracked_tickets((TicketRef("acme/widgets", "12", "github"),))
    return ticket.ci[gh.head]


def test_the_poller_holds_a_rerun_of_a_run_created_before_the_retarget(tmp_path: Path) -> None:
    gh = RunGh(started_at=AFTER, created_at=BEFORE)

    assert run_read(tmp_path, gh).pending == ("test (stale after retarget)",)
    assert gh.read("/actions/runs/777")


def test_the_poller_clears_a_run_created_after_the_retarget(tmp_path: Path) -> None:
    assert run_read(tmp_path, RunGh(started_at=AFTER, created_at=AFTER)).clears


def test_a_retarget_stale_green_in_the_merge_queue_does_not_merge_and_is_not_rerun(
    tmp_path: Path,
) -> None:
    board, pr = queued(tmp_path)
    board.raw[sha(1)] = new_base(BEFORE)
    board.poll()
    board.poll()

    assert board.actions.merges == []
    assert board.full_reruns == [] and board.failed_reruns == []
    assert board.state("42")["workflow_state"] == "merge-queue"

    board.raw[sha(1)] = new_base(AFTER)
    board.poll()

    assert board.actions.merges == [(True, sha(1))]


def test_a_retarget_stale_green_is_not_handed_off(tmp_path: Path) -> None:
    board = FreshRunBoard(tmp_path)
    board.factory = replace(board.factory, ship="handoff", finish_line=None)
    base = [PARENT_BRANCH]
    stacked(board, base)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr)
    board.raw[sha(1)] = new_base(BEFORE)
    base[0] = "main"
    board.poll()
    board.poll()

    assert board.actions.handoffs == []

    board.raw[sha(1)] = new_base(AFTER)
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


def test_a_cached_closed_read_after_the_recovery_reopen_does_not_abandon_the_ticket(
    tmp_path: Path,
) -> None:
    queue, github, dispatcher = noop_rebase(tmp_path, crash_after_close=True)
    with pytest.raises(SystemExit):
        dispatcher.process(polled(tmp_path, github))
    cached_closed = polled(tmp_path, github)
    dispatcher.process(cached_closed)
    assert github.pulls[(STACK_REPO, CHILD_PR)]["state"] == "open"
    reopened_at = queue.now()

    queue.clock = lambda: reopened_at + 30
    dispatcher.process(cached_closed)

    assert workflow_state(tmp_path) != "abandoned"
    assert dispatcher.review_actions.pull_events == ["close", "reopen"]

    queue.clock = lambda: reopened_at + OWN_CLOSE_SETTLE_SECONDS + 1
    dispatcher.process(polled(tmp_path, github))

    assert workflow_state(tmp_path) == "review"


def test_the_own_close_hold_outlasts_every_cached_github_read() -> None:
    assert OWN_CLOSE_SETTLE_SECONDS >= OPEN_PULLS_TTL
    assert OWN_CLOSE_SETTLE_SECONDS >= _cache_ttl(REF_STATUS_CACHE)
    assert OWN_CLOSE_SETTLE_SECONDS >= _cache_ttl("60s")


def test_a_person_closing_the_pr_after_the_restart_still_abandons_the_ticket(tmp_path: Path) -> None:
    queue, github, dispatcher = noop_rebase(tmp_path)
    dispatcher.process(polled(tmp_path, github))
    github.pulls[(STACK_REPO, CHILD_PR)]["state"] = "closed"
    reopened_at = queue.now()
    queue.clock = lambda: reopened_at + OWN_CLOSE_SETTLE_SECONDS + 1

    dispatcher.process(polled(tmp_path, github))

    assert dispatcher.review_actions.pull_events == ["close", "reopen"]
    assert workflow_state(tmp_path) == "abandoned"


def test_ci_fresh_holds_a_pre_retarget_green_and_clears_a_later_one() -> None:
    before = CIResult("success", checks=(CheckRow("test", "completed", "success", started_at=BEFORE),))
    after = CIResult("success", checks=(CheckRow("test", "completed", "success", started_at=AFTER),))

    assert ci_fresh(before, (RETARGET,), ()).stale == (RETARGET,)
    assert ci_fresh(after, (RETARGET,), ()) is after
