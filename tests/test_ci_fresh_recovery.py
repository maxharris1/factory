from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

from lib.dispatch.constants import CI_RERUN_REFUSED
from lib.dispatch.types import CIChange, CIResult, CheckRow, ci_fresh
from lib.github_actions import GhResult, GitHubReviewActions
from lib.poll_source import LivePollSource, RequiredGlobs
from tests.merge_queue_support import Board, sha


def gated_source(factory, raw, head, *, changes=None, github=None, base_sha=None):
    source = LivePollSource.__new__(LivePollSource)
    source.github = github
    source.required_globs = changes or RequiredGlobs()
    source.wall = lambda: 2000000000.0
    source._record = lambda *args: None
    pull = {"head": {"sha": head}, "base": {"ref": "main", **({"sha": base_sha} if base_sha else {})}}
    return source._gated_ci(
        "owner/repo", "42", "42", factory, pull, raw, {"changed_files": []}, fresh=True
    )


def merge_poll(board: Board, pr, result: CIResult) -> None:
    snapshot = board.snapshot
    board.snapshot = lambda p, fresh=False: replace(snapshot(p, fresh=fresh), ci={p.head: result})
    pr.mergeable, pr.mergeable_state = True, "clean"
    board.poll()


def check_run(job: int, run: str, minute: str) -> dict:
    return {
        "id": job,
        "name": "test",
        "status": "completed",
        "conclusion": "success",
        "started_at": f"2026-11-22T10:{minute}:00Z",
        "details_url": f"https://github.com/owner/repo/actions/runs/{run}/job/{job}",
    }


def test_a_same_sha_rerun_after_a_base_workflow_change_updates_the_head_instead_of_merging(
    tmp_path: Path,
) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=None)

    def runner(argv, timeout):
        endpoint = argv[-1]
        if "/check-runs?" in endpoint:
            rows = [check_run(11, "111", "00"), check_run(12, "111", "20")]
            rows = rows if "filter=all" in endpoint else rows[1:]
            result = {"total_count": len(rows), "check_runs": rows}
        elif endpoint.endswith("/status"):
            result = {"total_count": 0, "statuses": []}
        elif "/commits?" in endpoint:
            result = [{"sha": sha(9), "commit": {"committer": {"date": "2026-11-22T10:10:00Z"}}}]
        elif "/compare/" in endpoint:
            result = {"status": "diverged"}
        else:
            raise AssertionError(endpoint)
        return GhResult(0, json.dumps(result), "")

    github = GitHubReviewActions(runner)
    result = gated_source(board.factory, github.ci_status("owner/repo", pr.head), pr.head, github=github)

    assert result.pending == ("test (stale after base-workflow)",)
    merge_poll(board, pr, result)
    assert board.actions.merges == []
    [update] = board.jobs("update-from-main")
    assert "workflow" in json.loads(update["detail"])["instruction"]


def test_a_post_change_dispatch_on_the_old_branch_does_not_refresh_the_base_workflow(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "git"
    repo.mkdir()

    def git(*args, date="2026-11-22T09:00:00Z"):
        return subprocess.run(
            ["git", "-c", "user.name=Repro", "-c", "user.email=repro@example.invalid", *args],
            cwd=repo, check=False, capture_output=True, text=True,
            env={**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date},
        )

    def out(*args, **kwargs):
        done = git(*args, **kwargs)
        assert done.returncode == 0, done.stderr
        return done.stdout.strip()

    out("init", "-b", "main")
    workflow = repo / ".github/workflows/test.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("on: [pull_request, workflow_dispatch]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo old-workflow\n")
    out("add", ".")
    out("commit", "-m", "old workflow")
    out("checkout", "-b", "pr")
    (repo / "code.txt").write_text("PR code")
    out("add", ".")
    out("commit", "-m", "PR code", date="2026-11-22T09:30:00Z")
    old_head = out("rev-parse", "HEAD")
    out("checkout", "main")
    workflow.write_text(workflow.read_text().replace("old-workflow", "new-workflow"))
    out("add", ".")
    out("commit", "-m", "new workflow", date="2026-11-22T10:10:00Z")
    base_head = out("rev-parse", "HEAD")

    def runner(argv, timeout):
        route = next(arg for arg in argv if arg.startswith("repos/"))
        if "/check-runs?" in route:
            row = {**check_run(55, "222", "20"), "head_sha": old_head}
            result = {"total_count": 1, "check_runs": [row]}
        elif route.endswith("/status"):
            result = {"total_count": 0, "statuses": []}
        elif "/commits?" in route:
            result = [{"sha": base_head, "commit": {"committer": {"date": "2026-11-22T10:10:00Z"}}}]
        elif "/compare/" in route:
            commit, head = route.rpartition("/compare/")[2].split("...")
            contained = git("merge-base", "--is-ancestor", commit, head).returncode == 0
            result = {"status": "ahead" if contained else "diverged"}
        else:
            raise AssertionError(route)
        return subprocess.CompletedProcess(argv, 0, json.dumps(result), "")

    board = Board(tmp_path)
    pr = board.open("1", old_head)
    board.build_and_approve(pr, mergeable=None)
    adapter = GitHubReviewActions(runner=runner)
    gated = gated_source(
        board.factory, adapter.ci_status("owner/repo", old_head), old_head,
        github=adapter, base_sha=base_head,
    )

    assert gated.pending == ("test (stale after base-workflow)",)
    merge_poll(board, pr, gated)
    assert board.actions.merges == []
    assert len(board.jobs("update-from-main")) == 1

    out("checkout", "pr")
    out("merge", "main", "-m", "update from main", date="2026-11-22T10:30:00Z")
    updated = out("rev-parse", "HEAD")
    fresh = gated_source(
        board.factory, adapter.ci_status("owner/repo", updated), updated,
        github=adapter, base_sha=base_head,
    )
    assert fresh.status == "success"


class RecoveryBoard(Board):
    def __init__(self, path):
        super().__init__(path)
        self.result = None
        self.calls = []
        self.factory = replace(self.factory, ci_required_checks=("test",))
        self.dispatcher.ci_full_rerun = lambda repo, ids: self.calls.append(tuple(ids)) or "requested"

    def snapshot(self, pr, *, fresh=False):
        ticket = super().snapshot(pr, fresh=fresh)
        return ticket if self.result is None else replace(ticket, ci={pr.head: self.result})

    def flags(self) -> int:
        with self.queue.connect() as conn:
            return conn.execute(
                "SELECT count(*) FROM needs_attention WHERE reason = ?", (CI_RERUN_REFUSED,)
            ).fetchone()[0]


def test_a_stale_green_ready_from_a_finished_build_requests_one_fresh_run(tmp_path: Path) -> None:
    board = RecoveryBoard(tmp_path)
    pr = board.open("42", sha(1))
    board.go(pr, "go")
    board.poll()
    raw = CIResult("success", checks=(CheckRow("test", "completed", "success", 10, run_id="777"),))
    board.result = ci_fresh(raw, (CIChange("required-set", 20, ("test",)),), ("test",))
    board.poll()

    assert board.calls == []

    board.finish("claude")
    board.ready(pr, pr.head)
    for _ in range(3):
        board.poll()

    assert board.calls == [("777",)]


def test_a_newly_required_check_missing_from_a_green_head_is_flagged_refused(tmp_path: Path) -> None:
    board = RecoveryBoard(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=None)
    board.factory = replace(board.factory, ci_required_checks=("test", "security"))
    changes = RequiredGlobs()
    changes.observe("owner/repo", "main", ("test",), 10)
    raw = CIResult("success", checks=(CheckRow("test", "completed", "success", 10, run_id="777"),))
    board.result = gated_source(board.factory, raw, sha(1), changes=changes)
    for _ in range(3):
        board.poll()

    assert board.result.pending == ("security (not reported)",)
    assert board.calls == []
    assert board.flags() == 1
    assert board.actions.merges == []


def test_a_check_not_yet_reported_on_a_head_whose_runs_started_after_the_change_stays_pending() -> None:
    change = CIChange("required-set", 20, ("security",))
    running = CIResult("pending", pending=("security (not reported)",), checks=(
        CheckRow("test", "completed", "success", 30, run_id="777"),
    ))

    assert ci_fresh(running, (change,), ("test", "security")).stale == ()


def test_a_pull_request_run_on_the_old_base_cannot_clear_a_later_pushed_workflow_commit(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "git"
    repo.mkdir()

    def git(*args: str, date: str = "2026-11-22T09:00:00Z") -> str:
        return subprocess.check_output(
            ["git", "-c", "user.name=T", "-c", "user.email=t@example.invalid", *args],
            cwd=repo,
            text=True,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date},
        ).strip()

    workflow = repo / ".github/workflows/test.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("old-workflow\n")
    git("init", "-b", "main")
    git("add", ".")
    git("commit", "-m", "old workflow")
    old_base = git("rev-parse", "HEAD")
    git("checkout", "-b", "pr")
    (repo / "code").write_text("PR code")
    git("add", ".")
    git("commit", "-m", "PR", date="2026-11-22T09:30:00Z")
    head = git("rev-parse", "HEAD")
    git("checkout", "-b", "local-workflow", old_base)
    workflow.write_text("new-workflow\n")
    git("add", ".")
    git("commit", "-m", "local workflow change", date="2026-11-22T10:10:00Z")
    changed = git("rev-parse", "HEAD")
    git("checkout", "main")
    board = Board(tmp_path)
    pr = board.open("42", head)
    board.build_and_approve(pr, mergeable=None)
    git("merge", "--ff-only", "local-workflow")

    def runner(argv, timeout):
        endpoint = next(arg for arg in argv if arg.startswith("repos/"))
        if "/check-runs?" in endpoint:
            result = {"total_count": 1, "check_runs": [check_run(55, "222", "20")]}
        elif endpoint.endswith("/status"):
            result = {"total_count": 0, "statuses": []}
        elif "/commits?" in endpoint:
            result = [{"sha": changed, "commit": {"committer": {"date": "2026-11-22T10:10:00Z"}}}]
        elif "/compare/" in endpoint:
            commit, target = endpoint.rsplit("/", 1)[1].split("...")
            ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", commit, target], cwd=repo)
            result = {"status": "ahead" if ancestor.returncode == 0 else "diverged"}
        else:
            raise AssertionError(endpoint)
        return GhResult(0, json.dumps(result), "")

    github = GitHubReviewActions(runner)
    result = gated_source(board.factory, github.ci_status("owner/repo", head), head, github=github)

    assert result.pending == ("test (stale after base-workflow)",)
    merge_poll(board, pr, result)
    assert board.actions.merges == []
    assert len(board.jobs("update-from-main")) == 1


def test_delayed_push_across_two_paths(tmp_path: Path) -> None:
    repo = tmp_path / "git"
    repo.mkdir()

    def git(*args: str, date: str = "2026-11-22T09:00:00Z") -> str:
        return subprocess.check_output(
            ["git", "-c", "user.name=T", "-c", "user.email=t@example.invalid", *args],
            cwd=repo,
            text=True,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date},
        ).strip()

    workflow = repo / ".github/workflows/test.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("old-workflow\n")
    (repo / "ci").mkdir()
    (repo / "ci/test.sh").write_text("old-ci\n")
    git("init", "-b", "main")
    git("add", ".")
    git("commit", "-m", "base")
    git("checkout", "-b", "local-ci")
    (repo / "ci/test.sh").write_text("new-ci\n")
    git("commit", "-am", "C2 ci change", date="2026-11-22T10:10:00Z")
    git("checkout", "main")
    workflow.write_text("new-workflow\n")
    git("commit", "-am", "C1 workflow change", date="2026-11-22T10:25:00Z")
    git("checkout", "-b", "pr")
    (repo / "code").write_text("PR code")
    git("add", ".")
    git("commit", "-m", "PR", date="2026-11-22T10:26:00Z")
    head = git("rev-parse", "HEAD")
    git("checkout", "main")
    board = Board(tmp_path)
    board.factory = replace(
        board.factory, ci_force_rerun_paths=(".github/workflows/**", "ci/**")
    )
    pr = board.open("42", head)
    board.build_and_approve(pr, mergeable=None)
    git("merge", "--no-ff", "-m", "integrate C2", "local-ci", date="2026-11-22T10:30:00Z")

    def runner(argv, timeout):
        endpoint = next(arg for arg in argv if arg.startswith("repos/"))
        if "/check-runs?" in endpoint:
            result = {"total_count": 1, "check_runs": [check_run(55, "222", "27")]}
        elif endpoint.endswith("/status"):
            result = {"total_count": 0, "statuses": []}
        elif "/commits?" in endpoint:
            path = endpoint.split("&path=")[1].split("&")[0]
            commit, date = git("log", "-1", "--format=%H %cI", "main", "--", path).split()
            result = [{"sha": commit, "commit": {"committer": {"date": date}}}]
        elif "/compare/" in endpoint:
            commit, target = endpoint.rsplit("/", 1)[1].split("...")
            ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", commit, target], cwd=repo)
            result = {"status": "ahead" if ancestor.returncode == 0 else "diverged"}
        else:
            raise AssertionError(endpoint)
        return GhResult(0, json.dumps(result), "")

    github = GitHubReviewActions(runner)
    result = gated_source(board.factory, github.ci_status("owner/repo", head), head, github=github)

    assert result.pending == ("test (stale after base-workflow)",)
    merge_poll(board, pr, result)
    assert board.actions.merges == []
    assert len(board.jobs("update-from-main")) == 1
