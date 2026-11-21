from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lib.dispatch.types import CheckRow, CIChange, CIResult, FailedCheck, ci_fresh
from lib.github_actions import GhResult
from lib.poll_source import LivePollSource, RequiredGlobs
from tests.merge_queue_support import Board, sha
from tests.test_live_poll import FACTORY, FakeGh, config
from tests.test_ready_ci_gate import ci_waits
from tests.test_review_before_ci import board_with, built_with_ci_pending, reviewed_at_pending_ci

CHANGE = 1_000.0
BEFORE = CHANGE - 60
AFTER = CHANGE + 60


def row(name: str, started_at: float | None, conclusion: str = "success") -> CheckRow:
    return CheckRow(name=name, status="completed", conclusion=conclusion, started_at=started_at)


def green(*rows: CheckRow) -> CIResult:
    return CIResult("success", checks=rows)


WORKFLOW = CIChange("head-workflow", CHANGE)


def test_a_green_head_whose_runs_predate_a_workflow_change_is_pending() -> None:
    fresh = ci_fresh(green(row("test", BEFORE), row("lint", AFTER)), (WORKFLOW,), ())

    assert fresh.status == "pending"
    assert not fresh.clears
    assert fresh.reported
    assert fresh.pending == ("test (stale after head-workflow)",)


def test_a_green_head_whose_runs_all_started_after_the_change_is_untouched() -> None:
    result = green(row("test", AFTER), row("lint", AFTER))

    assert ci_fresh(result, (WORKFLOW,), ()) is result


def test_an_old_green_run_beside_a_new_green_run_of_the_same_check_clears() -> None:
    result = green(row("test", BEFORE), row("test", AFTER))

    assert ci_fresh(result, (WORKFLOW,), ()).clears


def test_a_run_with_no_start_time_is_stale() -> None:
    assert ci_fresh(green(row("test", None)), (WORKFLOW,), ()).status == "pending"


def test_with_required_checks_only_the_gate_globs_must_be_fresh() -> None:
    result = green(row("ci-ok", AFTER), row("lint", BEFORE))
    stale = green(row("ci-ok", BEFORE), row("lint", AFTER))

    assert ci_fresh(result, (WORKFLOW,), ("ci-ok",)) is result
    assert ci_fresh(stale, (WORKFLOW,), ("ci-ok",)).pending == ("ci-ok (stale after head-workflow)",)


def test_a_fresh_skipped_row_does_not_refresh_a_stale_success() -> None:
    result = green(row("test (1)", BEFORE), row("test (2)", AFTER, "skipped"))

    assert ci_fresh(result, (WORKFLOW,), ("test (*)",)).status == "pending"


def test_a_required_glob_added_after_its_success_started_is_pending() -> None:
    added = CIChange("required-set", CHANGE, ("ci-ok",))
    result = green(row("ci-ok", BEFORE), row("lint", BEFORE))

    fresh = ci_fresh(result, (added,), ("lint", "ci-ok"))

    assert fresh.status == "pending"
    assert fresh.pending == ("ci-ok (stale after required-set)",)
    assert ci_fresh(green(row("ci-ok", AFTER), row("lint", BEFORE)), (added,), ("lint", "ci-ok")).clears


@pytest.mark.parametrize(
    "result",
    [
        CIResult("failure", "failed: test", failed=(FailedCheck("test"),), checks=(row("test", BEFORE, "failure"),)),
        CIResult("pending", pending=("test",), checks=(row("test", BEFORE),)),
        CIResult("not-run", reported=False),
    ],
    ids=["red", "pending", "not-run"],
)
def test_a_head_that_is_not_green_passes_through(result: CIResult) -> None:
    changes = (WORKFLOW, CIChange("required-set", CHANGE, ("test",)))

    assert ci_fresh(result, changes, ("test",)) is result


def test_no_change_leaves_a_green_head_untouched() -> None:
    result = green(row("test", BEFORE))

    assert ci_fresh(result, (), ()) is result


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(name: str, started_at: float) -> dict:
    return {"name": name, "status": "completed", "conclusion": "success", "started_at": iso(started_at)}


class FreshGh(FakeGh):
    def __init__(self, factory: str, runs: list[dict], files: list[str], committed: float):
        super().__init__(factory)
        self.runs = runs
        self.files = [{"filename": path} for path in files]
        self.committed = committed
        self.issues = [{"number": 12, "pull_request": {}, "labels": [{"name": "orch:state/intake"}]}]

    def __call__(self, argv, timeout):
        endpoint = next((part for part in argv if part.startswith("repos/")), "")
        if endpoint.endswith("/check-runs?per_page=100"):
            self.calls.append(list(argv))
            return GhResult(0, json.dumps({"total_count": len(self.runs), "check_runs": self.runs}), "")
        if endpoint.endswith(f"/commits/{self.head}"):
            self.calls.append(list(argv))
            value = {"sha": self.head, "commit": {"committer": {"date": iso(self.committed)}}}
            return GhResult(0, json.dumps(value), "")
        return super().__call__(argv, timeout)

    def read(self, endpoint: str) -> bool:
        return any(part.endswith(endpoint) for call in self.calls for part in call)


def factory_md(ci: str = "") -> str:
    text = FACTORY.format(tickets="github", ship="merge")
    return text if not ci else text.replace("skills: []\n---", f"skills: []\nci:\n{ci}---")


def polled(gh: FreshGh, *, globs: RequiredGlobs | None = None, now: float = AFTER + 60) -> CIResult:
    source = LivePollSource(config(), gh_runner=gh, required_globs=globs, wall=lambda: now)
    [ticket] = source.github_candidates("orch:state/")
    return ticket.ci[gh.head]


WORKFLOW_DIFF = [".github/workflows/ci.yml", "lib/x.py"]


def test_the_poller_holds_a_green_head_touching_a_workflow_with_runs_from_before_its_commit() -> None:
    gh = FreshGh(factory_md(), [run("test", BEFORE)], WORKFLOW_DIFF, CHANGE)

    ci = polled(gh)

    assert ci.status == "pending"
    assert not ci.clears
    assert ci.pending == ("test (stale after head-workflow)",)


def test_the_poller_clears_a_workflow_head_whose_runs_started_after_its_commit() -> None:
    gh = FreshGh(factory_md(), [run("test", AFTER)], WORKFLOW_DIFF, CHANGE)

    assert polled(gh).status == "success"


def test_force_rerun_paths_names_the_paths_that_trigger() -> None:
    custom = factory_md("  force_rerun_paths: [ci/**]\n")

    assert polled(FreshGh(custom, [run("test", BEFORE)], ["ci/run.sh"], CHANGE)).status == "pending"
    assert polled(FreshGh(custom, [run("test", BEFORE)], WORKFLOW_DIFF, CHANGE)).status == "success"


def test_a_head_off_the_rerun_paths_keeps_todays_green_and_reads_no_commit() -> None:
    gh = FreshGh(factory_md(), [run("test", BEFORE)], ["lib/x.py"], CHANGE)

    assert polled(gh).status == "success"
    assert not gh.read(f"/commits/{gh.head}")


def test_an_empty_force_rerun_paths_turns_off_the_path_trigger() -> None:
    gh = FreshGh(factory_md("  force_rerun_paths: []\n"), [run("test", BEFORE)], WORKFLOW_DIFF, CHANGE)

    assert polled(gh).status == "success"
    assert not gh.read("/files?per_page=100")


def required(*globs: str, rerun: str = "") -> str:
    return factory_md(f"  required_checks: [{', '.join(globs)}]\n{rerun}")


def test_a_required_check_added_after_the_green_runs_started_holds_the_head(tmp_path: Path) -> None:
    globs = RequiredGlobs(tmp_path / "orch.db")
    runs = [run("ci-ok", BEFORE), run("lint", BEFORE)]
    assert polled(FreshGh(required("lint"), runs, ["lib/x.py"], 0), globs=globs, now=BEFORE).clears

    ci = polled(FreshGh(required("lint", "ci-ok"), runs, ["lib/x.py"], 0), globs=globs, now=CHANGE)

    assert ci.status == "pending"
    assert ci.pending == ("ci-ok (stale after required-set)",)
    rerun = [run("ci-ok", AFTER), run("lint", BEFORE)]
    assert polled(FreshGh(required("lint", "ci-ok"), rerun, ["lib/x.py"], 0), globs=globs).clears


def test_an_empty_force_rerun_paths_keeps_the_required_set_trigger(tmp_path: Path) -> None:
    globs = RequiredGlobs(tmp_path / "orch.db")
    off = "  force_rerun_paths: []\n"
    runs = [run("ci-ok", BEFORE), run("lint", BEFORE)]
    polled(FreshGh(required("lint", rerun=off), runs, [], 0), globs=globs, now=BEFORE)

    ci = polled(FreshGh(required("lint", "ci-ok", rerun=off), runs, [], 0), globs=globs, now=CHANGE)

    assert ci.status == "pending"


def test_the_first_observed_required_set_is_a_baseline_and_holds_nothing(tmp_path: Path) -> None:
    gh = FreshGh(required("ci-ok"), [run("ci-ok", BEFORE)], [], 0)

    assert polled(gh, globs=RequiredGlobs(tmp_path / "orch.db"), now=AFTER).clears


def test_the_required_set_store_survives_a_restart_and_dates_only_additions(tmp_path: Path) -> None:
    path = tmp_path / "orch.db"
    assert RequiredGlobs(path).observe("o/r", "main", ("lint",), BEFORE) == {"lint": None}
    assert RequiredGlobs(path).observe("O/R", "main", ("lint", "ci-ok"), CHANGE) == {
        "lint": None,
        "ci-ok": CHANGE,
    }
    assert RequiredGlobs(path).observe("o/r", "main", ("ci-ok",), AFTER) == {"ci-ok": CHANGE}
    assert RequiredGlobs(path).observe("o/r", "main", ("ci-ok", "lint"), AFTER) == {
        "ci-ok": CHANGE,
        "lint": AFTER,
    }
    assert RequiredGlobs(path).observe("o/r", "release", ("lint",), AFTER) == {"lint": None}


STALE = ci_fresh(CIResult("success", checks=(row("test", BEFORE),)), (WORKFLOW,), ())


def serve(board, raw: dict[str, CIResult]):
    snapshot = board.snapshot

    def staled(pr, *, fresh: bool = False):
        ticket = snapshot(pr, fresh=fresh)
        return replace(ticket, ci={head: raw.get(head, ci) for head, ci in ticket.ci.items()})

    board.snapshot = staled
    return board


def stale_board(tmp_path: Path, raw: dict[str, CIResult], *, on: bool = False):
    return serve(board_with(tmp_path, on=on), raw)


def test_t2_holds_a_stale_green_ready_ci_pending(tmp_path: Path) -> None:
    board = stale_board(tmp_path, {sha(1): STALE})
    pr = built_with_ci_pending(board, sha(1))
    ready = board.ready(pr, pr.head)
    board.poll()

    assert board.outcome(ready) is None
    assert board.jobs("review") == []
    assert [wait["head"] for wait in ci_waits(board)] == [sha(1)]


def test_ship_does_not_merge_a_stale_green_head(tmp_path: Path) -> None:
    raw = {sha(1): CIResult("pending")}
    board = stale_board(tmp_path, raw, on=True)
    pr = built_with_ci_pending(board, sha(1))
    reviewed_at_pending_ci(board, pr, pr.head)
    raw[sha(1)] = STALE
    approve = board.approve(pr)
    board.poll()

    assert board.outcome(approve) != "merge-queue"
    assert not pr.merged


class UnreadDiffGh(FreshGh):
    def __call__(self, argv, timeout):
        if any(part.endswith("/files?per_page=100") for part in argv):
            self.calls.append(list(argv))
            return GhResult(1, "", "failed (HTTP 502)")
        return super().__call__(argv, timeout)


def test_an_unread_diff_holds_a_green_head_pending_and_keeps_the_ticket() -> None:
    errors: list[str] = []
    gh = UnreadDiffGh(factory_md(), [run("test", AFTER)], [], CHANGE)
    source = LivePollSource(
        config(), gh_runner=gh, wall=lambda: AFTER, error_sink=lambda repo, ticket, reason: errors.append(ticket)
    )

    [ticket] = source.github_candidates("orch:state/")

    assert ticket.ci[gh.head].status == "pending"
    assert ticket.ci[gh.head].pending == ("CI freshness (unread)",)
    assert errors == ["12"]


def test_the_merge_queue_does_not_merge_a_stale_green_head_until_a_fresh_run_clears(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=None)
    raw = {sha(1): STALE}
    serve(board, raw)

    pr.mergeable, pr.mergeable_state = True, "clean"
    pr.ci[sha(1)] = "success"
    board.poll()
    assert board.actions.merges == []
    assert board.state("42")["workflow_state"] == "merge-queue"

    raw[sha(1)] = ci_fresh(CIResult("success", checks=(row("test", AFTER),)), (WORKFLOW,), ())
    board.poll()
    assert board.actions.merges == [(True, sha(1))]
