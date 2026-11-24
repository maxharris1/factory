import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from lib.dispatcher import CIResult, TrackedTicket
from lib.github_actions import GhResult, GitHubAdapterError, GitHubReviewActions
from tests.test_dispatcher import (
    VERDICT,
    FakeReviewActions,
    comment,
    seed_review_job,
    setup,
    ticket,
)

COMPARE = "repos/owner/repo/compare/"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit(repo: Path, name: str, text: str, message: str) -> str:
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def heads(tmp_path: Path) -> dict[str, str | Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    commit(repo, "base.txt", "one\n", "base")
    git(repo, "checkout", "-q", "-b", "feature")
    a = commit(repo, "feature.txt", "feature\n", "feature")
    git(repo, "checkout", "-q", "main")
    commit(repo, "other.txt", "other\n", "newer base")
    git(repo, "checkout", "-q", "-b", "rebased")
    git(repo, "cherry-pick", a)
    b = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "different", "main")
    c = commit(repo, "feature.txt", "something else\n", "feature")
    git(repo, "checkout", "-q", "main")
    return {"repo": repo, "original": a, "rebased": b, "different": c}


class RepoGh:

    def __init__(self, repo: Path, *, diff: str | None = None, fail: bool = False, base: str = "main"):
        self.repo = repo
        self.base = base
        self.diff = diff
        self.fail = fail
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], timeout: float) -> GhResult:
        argv = list(argv)
        self.calls.append(argv)
        endpoint = argv[-1]
        if endpoint == "repos/owner/repo/pulls/42":
            return GhResult(0, json.dumps({"base": {"ref": self.base}}), "")
        assert argv[-3:-1] == ["-H", "Accept: application/vnd.github.diff"], argv
        prefix = f"{COMPARE}{self.base}..."
        assert endpoint.startswith(prefix), endpoint
        if self.fail:
            return GhResult(1, "", "gh: Not Found (HTTP 404)\n")
        if self.diff is not None:
            return GhResult(0, self.diff, "")
        head = endpoint.removeprefix(prefix)
        base = git(self.repo, "merge-base", self.base, head)
        return GhResult(0, git(self.repo, "diff", base, head) + "\n", "")


def pr() -> TrackedTicket:
    return ticket()


def local_patch_id(repo: Path, head: str) -> str:
    base = git(repo, "merge-base", "main", head)
    diff = git(repo, "diff", base, head) + "\n"
    out = subprocess.run(
        ["git", "patch-id", "--stable"],
        input=diff,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return out.split()[0]


def test_patch_id_is_git_patch_id_stable_of_the_three_dot_diff(heads) -> None:
    repo = heads["repo"]
    actions = GitHubReviewActions(RepoGh(repo))

    a = actions.stable_patch_id(pr(), heads["original"])
    b = actions.stable_patch_id(pr(), heads["rebased"])
    c = actions.stable_patch_id(pr(), heads["different"])

    assert a == local_patch_id(repo, heads["original"])
    assert len(a) == 40
    assert a == b, "a pure rebase keeps the patch id"
    assert a != c, "different content changes the patch id"


@pytest.mark.parametrize(
    "gh",
    [
        pytest.param({"fail": True}, id="gh-error"),
        pytest.param({"diff": ""}, id="empty-diff"),
        pytest.param(
            {
                "diff": "diff --git a/x.png b/x.png\nindex 1..2 100644\n"
                "Binary files a/x.png and b/x.png differ\n"
            },
            id="binary-diff",
        ),
    ],
)
def test_an_unreadable_patch_id_raises(heads, gh: dict) -> None:
    actions = GitHubReviewActions(RepoGh(heads["repo"], **gh))

    with pytest.raises(GitHubAdapterError):
        actions.stable_patch_id(pr(), heads["original"])


def test_a_missing_git_raises(heads, monkeypatch: pytest.MonkeyPatch) -> None:
    diff = "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n-old\n+new\n"
    monkeypatch.setenv("PATH", "/nonexistent")
    actions = GitHubReviewActions(RepoGh(heads["repo"], diff=diff))

    with pytest.raises(GitHubAdapterError, match="git could not run"):
        actions.stable_patch_id(pr(), heads["original"])


class AdapterPatchIds(FakeReviewActions):

    def __init__(self, gh: RepoGh):
        super().__init__()
        self.github = GitHubReviewActions(gh)

    def stable_patch_id(self, ticket: TrackedTicket, head: str) -> str:
        self.patch_id_calls.append(head)
        return self.github.stable_patch_id(ticket, head)


def approve_then_move(
    tmp_path: Path, actions: AdapterPatchIds, approved: str, current: str, *, named: int = 40
):
    dispatcher, queue = setup(tmp_path, review_actions=actions)
    seed_review_job(dispatcher, queue, approved)
    actions.states.clear()
    dispatcher.process(
        ticket(
            comment(
                "approve-1",
                VERDICT.format(round=1, head=approved[:named], verdict="APPROVE", risk="high"),
                author="codex-login",
            ),
            head=current,
            ci={current: CIResult("success")},
        )
    )
    return queue.claim("codex")


@pytest.mark.parametrize("named", [40, 7], ids=["full-head", "abbreviated-head"])
def test_an_approve_carries_across_a_pure_rebase(tmp_path: Path, heads, named: int) -> None:
    actions = AdapterPatchIds(RepoGh(heads["repo"]))

    review = approve_then_move(
        tmp_path, actions, heads["original"], heads["rebased"], named=named
    )

    assert review is None
    assert actions.merges == [(True, heads["rebased"])]
    assert actions.patch_id_calls == [heads["original"], heads["rebased"]]


@pytest.mark.parametrize(
    "gh",
    [
        pytest.param({}, id="different-patch"),
        pytest.param({"fail": True}, id="gh-error"),
        pytest.param({"diff": "Binary files a/x and b/x differ\n"}, id="binary-diff"),
    ],
)
def test_a_changed_or_unreadable_patch_re_reviews(tmp_path: Path, heads, gh: dict) -> None:
    actions = AdapterPatchIds(RepoGh(heads["repo"], **gh))

    review = approve_then_move(tmp_path, actions, heads["original"], heads["different"])

    assert actions.merges == []
    assert review is not None
    assert review.request.action == "review"
    assert review.request.head == heads["different"]


def test_a_stacked_child_compare_covers_only_its_own_files(tmp_path: Path) -> None:
    repo = tmp_path / "stack"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    commit(repo, "base.txt", "one\n", "base")
    git(repo, "checkout", "-q", "-b", "factory/issue-1449")
    commit(repo, "parent_only.py", "parent = 1\n", "parent")
    git(repo, "checkout", "-q", "-b", "child")
    head = commit(repo, "child.py", "child = 1\n", "child")
    gh = RepoGh(repo, base="factory/issue-1449")

    GitHubReviewActions(gh).stable_patch_id(pr(), head)

    (compare,) = [call for call in gh.calls if "/compare/" in call[-1]]
    assert compare[-1] == f"{COMPARE}factory/issue-1449...{head}"
    diff = gh(compare, 10).stdout
    assert "child.py" in diff
    assert "parent_only.py" not in diff
