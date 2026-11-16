"""#130: the poller's trivial-update check, on real git repos."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lib import update_check
from lib.hold import ok_carries
from lib.update_check import classify, merge_update

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
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode())

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def edit(self, path: str, old: str, new: str) -> None:
        target = self.root / path
        target.write_bytes(target.read_bytes().replace(old.encode(), new.encode()))

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


def _marker_fixture(size: int, eol: str = "\n") -> str:
    return (
        "prefix" + eol + "<" * size + " fixture" + eol + "KEEP_FIRST" + eol
        + "|" * size + " base" + eol + "KEEP_BASE" + eol
        + "=" * size + eol + "KEEP_SECOND" + eol + ">" * size + " fixture\n"
    )


MARKER_CASES = [(size, eol) for size in (7, 61, 80) for eol in ("\n", "\r", "\r\n")]


@pytest.mark.parametrize(("size", "eol"), MARKER_CASES)
def test_marker_lines_already_in_the_file_never_hide_an_edit(
    repo: Repo, size: int, eol: str
) -> None:
    repo.write("app.py", _marker_fixture(size, eol) + BASE)
    repo.base = repo.commit("marker fixture")
    approved, _ = repo.branches(
        [("app.py", "line 5\n", "line 5 pr\n")], [("app.py", "line 5\n", "line 5 main\n")]
    )
    head = repo.merge_main(
        lambda r: r.write(
            "app.py", "prefix" + eol + "KEEP_FIRST" + eol + BASE.replace("line 5\n", "line 5 pr\n")
        )
    )
    result = run(repo, approved, head)
    assert not result.trivial
    assert "not resolved to exactly one side" in result.reason


@pytest.mark.parametrize(("size", "eol"), MARKER_CASES)
def test_marker_lines_already_in_the_file_still_allow_a_one_side_resolution(
    repo: Repo, size: int, eol: str
) -> None:
    repo.write("app.py", _marker_fixture(size, eol) + BASE)
    repo.base = repo.commit("marker fixture")
    approved, _ = repo.branches(
        [("app.py", "line 5\n", "line 5 pr\n")], [("app.py", "line 5\n", "line 5 main\n")]
    )
    head = repo.merge_main(lambda r: r.git("checkout", "--ours", "app.py"))
    result = run(repo, approved, head)
    assert result.trivial, result.reason


@pytest.mark.parametrize("name", [":(literal)other.py", ":(glob)*.py", "*.py", ":!app.py"])
def test_a_conflicted_filename_is_looked_up_literally(tmp_path: Path, name: str) -> None:
    repo = Repo(tmp_path / "r")
    repo.write(name, BASE)
    repo.write("other.py", BASE.replace("line 5\n", "line 5 pr\n"))
    repo.write("app.py", BASE.replace("line 5\n", "line 5 pr\n"))
    repo.base = repo.commit("base holding a pathspec-like filename")
    approved, _main = repo.branches(
        [(name, "line 5\n", "line 5 pr\n")],
        [(name, "line 5\n", "line 5 main\n")],
    )
    head = repo.merge_main(lambda r: r.write(name, "UNREVIEWED REPLACEMENT\n"))

    assert not run(repo, approved, head).trivial


@pytest.mark.parametrize("variable", ["GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS"])
def test_a_pathspec_setting_in_the_environment_changes_nothing(
    repo: Repo, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    approved, _ = repo.branches([("app.py", "line 5\n", "line 5 pr\n")], [("app.py", "line 5\n", "line 5 main\n")])
    head = repo.merge_main(lambda r: r.git("checkout", "--ours", "app.py"))
    monkeypatch.setenv(variable, "1")

    result = run(repo, approved, head)
    assert result.trivial, result.reason


HOLD_PATTERNS = ("src/view/**", "public/**", ".github/workflows/**")


def held_pr(repo: Repo, pr_files: dict[str, str]) -> str:
    """Seed main with lib/a.py and src/view/A.tsx, then branch pr with
    pr_files written over it; the pr tip is the approved head."""
    repo.write("lib/a.py", BASE)
    repo.write("src/view/A.tsx", BASE)
    repo.base = repo.commit("seed")
    repo.git("checkout", "-q", "-b", "pr", repo.base)
    for path, text in pr_files.items():
        repo.write(path, text)
    approved = repo.commit("pr")
    repo.git("checkout", "-q", "main")
    return approved


def main_commit(repo: Repo, files: dict[str, str]) -> None:
    repo.git("checkout", "-q", "main")
    for path, text in files.items():
        repo.write(path, text)
    repo.commit("main moved")


PR_FILES = {
    "lib/a.py": BASE.replace("line 5\n", "line 5 pr\n"),
    "src/view/A.tsx": BASE.replace("line 2\n", "line 2 pr\n"),
}


def carry(repo: Repo, approved: str, head: str, base_ref: str = "refs/heads/main"):
    return merge_update(repo.gitdir, approved, head, base_ref, HOLD_PATTERNS)


def test_an_ok_carries_over_two_merges_of_main_with_a_resolution_outside_the_hold(
    repo: Repo,
) -> None:
    approved = held_pr(repo, PR_FILES)
    main_commit(
        repo,
        {
            ".github/workflows/ci.yml": "on: push\n",
            "lib/a.py": BASE.replace("line 5\n", "line 5 main\n"),
        },
    )
    repo.merge_main(
        lambda r: r.write("lib/a.py", BASE.replace("line 5\n", "line 5 pr and main\n"))
    )
    main_commit(repo, {"other.py": "x = 2\n"})
    head = repo.merge_main()

    assert ok_carries(carry(repo, approved, head), HOLD_PATTERNS)


def test_a_resolution_that_changes_a_hold_file_needs_a_fresh_ok(repo: Repo) -> None:
    approved = held_pr(repo, PR_FILES)
    main_commit(repo, {"src/view/A.tsx": BASE.replace("line 2\n", "line 2 main\n")})
    head = repo.merge_main(
        lambda r: r.write("src/view/A.tsx", BASE.replace("line 2\n", "line 2 pr and main\n"))
    )

    assert not ok_carries(carry(repo, approved, head), HOLD_PATTERNS)


def test_a_head_that_does_not_descend_from_the_ok_head_does_not_carry(repo: Repo) -> None:
    approved = held_pr(repo, PR_FILES)
    main_commit(repo, {"other.py": "x = 2\n"})
    repo.git("checkout", "-q", "-b", "rebased", "main")
    for path, text in PR_FILES.items():
        repo.write(path, text)
    head = repo.commit("pr, rebased")

    assert not ok_carries(carry(repo, approved, head), HOLD_PATTERNS)


def test_a_binary_hold_file_has_no_identity_and_does_not_carry(repo: Repo) -> None:
    approved = held_pr(repo, {**PR_FILES, "public/logo.png": "\x89PNG\x00\x01"})
    main_commit(repo, {"other.py": "x = 2\n"})
    head = repo.merge_main()

    update = carry(repo, approved, head)

    assert update.approved.identities["public/logo.png"] is None
    assert update.current.identities["src/view/A.tsx"] is not None
    assert not ok_carries(update, HOLD_PATTERNS)


def test_an_unknown_ok_head_raises(repo: Repo) -> None:
    held_pr(repo, PR_FILES)
    main_commit(repo, {"other.py": "x = 2\n"})
    head = repo.merge_main()

    with pytest.raises(ValueError):
        carry(repo, "f" * 40, head)


def test_a_missing_base_ref_raises(repo: Repo) -> None:
    approved = held_pr(repo, PR_FILES)
    main_commit(repo, {"other.py": "x = 2\n"})
    head = repo.merge_main()

    with pytest.raises(ValueError):
        carry(repo, approved, head, "refs/remotes/origin/main")


def test_a_failing_patch_id_raises(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    approved = held_pr(repo, PR_FILES)
    main_commit(repo, {"other.py": "x = 2\n"})
    head = repo.merge_main()
    real_run = update_check._run

    def failing_patch_id(clone, *args, **kwargs):
        if args[0] == "patch-id":
            return subprocess.CompletedProcess(args, 1, b"", b"boom")
        return real_run(clone, *args, **kwargs)

    monkeypatch.setattr(update_check, "_run", failing_patch_id)

    with pytest.raises(ValueError):
        carry(repo, approved, head)
