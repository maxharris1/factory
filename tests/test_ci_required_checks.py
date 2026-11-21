from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatch.types import (
    CHECK_FAILED,
    CheckRow,
    CIResult,
    FailedCheck,
    ci_required,
    pr_ci,
)
from lib.factory_md import FactoryConfig
from lib.github_actions import GhResult
from lib.poll_source import LivePollSource
from tests.merge_queue_support import sha
from tests.test_live_poll import FACTORY, FakeGh, config
from tests.test_ready_ci_gate import ci_waits
from tests.test_review_before_ci import board_with, built_with_ci_pending, reviewed_at_pending_ci

GATE = ("ci-ok",)


def row(name: str, conclusion: str | None = None, status: str | None = None) -> CheckRow:
    if status is None:
        status = "queued" if conclusion is None else "completed"
    return CheckRow(name=name, status=status, conclusion=conclusion)


def adapter(*rows: CheckRow, failed: tuple[FailedCheck, ...] | None = None) -> CIResult:
    if failed is None:
        failed = tuple(FailedCheck(item.name) for item in rows if item.conclusion in CHECK_FAILED)
    pending = tuple(item.name for item in rows if item.conclusion is None)
    if failed:
        log = "failed: " + ", ".join(check.name for check in failed)
        return CIResult("failure", log, failed=failed, pending=pending, checks=rows)
    passed = any(item.conclusion == "success" for item in rows)
    status = "success" if passed and not pending else "pending"
    return CIResult(status, pending=pending, checks=rows)


@pytest.mark.parametrize(
    "others",
    [
        (row("omr-service", "skipped"),),
        (),
        (row("test (3)"),),
        (row("test (3)", status="in_progress"),),
        (row("checks", "failure"),),
        (row("edge-functions-deno", "neutral"), row("test (1)", "success")),
    ],
    ids=["skipped", "absent", "queued", "in-progress", "failed-alias", "neutral"],
)
def test_a_green_ci_ok_clears_whatever_other_jobs_did(others: tuple[CheckRow, ...]) -> None:
    raw = adapter(row("ci-ok", "success"), *others)

    gated = ci_required(raw, GATE)

    assert gated.status == "success"
    assert gated.clears
    assert gated.failed == ()
    assert gated.pending == ()
    assert gated.checks == raw.checks


@pytest.mark.parametrize(
    ("ci_ok", "pending"),
    [
        ((), ("ci-ok (not reported)",)),
        ((row("ci-ok"),), ("ci-ok",)),
        ((row("ci-ok", status="in_progress"),), ("ci-ok",)),
        ((row("ci-ok", "skipped"),), ("ci-ok (no success)",)),
        ((row("ci-ok", "neutral"),), ("ci-ok (no success)",)),
    ],
    ids=["missing", "queued", "in-progress", "skipped", "neutral"],
)
def test_a_ci_ok_without_a_success_is_pending_and_names_only_the_gate(
    ci_ok: tuple[CheckRow, ...], pending: tuple[str, ...]
) -> None:
    raw = adapter(*ci_ok, row("lint", "success"), row("test (3)"))

    gated = ci_required(raw, GATE)

    assert gated.status == "pending"
    assert not gated.clears
    assert gated.pending == pending
    assert gated.reported


def test_a_missing_ci_ok_is_pending_even_when_every_other_job_is_green() -> None:
    raw = adapter(row("lint", "success"), row("test (1)", "success"))
    assert raw.status == "success"

    gated = ci_required(raw, GATE)

    assert gated.status == "pending"
    assert gated.pending == ("ci-ok (not reported)",)


@pytest.mark.parametrize("conclusion", CHECK_FAILED)
def test_a_failed_ci_ok_is_failure_naming_ci_ok(conclusion: str) -> None:
    raw = adapter(row("ci-ok", conclusion), row("omr-service", "skipped"))

    gated = ci_required(raw, GATE)

    assert gated.status == "failure"
    assert not gated.clears
    assert [check.name for check in gated.failed] == ["ci-ok"]
    assert gated.log == "failed: ci-ok"


def test_a_failed_ci_ok_keeps_the_culprit_shard_so_the_cause_sees_it() -> None:
    culprit = FailedCheck("omr-service", run_id="9", job_id="11", cause="infra", signal="runner lost")
    aggregate = FailedCheck("ci-ok", run_id="9", job_id="12")
    raw = adapter(
        row("omr-service", "cancelled"),
        row("ci-ok", "failure"),
        failed=(culprit, aggregate),
    )

    gated = ci_required(raw, GATE)

    assert gated.status == "failure"
    assert gated.failed == (aggregate, culprit)
    assert gated.log == "failed: ci-ok, omr-service"
    assert gated.cause == "infra"


def test_a_failed_non_gate_job_next_to_a_pending_ci_ok_is_pending() -> None:
    raw = adapter(row("ci-ok"), row("test (2)", "failure"))
    assert raw.status == "failure"

    gated = ci_required(raw, GATE)

    assert gated.status == "pending"
    assert gated.failed == ()
    assert gated.pending == ("ci-ok",)


def test_every_glob_needs_a_success_and_a_skipped_shard_beside_a_green_one_is_ignored() -> None:
    gate = ("ci-ok", "test (*)")
    shards = (row("test (1)", "success"), row("test (2)", "skipped"))

    assert ci_required(adapter(row("ci-ok", "success")), gate).pending == ("test (*) (not reported)",)
    assert ci_required(adapter(row("ci-ok", "success"), *shards), gate).status == "success"
    assert ci_required(adapter(row("ci-ok", "success"), row("test (1)")), gate).status == "pending"


@pytest.mark.parametrize(
    "raw",
    [CIResult("pending"), CIResult("pending", reported=False)],
    ids=["short-read", "nothing-reported"],
)
def test_a_short_read_or_an_unreported_head_passes_through(raw: CIResult) -> None:
    assert ci_required(raw, GATE) is raw


@pytest.mark.parametrize(
    "raw",
    [
        adapter(row("test", "success"), row("lint", "skipped")),
        adapter(row("lint", "skipped")),
        adapter(row("ci-ok", "failure")),
        adapter(row("test")),
        CIResult("pending", reported=False),
    ],
    ids=["success", "all-skipped", "failure", "pending", "nothing-reported"],
)
def test_without_the_key_the_result_is_untouched(raw: CIResult) -> None:
    assert ci_required(raw, ()) is raw


FACTORY_CONFIG = FactoryConfig(
    base_branch="main",
    tickets="github",
    builder_harness="claude",
    reviewer_harness="codex",
    rounds_low=1,
    rounds_high=3,
    verify="none",
    ship="handoff",
    skills=(),
)


def test_the_gate_applies_only_on_a_ci_base() -> None:
    raw = adapter(row("test", "success"))
    unreported = CIResult("pending", reported=False)
    gated = replace(FACTORY_CONFIG, ci_required_checks=GATE)
    scoped = replace(gated, ci_branches=("main",))

    assert pr_ci(raw, gated, "main").status == "pending"
    assert pr_ci(raw, scoped, "main").status == "pending"
    assert pr_ci(raw, scoped, "release/1.x") is raw
    assert pr_ci(unreported, scoped, "release/1.x").status == "not-run"
    assert pr_ci(unreported, gated, "main") is unreported
    assert pr_ci(raw, FACTORY_CONFIG, "main") is raw


GATED_FACTORY = FACTORY.format(tickets="github", ship="merge").replace(
    "skills: []\n---", "skills: []\nci:\n  required_checks: [ci-ok]\n---"
)


class ChecksGh(FakeGh):
    def __init__(self, factory: str, runs: list[dict]):
        super().__init__(factory)
        self.runs = runs
        self.issues = [{"number": 12, "pull_request": {}, "labels": [{"name": "orch:state/intake"}]}]

    def __call__(self, argv, timeout):
        if any(part.endswith("/check-runs?per_page=100") for part in argv):
            self.calls.append(list(argv))
            return GhResult(0, json.dumps({"total_count": len(self.runs), "check_runs": self.runs}), "")
        return super().__call__(argv, timeout)


def run(name: str, conclusion: str | None) -> dict:
    status = "queued" if conclusion is None else "completed"
    return {"name": name, "status": status, "conclusion": conclusion}


@pytest.mark.parametrize(
    ("runs", "without", "with_key"),
    [
        (
            [run("ci-ok", "success"), run("omr-service", "skipped"), run("test (3)", None)],
            "pending",
            "success",
        ),
        ([run("lint", "success"), run("test (1)", "success")], "success", "pending"),
        ([run("ci-ok", "skipped"), run("lint", "success")], "success", "pending"),
        ([run("ci-ok", "failure"), run("lint", "success")], "failure", "failure"),
    ],
    ids=["green-ci-ok-beside-a-queued-shard", "missing-ci-ok", "skipped-ci-ok", "red-ci-ok"],
)
def test_the_poller_gates_ticket_ci_on_the_required_checks(
    runs: list[dict], without: str, with_key: str
) -> None:
    def polled(factory: str) -> CIResult:
        gh = ChecksGh(factory, runs)
        [ticket] = LivePollSource(config(), gh_runner=gh).github_candidates("orch:state/")
        return ticket.ci[gh.head]

    assert polled(FACTORY.format(tickets="github", ship="merge")).status == without
    assert polled(GATED_FACTORY).status == with_key


def gated_board(tmp_path: Path, raw: dict[str, CIResult]):
    board = board_with(tmp_path, on=False)
    board.factory = replace(board.factory, ci_required_checks=GATE)
    snapshot = board.snapshot

    def gated(pr, *, fresh: bool = False):
        ticket = snapshot(pr, fresh=fresh)
        ci = {
            head: pr_ci(raw[head], ticket.factory, ticket.pr_base) if head in raw else result
            for head, result in ticket.ci.items()
        }
        return replace(ticket, ci=ci)

    board.snapshot = gated
    return board


GREEN_BESIDE_QUEUED = adapter(row("ci-ok", "success"), row("omr-service", "skipped"), row("test (3)"))


def test_t2_queues_review_on_a_green_ci_ok_while_another_job_is_still_queued(tmp_path: Path) -> None:
    assert GREEN_BESIDE_QUEUED.status == "pending"
    board = gated_board(tmp_path, {sha(1): GREEN_BESIDE_QUEUED})
    pr = built_with_ci_pending(board, sha(1))
    ready = board.ready(pr, pr.head)
    board.poll()

    assert board.outcome(ready) == "queued-review"
    assert [job["head"] for job in board.jobs("review")] == [sha(1)]


@pytest.mark.parametrize(
    "raw",
    [
        adapter(row("lint", "success")),
        adapter(row("ci-ok"), row("lint", "success")),
        adapter(row("ci-ok", "skipped"), row("lint", "success")),
        adapter(row("ci-ok", "neutral"), row("lint", "success")),
    ],
    ids=["missing", "queued", "skipped", "neutral"],
)
def test_t2_holds_a_ready_ci_pending_until_ci_ok_succeeds(tmp_path: Path, raw: CIResult) -> None:
    board = gated_board(tmp_path, {sha(1): raw})
    pr = built_with_ci_pending(board, sha(1))
    ready = board.ready(pr, pr.head)
    board.poll()

    assert board.outcome(ready) is None
    assert board.jobs("review") == []
    assert [wait["head"] for wait in ci_waits(board)] == [sha(1)]


def test_t2_queues_a_ci_fix_on_a_red_ci_ok(tmp_path: Path) -> None:
    board = gated_board(tmp_path, {sha(1): adapter(row("ci-ok", "failure"), row("lint", "success"))})
    pr = built_with_ci_pending(board, sha(1))
    ready = board.ready(pr, pr.head)
    board.poll()

    assert board.outcome(ready) == "queued-ci-fix"
    [fix] = [job for job in board.jobs("fix") if job["source_comment_id"] == ready]
    assert "ci-ok" in json.dumps(json.loads(fix["detail"]).get("ci_failure"))


def test_ship_proceeds_on_a_green_ci_ok_while_another_job_is_still_queued(tmp_path: Path) -> None:
    raw = {sha(1): CIResult("pending")}
    board = gated_board(tmp_path, raw)
    pr = built_with_ci_pending(board, sha(1))
    reviewed_at_pending_ci(board, pr, pr.head)
    raw[sha(1)] = GREEN_BESIDE_QUEUED
    approve = board.approve(pr)
    board.poll()

    assert board.outcome(approve) == "merge-queue"


def test_ship_waits_on_a_missing_ci_ok_even_when_every_other_job_is_green(tmp_path: Path) -> None:
    raw = {sha(1): CIResult("pending")}
    board = gated_board(tmp_path, raw)
    pr = built_with_ci_pending(board, sha(1))
    reviewed_at_pending_ci(board, pr, pr.head)
    raw[sha(1)] = adapter(row("lint", "success"), row("test (1)", "success"))
    approve = board.approve(pr)
    board.poll()

    assert board.outcome(approve) != "merge-queue"
    assert not pr.merged
