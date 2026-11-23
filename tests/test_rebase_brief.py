"""The rebase brief rebases onto the project base and pushes with a lease, never a merge (#1415)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from lib.brief import brief_kind, render_brief
from lib.factory_md import parse_factory_context
from lib.queue import Job
from tests.regenerate_brief_snapshots import BOX_FACTORY_ROOT, FACTORY_MD, HEAD, REMOTE_DEVICE, _job
from tests.test_brief import contradictions_in

BRANCH = "factory/issue-42"


def rebase_job(base: str, branch: str | None = BRANCH) -> Job:
    return _job("fix", "claude", 1, HEAD, {"kind": "rebase", "base_branch": base, "gitBranchName": branch})


def render(job: Job, place: str) -> str:
    if place == "remote":
        return render_brief(
            job,
            bot="claude-bot",
            context=None,
            context_note="no factory.md on dev",
            remote_device=REMOTE_DEVICE,
            factory_root="/home/claude-bot/factory",
        )
    context, findings = parse_factory_context(FACTORY_MD)
    assert context is not None, findings
    return render_brief(job, bot="claude-bot", context=context, context_ref="dev", factory_root=BOX_FACTORY_ROOT)


def section(text: str, title: str) -> str:
    return text.split(f"## {title}\n", 1)[1].split("\n## ", 1)[0]


@pytest.fixture(params=["main", "dev"])
def base(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture(params=["box", "remote"])
def text(request: pytest.FixtureRequest, base: str) -> str:
    return render(rebase_job(base), request.param)


@pytest.fixture
def method(text: str) -> str:
    return section(text, "Method")


def test_brief_kind_is_rebase(text: str) -> None:
    assert brief_kind(rebase_job("main")) == "rebase"
    assert brief_kind(_job("fix", "claude", 1, HEAD, {"kind": "merge-only"})) == "merge-only"
    assert text.startswith("# Factory job brief: rebase\n")


def test_fetches_and_rebases_onto_the_job_base(text: str, method: str, base: str) -> None:
    assert f"`git fetch origin {base}`" in method
    assert f"`git rebase origin/{base}`" in method
    assert set(re.findall(r"origin/([\w/-]+)", text)) == {base}


def test_never_rebase_merges_never_merges(text: str, method: str) -> None:
    assert method.count("--rebase-merges") == 1
    assert "without `--rebase-merges`" in method
    assert len(re.findall(r"git merge\b", text)) == method.count("Never run `git merge`") == 1


def test_regenerates_supabase_types_on_conflict(method: str) -> None:
    assert (
        "If a generated Supabase types file conflicts, take the base side, regenerate it with the "
        "project's documented types command, and commit the result. If that command cannot run, "
        "the conflict counts as one you cannot resolve."
    ) in method


def test_checks_for_no_merge_commits(method: str, base: str) -> None:
    assert f"`git rev-list --merges origin/{base}..HEAD` prints nothing" in method


def test_pushes_with_a_lease_on_the_branch_and_head(text: str, method: str) -> None:
    assert f"`git push --force-with-lease={BRANCH}:{HEAD} origin HEAD:{BRANCH}`" in method
    assert "Never push with a bare `--force`." in method
    assert re.search(r"push --force(?!-with-lease)", text) is None
    assert re.search(r"push\b[^\n`]* -f\b", text) is None


@pytest.mark.parametrize("branch", [None, "not a branch"])
def test_an_unknown_branch_prints_a_placeholder(branch: str | None) -> None:
    method = section(render(rebase_job("main", branch), "box"), "Method")
    assert f"`git push --force-with-lease=<pr-branch>:{HEAD} origin HEAD:<pr-branch>`" in method
    assert "Check out `<pr-branch>`" in method


def test_unresolvable_conflict_aborts_and_asks(text: str, method: str) -> None:
    (paragraph,) = [line for line in method.splitlines() if "git rebase --abort" in line]
    assert "wall-clock budget" in paragraph
    steps = [
        "`git diff --name-only --diff-filter=U`",
        f"`git rebase --abort 2>/dev/null; git reset --hard {HEAD}`",
        f"`git rev-parse HEAD` prints `{HEAD}`",
        "push nothing",
        "`questions` handoff that names each file",
    ]
    positions = [paragraph.index(step) for step in steps]
    assert positions == sorted(positions)
    assert "Post only this job's handoff, or a `questions` handoff." in text


def test_wall_clock_line_pushes_nothing(text: str) -> None:
    assert "Near it, take the conflict steps above: push nothing and post `questions`." in section(text, "Job")
    assert "push what you have" not in text


def test_no_contradiction_in_the_rebase_brief(text: str) -> None:
    assert contradictions_in(text) == []
    assert "no rebase, no force-push" not in text


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def commit(repo: Path, name: str, text: str) -> str:
    (repo / name).write_text(text)
    git(repo, "add", name)
    assert git(repo, "commit", "-q", "-m", name).returncode == 0
    return git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.mark.parametrize("stage", ["conflicted", "finished"])
def test_budget_cleanup_restores_the_original_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    for key in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{key}_NAME", "t")
        monkeypatch.setenv(f"GIT_{key}_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit(repo, "a.txt", "base\n")
    git(repo, "checkout", "-q", "-b", BRANCH)
    head = commit(repo, "a.txt", "branch\n")
    git(repo, "checkout", "-q", "main")
    commit(repo, "a.txt" if stage == "conflicted" else "b.txt", "main\n")
    git(repo, "checkout", "-q", BRANCH)
    rebase = git(repo, "rebase", "main")
    assert (rebase.returncode != 0) == (stage == "conflicted")
    assert git(repo, "rev-parse", "HEAD").stdout.strip() != head

    job = _job("fix", "claude", 1, head, {"kind": "rebase", "base_branch": "main", "gitBranchName": BRANCH})
    method = section(render(job, "box"), "Method")
    (paragraph,) = [line for line in method.splitlines() if "git rebase --abort" in line]
    cleanup = re.search(r"run `([^`]*git rebase --abort[^`]*)`", paragraph)
    assert cleanup is not None, paragraph
    subprocess.run(cleanup.group(1), shell=True, cwd=repo, capture_output=True)

    assert git(repo, "rev-parse", "HEAD").stdout.strip() == head
    assert git(repo, "status", "--porcelain").stdout == ""
    assert git(repo, "rev-parse", "-q", "--verify", "REBASE_HEAD").returncode != 0
    assert "rebase" not in git(repo, "status").stdout
