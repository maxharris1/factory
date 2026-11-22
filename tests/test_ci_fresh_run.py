from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatch.constants import CI_RERUN_REFUSED
from lib.dispatch.types import CheckRow, CIChange, CIResult, TicketRef, ci_fresh
from lib.github_actions import GitHubAdapterError, GitHubReviewActions, GhResult
from lib.poll_source import LivePollSource
from tests.merge_queue_support import Board, sha
from tests.test_ci_fresh import AFTER, BEFORE, CHANGE, FreshGh, factory_md, iso, run, serve
from tests.test_live_poll import config

RUN = "777"
RUN_PATH = f"repos/owner/repo/actions/runs/{RUN}"
ADDED = CIChange("required-set", CHANGE, ("ci-ok",))
BASE = CIChange("base-workflow", CHANGE)


def actions_row(name: str, started_at: float, *, run_id: str | None = RUN) -> CheckRow:
    return CheckRow(
        name=name,
        status="completed",
        conclusion="success",
        started_at=started_at,
        run_id=run_id,
        job_id=None if run_id is None else "1",
    )


def gated(*rows: CheckRow, change: CIChange = ADDED) -> CIResult:
    return ci_fresh(CIResult("success", checks=rows), (change,), ("ci-ok",))


def test_no_run_on_a_head_lacking_a_base_workflow_commit_refreshes_it() -> None:
    for row in (
        actions_row("ci-ok", BEFORE),
        actions_row("ci-ok", AFTER),
        actions_row("ci-ok", AFTER, run_id=None),
    ):
        held = gated(row, change=BASE)
        assert held.status == "pending" and held.stale == (BASE,)


def test_a_same_sha_rerun_refreshes_a_head_after_a_required_set_change() -> None:
    assert gated(actions_row("ci-ok", AFTER)).clears
    assert gated(actions_row("ci-ok", BEFORE)).stale == (ADDED,)


def adapter(posts: list[str], *, status: str = "completed", post: GhResult = GhResult(0, "", "")):
    def gh(argv, timeout: float) -> GhResult:
        endpoint = argv[-1]
        if "POST" in argv:
            posts.append(endpoint)
            return post
        return GhResult(0, json.dumps({"id": int(RUN), "status": status}), "")

    return GitHubReviewActions(gh)


def test_the_adapter_reruns_the_whole_workflow_run_not_its_failed_jobs() -> None:
    posts: list[str] = []

    assert adapter(posts).rerun_workflow_runs("owner/repo", [RUN]) == "requested"
    assert posts == [f"{RUN_PATH}/rerun"]


def test_the_adapter_posts_no_full_rerun_while_the_run_is_in_progress() -> None:
    posts: list[str] = []

    assert adapter(posts, status="in_progress").rerun_workflow_runs("owner/repo", [RUN]) == "running"
    assert posts == []


def test_the_adapter_raises_a_refused_full_rerun_of_a_completed_run() -> None:
    forbidden = GhResult(1, "", 'gh: Resource not accessible by integration (HTTP 403)\n{"message":"Resource not accessible by integration"}')

    with pytest.raises(GitHubAdapterError):
        adapter([], post=forbidden).rerun_workflow_runs("owner/repo", [RUN])


@pytest.mark.parametrize(
    ("paths", "query"),
    [
        ((".github/workflows/**", ".github/workflows/ci.yml"), "&path=.github/workflows&path=.github/workflows/ci.yml"),
        (("**/*.yml",), ""),
    ],
)
def test_the_adapter_reads_each_glob_by_its_literal_directory_or_the_whole_branch(
    paths: tuple[str, ...], query: str
) -> None:
    reads: list[str] = []

    def gh(argv, timeout: float) -> GhResult:
        reads.append(argv[-1])
        commit = {"sha": sha(len(reads)), "commit": {"committer": {"date": iso(CHANGE + len(reads))}}}
        return GhResult(0, json.dumps([commit]), "")

    changed = GitHubReviewActions(gh).base_workflow_commit("owner/repo", "main", paths)

    assert "".join(read.split("?sha=main")[1].removesuffix("&per_page=1") for read in reads) == query
    assert changed == (sha(len(reads)), CHANGE + len(reads))


@pytest.mark.parametrize(
    ("status", "contains"), [("ahead", True), ("identical", True), ("behind", False), ("diverged", False)]
)
def test_the_adapter_reads_whether_a_head_contains_a_base_commit(status: str, contains: bool) -> None:
    reads: list[str] = []

    def gh(argv, timeout: float) -> GhResult:
        reads.append(argv[-1])
        return GhResult(0, json.dumps({"status": status}), "")

    assert GitHubReviewActions(gh).contains_commit("owner/repo", sha(1), sha(9)) is contains
    assert reads == [f"repos/owner/repo/compare/{sha(9)}...{sha(1)}"]


def actions_run(name: str, started_at: float) -> dict:
    url = f"https://github.com/owner/repo/actions/runs/{RUN}/job/1"
    return {**run(name, started_at), "details_url": url}


class BaseGh(FreshGh):
    def __init__(
        self,
        runs: list[dict],
        changed: float | None,
        ci: str = "",
        *,
        compare: str = "behind",
    ):
        super().__init__(factory_md(ci), runs, ["lib/x.py"], 0)
        self.changed = changed
        self.compare = compare

    def __call__(self, argv, timeout):
        endpoint = next((part for part in argv if part.startswith("repos/")), "")
        if "/commits?sha=" in endpoint:
            self.calls.append(list(argv))
            commits = [] if self.changed is None else [
                {"sha": sha(9), "commit": {"committer": {"date": iso(self.changed)}}}
            ]
            return GhResult(0, json.dumps(commits), "")
        if "/compare/" in endpoint:
            self.calls.append(list(argv))
            return GhResult(0, json.dumps({"status": self.compare}), "")
        return super().__call__(argv, timeout)


def read(gh: BaseGh, *, fresh: bool) -> CIResult:
    source = LivePollSource(config(), gh_runner=gh, wall=lambda: AFTER + 60)
    [ticket] = source.github_candidates("orch:state/")
    if fresh:
        ticket = source.fresh_ticket(TicketRef(ticket.repo, ticket.ticket_id, "github"))
    return ticket.ci[gh.head]


def test_a_merge_read_holds_a_green_head_whose_runs_predate_a_base_workflow_commit() -> None:
    gh = BaseGh([actions_run("test", BEFORE)], CHANGE)

    ci = read(gh, fresh=True)

    assert ci.status == "pending"
    assert [change.kind for change in ci.stale] == ["base-workflow"]
    assert gh.read("/commits?sha=main&path=.github/workflows&per_page=1")
    assert gh.read(f"/compare/{sha(9)}...{gh.head}")


def test_a_merge_read_holds_a_head_lacking_the_base_commit_whatever_its_runs_started() -> None:
    for gh in (BaseGh([actions_run("test", AFTER)], CHANGE), BaseGh([run("test", AFTER)], CHANGE)):
        assert [change.kind for change in read(gh, fresh=True).stale] == ["base-workflow"]


def test_a_merge_read_clears_a_head_containing_the_base_commit() -> None:
    assert read(BaseGh([run("test", BEFORE)], CHANGE, compare="ahead"), fresh=True).clears
    assert read(BaseGh([run("test", BEFORE)], None), fresh=True).clears


def test_an_ordinary_poll_and_an_empty_force_rerun_paths_read_no_base_commits() -> None:
    polled = BaseGh([run("test", BEFORE)], CHANGE)
    off = BaseGh([run("test", BEFORE)], CHANGE, "  force_rerun_paths: []\n")

    assert read(polled, fresh=False).clears
    assert read(off, fresh=True).clears
    assert not polled.read("/commits?sha=") and not off.read("/commits?sha=")


class FreshRunBoard(Board):
    def __init__(self, tmp_path: Path):
        super().__init__(tmp_path)
        self.raw: dict[str, CIResult] = {}
        self.full_reruns: list[list[str]] = []
        self.failed_reruns: list[list[str]] = []
        self.refusal: Exception | None = None
        self.dispatcher.ci_full_rerun = self.full_rerun
        self.dispatcher.ci_rerun = lambda repo, run_ids: self.failed_reruns.append(list(run_ids))
        serve(self, self.raw)
        self.factory = replace(self.factory, ci_required_checks=("ci-ok",))

    def full_rerun(self, repo: str, run_ids):
        self.full_reruns.append(list(run_ids))
        if self.refusal is not None:
            raise self.refusal
        return "requested"

    def fresh_runs(self) -> list[tuple[str, str]]:
        with self.queue.connect() as connection:
            return [
                (row["head"], row["outcome"])
                for row in connection.execute("SELECT head, outcome FROM ci_fresh_runs")
            ]

    def attention(self) -> list[str]:
        with self.queue.connect() as connection:
            return [
                row["detail"]
                for row in connection.execute(
                    "SELECT detail FROM needs_attention WHERE reason = ?", (CI_RERUN_REFUSED,)
                )
            ]


def queued(tmp_path: Path) -> tuple[FreshRunBoard, object]:
    board = FreshRunBoard(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=None)
    pr.mergeable, pr.mergeable_state = True, "clean"
    return board, pr


def test_a_stale_required_set_green_in_the_merge_queue_gets_one_full_rerun_then_merges(
    tmp_path: Path,
) -> None:
    board, pr = queued(tmp_path)
    board.raw[sha(1)] = gated(actions_row("ci-ok", BEFORE))
    board.poll()
    board.poll()

    assert board.actions.merges == []
    assert board.full_reruns == [[RUN]]
    assert board.failed_reruns == []
    assert board.fresh_runs() == [(sha(1), "requested")]
    assert board.state("42")["workflow_state"] == "merge-queue"

    board.raw[sha(1)] = gated(actions_row("ci-ok", AFTER))
    board.poll()

    assert board.actions.merges == [(True, sha(1))]
    assert board.full_reruns == [[RUN]]


@pytest.mark.parametrize("cause", ["refused", "no-run"])
def test_a_refused_fresh_run_never_clears_on_the_stale_green_and_flags_max(
    tmp_path: Path, cause: str
) -> None:
    board, pr = queued(tmp_path)
    if cause == "refused":
        board.refusal = GitHubAdapterError("rerun refused (HTTP 403)")
        board.raw[sha(1)] = gated(actions_row("ci-ok", BEFORE))
    else:
        board.raw[sha(1)] = gated(actions_row("ci-ok", BEFORE, run_id=None))
    board.poll()
    board.poll()
    board.poll()

    assert board.actions.merges == []
    assert board.fresh_runs() == [(sha(1), "refused")]
    assert board.full_reruns == ([[RUN]] if cause == "refused" else [])
    assert board.failed_reruns == []
    [flag] = board.attention()
    assert "stale after required-set" in flag
    assert len(board.wakes(CI_RERUN_REFUSED)) == 1


def test_an_approved_queued_head_behind_a_base_workflow_change_updates_from_base_before_it_merges(
    tmp_path: Path,
) -> None:
    board, pr = queued(tmp_path)
    board.raw[sha(1)] = gated(actions_row("ci-ok", AFTER), change=BASE)
    board.poll()
    board.poll()

    assert board.actions.merges == []
    assert board.full_reruns == [] and board.failed_reruns == []
    [update] = board.jobs("update-from-main")
    assert "workflow" in json.loads(update["detail"])["instruction"]
    assert board.state("42")["workflow_state"] == "updating"

    board.finish("claude")
    pr.push(sha(2))
    board.raw[sha(2)] = gated(actions_row("ci-ok", AFTER))
    ready = board.ready(pr, sha(2))
    board.poll()

    assert board.outcome(ready) == f"approval-carried:{sha(1)[:12]}->{sha(2)[:12]}"
    assert board.actions.merges == [(True, sha(2))]
