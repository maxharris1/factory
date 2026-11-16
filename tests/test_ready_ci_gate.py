from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dashboard import collect_snapshot
from lib.dashboard_pages import lanes, tickets
from lib.dispatch.constants import (
    BASE_UPDATE_GRACE_SECONDS,
    CI_SILENT_REASON,
    CI_SILENT_SECONDS,
)
from lib.dispatch.types import ci_on_base
from tests.merge_queue_support import APPROVE, READY, sha
from tests.test_ready_after_approve import approved_on_red_ci
from tests.test_review_before_ci import (
    board_with,
    built_with_ci_pending,
    ci_failure,
    reviewed_at_pending_ci,
)


def claims(board) -> list[tuple[str, int]]:
    claimed = []
    while (job := board.queue.claim("claude")) is not None:
        claimed.append((job.request.source_comment_id, job.request.attempt))
    return claimed


def test_a_ready_back_on_an_old_red_head_queues_its_fix(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=True)
    pr = built_with_ci_pending(board, sha(1))
    reviewed_at_pending_ci(board, pr, pr.head)
    pr.push(sha(2), ci="failure")
    board.post(pr, "approve-a", APPROVE.format(round="1/3", head=sha(1)), "codex-login")
    board.poll()
    assert board.outcome("approve-a") == "approve-ci-red-fix"
    board.finish("claude")
    pr.push(sha(1), ci="failure")
    ready = board.ready(pr, pr.head, "ready-back-to-a")
    board.poll()
    board.poll()

    assert board.outcome(ready) == "queued-ci-fix"
    assert claims(board) == [(ready, 1)]


def test_a_late_revise_after_a_failed_review_leaves_one_fix(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=True)
    pr = built_with_ci_pending(board, sha(1))
    board.ready(pr, pr.head)
    board.poll()
    board.fail("codex")
    pr.ci[pr.head] = "failure"
    board.ready(pr, pr.head, "ready-after-review-process-failed")
    board.poll()
    revise = board.revise(pr, "1/3")
    board.poll()

    assert claims(board) == [(revise, 1)]


@pytest.mark.parametrize("verdict", ["revise", "approve"])
def test_a_red_ready_before_t6_retries_a_failed_fix_leaves_one_fix(
    tmp_path: Path, verdict: str
) -> None:
    board = board_with(tmp_path, on=True)
    pr = built_with_ci_pending(board, sha(1))
    reviewed_at_pending_ci(board, pr, pr.head)
    pr.ci[pr.head] = "failure"
    verdict_id = getattr(board, verdict)(pr, "1/3")
    board.poll()
    board.fail("claude")
    board.ready(pr, pr.head, "ready-before-fix-t6")
    board.poll()
    board.now[0] += 600
    board.poll()

    assert claims(board) == [(verdict_id, 2)]


@pytest.mark.parametrize("verdict", ["revise", "approve"])
def test_a_red_ready_after_a_failed_fix_readied_another_head_queues_a_fix(
    tmp_path: Path, verdict: str
) -> None:
    board = board_with(tmp_path, on=True)
    pr = built_with_ci_pending(board, sha(1))
    reviewed_at_pending_ci(board, pr, pr.head)
    pr.ci[pr.head] = "failure"
    getattr(board, verdict)(pr, "1/3")
    board.poll()
    fix = board.claim("claude")
    pr.push(sha(2), ci="pending")
    board.ready(pr, pr.head, "ready-b")
    board.poll()
    assert board.outcome("ready-b") == "queued-review"
    board.queue.finish(fix.id, owner="claude", handoff_comment_id=None)
    pr.push(sha(1), ci="failure")
    board.ready(pr, pr.head, "ready-back-on-a")
    board.poll()
    board.now[0] += 600
    board.poll()

    assert board.outcome("ready-back-on-a") == "queued-ci-fix"
    assert claims(board) == [("ready-back-on-a", 1)]


@pytest.mark.parametrize("verdict", ["revise", "approve"])
def test_a_red_ready_waits_while_a_failed_fixs_ready_is_unread(
    tmp_path: Path, verdict: str
) -> None:
    board = board_with(tmp_path, on=True)
    pr = built_with_ci_pending(board, sha(1))
    reviewed_at_pending_ci(board, pr, pr.head)
    pr.ci[pr.head] = "failure"
    verdict_id = getattr(board, verdict)(pr, "1/3")
    board.poll()
    fix = board.claim("claude")
    pr.push(sha(2), ci="pending")
    board.ready(pr, pr.head, "ready-b")
    board.queue.finish(fix.id, owner="claude", handoff_comment_id=None)
    pr.push(sha(1), ci="failure")
    board.ready(pr, pr.head, "ready-back-on-a")
    fresh_ticket = board.fresh_ticket

    def unavailable(ref):
        raise RuntimeError("temporary PR read failure")

    board.fresh_ticket = unavailable
    board.poll()
    assert board.outcome("ready-back-on-a") is None
    board.fresh_ticket = fresh_ticket
    board.now[0] += 600
    board.poll()
    board.poll()

    assert board.outcome("ready-b") == "stale-ready-head"
    assert board.outcome("ready-back-on-a") == "ci-red-in-flight"
    assert claims(board) == [(verdict_id, 2)]


CHECKS = ("build", "lint")


def naming_pending_checks(board) -> None:
    snapshot = board.snapshot

    def named(pr, *, fresh: bool = False):
        ticket = snapshot(pr, fresh=fresh)
        ci = {
            head: replace(result, pending=CHECKS) if result.status == "pending" else result
            for head, result in ticket.ci.items()
        }
        return replace(ticket, ci=ci)

    board.snapshot = named


def ci_waits(board) -> list[dict]:
    with board.queue.connect() as connection:
        rows = connection.execute("SELECT * FROM ci_waits").fetchall()
    return [{**dict(row), "checks": json.loads(row["checks"])} for row in rows]


def test_a_ready_held_for_pending_ci_is_recorded_until_it_is_handled(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=False)
    naming_pending_checks(board)
    pr = built_with_ci_pending(board, sha(1))
    ready = board.ready(pr, pr.head)
    board.poll()
    since = board.now[0]
    board.poll()

    assert board.outcome(ready) is None
    assert ci_waits(board) == [{
        "repo": "owner/repo", "ticket_id": "42", "comment_id": ready, "head": sha(1),
        "reason": "ci-pending", "checks": list(CHECKS), "since": since,
    }]

    pr.ci[pr.head] = "success"
    board.poll()
    assert board.outcome(ready) == "queued-review"
    assert ci_waits(board) == []


def test_a_covered_ready_held_for_pending_ci_is_recorded_until_it_ships(tmp_path: Path) -> None:
    board, pr = approved_on_red_ci(tmp_path)
    naming_pending_checks(board)
    pr.ci[pr.head] = "pending"
    ready = board.ready(pr, pr.head, "ready-pending")
    board.poll()

    assert [(w["comment_id"], w["reason"], w["checks"]) for w in ci_waits(board)] == [
        (ready, "approved-ci-pending", list(CHECKS))
    ]

    pr.ci[pr.head] = "success"
    board.poll()
    assert board.outcome(ready) == "approval-covers:merge-queue"
    assert ci_waits(board) == []


def test_the_wait_clock_runs_from_the_first_ready_on_a_head_and_restarts_on_a_new_head(
    tmp_path: Path,
) -> None:
    board = board_with(tmp_path, on=False)
    pr = built_with_ci_pending(board, sha(1))
    board.ready(pr, pr.head, "ready-a")
    board.poll()
    since = board.now[0]
    board.ready(pr, pr.head, "ready-a-again")
    board.poll()
    board.poll()
    assert [(w["comment_id"], w["since"], w["checks"]) for w in ci_waits(board)] == [
        ("ready-a-again", since, [])
    ]

    pr.push(sha(2), ci="pending")
    board.ready(pr, pr.head, "ready-b")
    board.poll()
    assert [(w["comment_id"], w["head"], w["since"]) for w in ci_waits(board)] == [
        ("ready-b", sha(2), board.now[0])
    ]


def board_cards(board, tmp_path: Path) -> tuple[dict, str]:
    snapshot = collect_snapshot(board.queue.path, tmp_path / "log", now=board.now[0])
    model = tickets.board(snapshot)
    markup = "".join(tickets._card(card) for column in model["columns"] for card in column["cards"])
    return snapshot, markup + "".join(lanes._ci_lane(snapshot))


def test_a_green_ready_held_for_its_stack_is_no_longer_waiting_on_ci(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=False)
    pr = built_with_ci_pending(board, sha(1))
    ready = board.ready(pr, pr.head)
    board.poll()
    assert [w["reason"] for w in ci_waits(board)] == ["ci-pending"]

    class StackHold:
        def feed_once(self):
            return None

        def hold_stacked_review(self, ticket, head):
            return True

    board.dispatcher.feeder = StackHold()
    pr.ci[pr.head] = "success"
    board.poll()

    snapshot, markup = board_cards(board, tmp_path)
    assert board.outcome(ready) is None
    assert ci_waits(board) == []
    assert not snapshot["running"] and not snapshot["queued"]
    assert "waiting on CI" not in markup


def test_a_deleted_ready_leaves_no_ci_wait(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=False)
    pr = built_with_ci_pending(board, sha(1))
    ready = board.ready(pr, pr.head)
    board.poll()
    assert [w["comment_id"] for w in ci_waits(board)] == [ready]

    pr.comments = [comment for comment in pr.comments if comment.id != ready]
    board.poll()

    assert ci_waits(board) == []
    assert "waiting on CI" not in board_cards(board, tmp_path)[1]


def test_a_merged_ticket_leaves_no_ci_wait(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=False)
    pr = built_with_ci_pending(board, sha(1))
    board.ready(pr, pr.head)
    board.poll()
    assert len(ci_waits(board)) == 1

    pr.closed = True
    pr.merged = True
    board.poll()

    assert board.state("42")["workflow_state"] == "merged"
    assert ci_waits(board) == []
    assert "waiting on CI" not in board_cards(board, tmp_path)[1]


def test_a_conflicting_ready_in_its_base_update_grace_is_waiting_on_ci(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=False)
    naming_pending_checks(board)
    pr = built_with_ci_pending(board, sha(1))
    snapshot = board.snapshot

    def unreported(pr, *, fresh: bool = False):
        ticket = snapshot(pr, fresh=fresh)
        return replace(ticket, ci={head: replace(ci, reported=False) for head, ci in ticket.ci.items()})

    board.snapshot = unreported
    pr.conflict()
    ready = board.ready(pr, pr.head)
    board.poll()

    shown, markup = board_cards(board, tmp_path)
    assert board.outcome(ready) is None
    assert [(w["comment_id"], w["reason"]) for w in ci_waits(board)] == [(ready, "ci-pending")]
    assert not shown["running"] and not shown["queued"]
    assert "waiting on CI" in markup
    assert "no job running" not in markup

    board.now[0] += BASE_UPDATE_GRACE_SECONDS
    board.poll()
    assert board.jobs("fix")
    assert ci_waits(board) == []


def stale_head_ready(board, pr) -> None:
    board.ready(pr, sha(1), "ready-other")


def reviewer_ready(board, pr) -> None:
    board.post(pr, "ready-other", READY.format(head=pr.head), "codex-login")


@pytest.mark.parametrize(
    ("post_other", "other_outcome"),
    [
        (stale_head_ready, "stale-ready-head"),
        (reviewer_ready, "rejected-ready-author"),
    ],
)
def test_a_handled_ready_does_not_hide_another_pending_ready(
    tmp_path: Path, post_other, other_outcome: str
) -> None:
    board = board_with(tmp_path, on=False)
    pr = built_with_ci_pending(board, sha(2))
    current = board.ready(pr, pr.head, "ready-current")
    board.poll()
    assert board.outcome(current) is None
    post_other(board, pr)
    outcomes = []
    original = board.dispatcher._t2

    def traced(*args):
        result = original(*args)
        outcomes.append((args[1].id, result))
        return result

    board.dispatcher._t2 = traced
    board.poll()
    snapshot = collect_snapshot(board.queue.path, tmp_path / "log", now=board.now[0])
    model = tickets.board(snapshot)
    cards = "".join(tickets._card(c) for col in model["columns"] for c in col["cards"])
    lane = "".join(lanes._ci_lane(snapshot))
    assert outcomes == [
        ("ready-current", (False, "ci-pending")),
        ("ready-other", (True, other_outcome)),
    ]
    assert [w["comment_id"] for w in ci_waits(board)] == ["ready-current"]
    assert not snapshot["running"] and not snapshot["queued"]
    assert "waiting on CI" in cards
    assert "no job running" not in cards
    assert "waiting on CI" in lane


NO_CI_BASE = "feature/upload-only-intake"


def on_base(board, base: str, *, ci_branches: tuple[str, ...] | None = ("dev", "main")) -> set[str]:
    """The PR targets base. A pending head reports nothing until it is added to the returned set."""
    board.factory = replace(board.factory, ci_branches=ci_branches)
    reported: set[str] = set()
    snapshot = board.snapshot

    def based(pr, *, fresh: bool = False):
        ticket = replace(snapshot(pr, fresh=fresh), pr_base=base)
        ci = {
            head: ci_on_base(
                replace(result, reported=head in reported or result.status != "pending"),
                ticket.no_ci_base,
            )
            for head, result in ticket.ci.items()
        }
        return replace(ticket, ci=ci)

    board.snapshot = based
    return reported


def ci_events(board) -> list[tuple[str, str]]:
    with board.queue.connect() as connection:
        rows = connection.execute("SELECT head, status FROM ci_events").fetchall()
    return [tuple(row) for row in rows]


def ci_silent(board) -> list[tuple[str, str]]:
    with board.queue.connect() as connection:
        rows = connection.execute(
            "SELECT ticket_id, detail FROM needs_attention WHERE reason = ?", (CI_SILENT_REASON,)
        ).fetchall()
    return [tuple(row) for row in rows]


def test_a_ready_on_a_base_ci_never_runs_on_goes_to_review(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=False)
    on_base(board, NO_CI_BASE)
    pr = built_with_ci_pending(board, sha(1))
    ready = board.ready(pr, pr.head)
    board.poll()

    assert board.outcome(ready) == "queued-review"
    assert [(job["recipient"], job["head"]) for job in board.jobs("review")] == [("codex", sha(1))]
    assert ci_waits(board) == []
    assert ci_events(board) == []


@pytest.mark.parametrize("on", [True, False])
def test_a_revise_on_a_base_ci_never_runs_on_queues_a_plain_fix(tmp_path: Path, on: bool) -> None:
    board = board_with(tmp_path, on=on)
    on_base(board, NO_CI_BASE)
    pr = built_with_ci_pending(board, sha(1))
    reviewed_at_pending_ci(board, pr, pr.head)
    revise = board.revise(pr, "1/3")
    board.poll()

    [fix] = [job for job in board.jobs("fix") if job["source_comment_id"] == revise]
    assert ci_failure(fix) is None
    assert ci_events(board) == []


@pytest.mark.parametrize("on", [True, False])
def test_an_approve_on_a_base_ci_never_runs_on_ships_without_recording_green(
    tmp_path: Path, on: bool
) -> None:
    board = board_with(tmp_path, on=on)
    on_base(board, NO_CI_BASE)
    pr = built_with_ci_pending(board, sha(1))
    reviewed_at_pending_ci(board, pr, pr.head)
    approve = board.approve(pr)
    board.poll()

    assert board.outcome(approve) == "merge-queue"
    assert ci_events(board) == []


@pytest.mark.parametrize(
    ("base", "ci_branches"),
    [("dev", ("dev", "main")), (NO_CI_BASE, None)],
    ids=["ci-base", "ci-branches-absent"],
)
def test_a_ready_with_unreported_ci_waits_where_ci_runs(
    tmp_path: Path, base: str, ci_branches: tuple[str, ...] | None
) -> None:
    board = board_with(tmp_path, on=False)
    on_base(board, base, ci_branches=ci_branches)
    pr = built_with_ci_pending(board, sha(1))
    ready = board.ready(pr, pr.head)
    board.poll()

    assert board.outcome(ready) is None
    assert board.jobs("review") == []
    assert [(w["comment_id"], w["reason"]) for w in ci_waits(board)] == [(ready, "ci-pending")]


@pytest.mark.parametrize("reports", ["pending", "success"])
def test_ci_silent_on_a_ci_base_raises_one_flag_until_ci_reports(
    tmp_path: Path, reports: str
) -> None:
    board = board_with(tmp_path, on=False)
    reported = on_base(board, "dev")
    pr = built_with_ci_pending(board, sha(1))
    ready = board.ready(pr, pr.head)
    board.poll()
    board.now[0] += CI_SILENT_SECONDS - 20
    board.poll()
    assert ci_silent(board) == []

    board.now[0] += 20
    for _ in range(3):
        board.poll()
    assert ci_silent(board) == [
        ("42", f"no check run or status on {sha(1)[:12]} after 10 minutes on CI base dev")
    ]

    reported.add(pr.head)
    pr.ci[pr.head] = reports
    board.poll()
    assert ci_silent(board) == []
    assert board.outcome(ready) == (None if reports == "pending" else "queued-review")


def test_ci_silent_is_never_raised_without_ci_branches(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=False)
    on_base(board, "dev", ci_branches=None)
    pr = built_with_ci_pending(board, sha(1))
    ready = board.ready(pr, pr.head)
    board.poll()
    board.now[0] += CI_SILENT_SECONDS * 3
    for _ in range(3):
        board.poll()

    assert ci_silent(board) == []
    assert [(w["comment_id"], w["reason"]) for w in ci_waits(board)] == [(ready, "ci-pending")]
