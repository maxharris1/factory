"""#130: the poller's trivial-update check, on real git repos."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lib import update_check
from lib.update_check import classify

BASE = "".join(f"line {n}\n" for n in range(1, 21))


class Repo:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "T")
        self.write("app.py", BASE)
        self.write("other.py", "x = 1\n")
        self.base = self.commit("base")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, text=True
        ).stdout.strip()

    def write(self, path: str, text: str) -> None:
        (self.root / path).write_text(text)

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def edit(self, path: str, old: str, new: str) -> None:
        target = self.root / path
        target.write_text(target.read_text().replace(old, new))

    def branches(self, pr_edits, main_edits) -> tuple[str, str]:
        """PR branch and main, each with its edits on top of base."""
        self.git("checkout", "-q", "-b", "pr", self.base)
        for edit in pr_edits:
            self.edit(*edit)
        approved = self.commit("pr")
        self.git("checkout", "-q", "main")
        for edit in main_edits:
            self.edit(*edit)
        self.commit("main moved")
        return approved, self.git("rev-parse", "main")

    def merge_main(self, resolve=None) -> str:
        """Merge main into pr; resolve(repo) settles the conflicts."""
        self.git("checkout", "-q", "pr")
        merged = subprocess.run(
            ["git", "merge", "-q", "--no-ff", "-m", "merge main", "main"],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        if merged.returncode != 0:
            assert resolve is not None, merged.stdout + merged.stderr
            resolve(self)
            self.git("add", "-A")
            self.git("commit", "-q", "--no-edit")
        return self.git("rev-parse", "HEAD")

    @property
    def gitdir(self) -> Path:
        return self.root / ".git"


def run(repo: Repo, approved: str, head: str):
    return classify(repo.gitdir, approved, head, "refs/heads/main")


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path / "r")


def test_clean_merge_with_no_edit_is_trivial(repo: Repo) -> None:
    approved, _ = repo.branches([("app.py", "line 2\n", "line 2 pr\n")], [("other.py", "1", "2")])
    head = repo.merge_main()
    result = run(repo, approved, head)
    assert result.trivial, result.reason
    assert result.reason == "clean merge of base"


@pytest.mark.parametrize("side", ["ours", "theirs"])
def test_one_conflict_resolved_to_one_side_is_trivial(repo: Repo, side: str) -> None:
    approved, _ = repo.branches([("app.py", "line 5\n", "line 5 pr\n")], [("app.py", "line 5\n", "line 5 main\n")])
    head = repo.merge_main(lambda r: r.git("checkout", f"--{side}", "app.py"))
    result = run(repo, approved, head)
    assert result.trivial, result.reason


def test_two_hunks_one_ours_one_theirs_is_trivial(repo: Repo) -> None:
    approved, _ = repo.branches(
        [("app.py", "line 3\n", "line 3 pr\n"), ("app.py", "line 17\n", "line 17 pr\n")],
        [("app.py", "line 3\n", "line 3 main\n"), ("app.py", "line 17\n", "line 17 main\n")],
    )

    def resolve(r: Repo) -> None:
        text = BASE.replace("line 3\n", "line 3 pr\n").replace("line 17\n", "line 17 main\n")
        r.write("app.py", text)

    head = repo.merge_main(resolve)
    result = run(repo, approved, head)
    assert result.trivial, result.reason


def test_hand_merged_line_is_not_trivial(repo: Repo) -> None:
    approved, _ = repo.branches([("app.py", "line 5\n", "line 5 pr\n")], [("app.py", "line 5\n", "line 5 main\n")])
    head = repo.merge_main(lambda r: r.write("app.py", BASE.replace("line 5\n", "line 5 pr main\n")))
    result = run(repo, approved, head)
    assert not result.trivial
    assert "exactly one side" in result.reason


def test_both_sides_kept_is_not_trivial(repo: Repo) -> None:
    approved, _ = repo.branches([("app.py", "line 5\n", "line 5 pr\n")], [("app.py", "line 5\n", "line 5 main\n")])
    head = repo.merge_main(
        lambda r: r.write("app.py", BASE.replace("line 5\n", "line 5 pr\nline 5 main\n"))
    )
    assert not run(repo, approved, head).trivial


def test_extra_edit_in_the_conflicted_file_is_not_trivial(repo: Repo) -> None:
    approved, _ = repo.branches([("app.py", "line 5\n", "line 5 pr\n")], [("app.py", "line 5\n", "line 5 main\n")])

    def resolve(r: Repo) -> None:
        r.git("checkout", "--ours", "app.py")
        r.edit("app.py", "line 15\n", "line 15 sneaky\n")

    head = repo.merge_main(resolve)
    assert not run(repo, approved, head).trivial


def test_extra_edit_in_another_file_is_not_trivial(repo: Repo) -> None:
    approved, _ = repo.branches([("app.py", "line 5\n", "line 5 pr\n")], [("app.py", "line 5\n", "line 5 main\n")])

    def resolve(r: Repo) -> None:
        r.git("checkout", "--ours", "app.py")
        r.edit("other.py", "1", "3")

    head = repo.merge_main(resolve)
    result = run(repo, approved, head)
    assert not result.trivial
    assert "other.py" in result.reason


def test_extra_file_is_not_trivial(repo: Repo) -> None:
    approved, _ = repo.branches([("app.py", "line 5\n", "line 5 pr\n")], [("other.py", "1", "2")])
    head = repo.merge_main()
    repo.write("new_test.py", "assert True\n")
    repo.git("add", "-A")
    repo.git("commit", "-q", "--amend", "--no-edit")
    head = repo.git("rev-parse", "HEAD")
    result = run(repo, approved, head)
    assert not result.trivial
    assert "new_test.py" in result.reason


def test_modify_delete_conflict_is_not_trivial(repo: Repo) -> None:
    repo.git("checkout", "-q", "-b", "pr", repo.base)
    repo.edit("other.py", "1", "5")
    approved = repo.commit("pr edits other")
    repo.git("checkout", "-q", "main")
    repo.git("rm", "-q", "other.py")
    repo.commit("main deletes other")
    head = repo.merge_main(lambda r: r.git("rm", "-q", "other.py"))
    result = run(repo, approved, head)
    assert not result.trivial
    assert "not a content conflict" in result.reason


def test_first_parent_must_be_the_approved_head(repo: Repo) -> None:
    approved, _ = repo.branches([("app.py", "line 5\n", "line 5 pr\n")], [("other.py", "1", "2")])
    repo.git("checkout", "-q", "pr")
    repo.edit("app.py", "line 9\n", "line 9 later\n")
    repo.commit("pushed after approval")
    head = repo.merge_main()
    result = run(repo, approved, head)
    assert not result.trivial
    assert result.reason == "not a merge of the approved head and base"


def test_rebased_single_parent_head_is_not_trivial(repo: Repo) -> None:
    approved, main = repo.branches([("app.py", "line 5\n", "line 5 pr\n")], [("other.py", "1", "2")])
    repo.git("checkout", "-q", "pr")
    repo.git("rebase", "-q", "main")
    head = repo.git("rev-parse", "HEAD")
    assert not run(repo, approved, head).trivial


def test_second_parent_not_on_base_is_not_trivial(repo: Repo) -> None:
    approved, _ = repo.branches([("app.py", "line 5\n", "line 5 pr\n")], [("other.py", "1", "2")])
    repo.git("checkout", "-q", "-b", "side", repo.base)
    repo.edit("other.py", "1", "9")
    repo.commit("side work")
    repo.git("checkout", "-q", "pr")
    repo.git("merge", "-q", "--no-ff", "-m", "merge side", "side")
    head = repo.git("rev-parse", "HEAD")
    result = run(repo, approved, head)
    assert not result.trivial
    assert result.reason == "second parent is not on base"


def test_git_failure_is_not_trivial(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    approved, _ = repo.branches([("app.py", "line 2\n", "line 2 pr\n")], [("other.py", "1", "2")])
    head = repo.merge_main()

    def broken(*args, **kwargs):
        raise OSError("git is missing")

    monkeypatch.setattr(update_check.subprocess, "run", broken)
    result = run(repo, approved, head)
    assert not result.trivial
    assert result.reason == "check failed: OSError"


def test_unknown_commit_is_not_trivial(repo: Repo) -> None:
    result = run(repo, "0" * 40, "1" * 40)
    assert not result.trivial
