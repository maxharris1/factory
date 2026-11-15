"""#207: is a new PR head a rewrite of the last reviewed head? On real git repos."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lib.update_check import RebaseScope, rebase_scope


class Repo:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "T")
        (root / "app.py").write_text("one\n")
        self.base = self.commit("base")

    @property
    def gitdir(self) -> Path:
        return self.root / ".git"

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, text=True
        ).stdout.strip()

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def pr_commit(self, text: str) -> str:
        self.git("checkout", "-q", "-B", "pr", self.base)
        (self.root / "feature.py").write_text(text)
        return self.commit("pr")

    def move_main(self) -> str:
        self.git("checkout", "-q", "main")
        (self.root / "app.py").write_text("two\n")
        return self.commit("main moved")


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path / "repo")


def test_a_plain_push_on_top_of_the_reviewed_head_has_no_rebase_scope(repo: Repo):
    reviewed = repo.pr_commit("x = 1\n")
    (repo.root / "feature.py").write_text("x = 2\n")
    head = repo.commit("follow-up")

    assert rebase_scope(repo.gitdir, reviewed, head, "refs/heads/main") is None


def test_a_force_push_onto_a_moved_base_gives_both_fork_points(repo: Repo):
    reviewed = repo.pr_commit("x = 1\n")
    moved = repo.move_main()
    repo.git("checkout", "-q", "pr")
    repo.git("rebase", "-q", "main")
    head = repo.git("rev-parse", "HEAD")

    assert rebase_scope(repo.gitdir, reviewed, head, "refs/heads/main") == RebaseScope(
        last_reviewed_head=reviewed, reviewed_base=repo.base, base=moved
    )


def test_an_amended_head_on_the_same_base_is_a_rewrite(repo: Repo):
    reviewed = repo.pr_commit("x = 1\n")
    head = repo.pr_commit("x = 2\n")

    assert rebase_scope(repo.gitdir, reviewed, head, "refs/heads/main") == RebaseScope(
        last_reviewed_head=reviewed, reviewed_base=repo.base, base=repo.base
    )


def test_a_commit_the_clone_does_not_hold_raises(repo: Repo):
    head = repo.pr_commit("x = 1\n")

    with pytest.raises(ValueError):
        rebase_scope(repo.gitdir, "f" * 40, head, "refs/heads/main")
