"""#130: the poller's trivial-update check, on real git repos."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lib import update_check
from lib.hold import ok_carries
from lib.secret_scan import ScanResult, SecretHit
from lib.update_check import (
    HeadDiff,
    MovedHead,
    classify,
    head_diff,
    merge_update,
    moved_head_probe,
)

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
    return merge_update(repo.gitdir, approved, head, base_ref)


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


def test_an_ok_carries_past_a_binary_hold_file_the_merges_never_touch(repo: Repo) -> None:
    approved = held_pr(repo, {**PR_FILES, "public/logo.png": "\x89PNG\x00\x01"})
    main_commit(repo, {"other.py": "x = 2\n"})
    head = repo.merge_main()

    assert ok_carries(carry(repo, approved, head), HOLD_PATTERNS)


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


@pytest.mark.parametrize(
    ("code", "stdout"), [(2, b""), (1, b"")], ids=["merge-tree-fails", "merge-tree-writes-no-tree"]
)
def test_an_unreadable_merge_tree_raises(
    repo: Repo, monkeypatch: pytest.MonkeyPatch, code: int, stdout: bytes
) -> None:
    approved = held_pr(repo, PR_FILES)
    main_commit(repo, {"other.py": "x = 2\n"})
    head = repo.merge_main()
    real_run = update_check._run

    def broken_merge_tree(clone, *args):
        if args[0] == "merge-tree":
            return subprocess.CompletedProcess(args, code, stdout, b"boom")
        return real_run(clone, *args)

    monkeypatch.setattr(update_check, "_run", broken_merge_tree)

    with pytest.raises(ValueError):
        carry(repo, approved, head)


def test_base_edits_near_a_held_change_still_carry_the_ok(repo: Repo) -> None:
    approved = held_pr(repo, PR_FILES)
    main_commit(
        repo,
        {
            ".github/workflows/ci.yml": "on: push\n",
            "lib/a.py": BASE.replace("line 5\n", "line 5 main\n"),
            "src/view/A.tsx": BASE.replace("line 4\n", "line 4 main\n"),
        },
    )
    repo.merge_main(
        lambda r: r.write("lib/a.py", BASE.replace("line 5\n", "line 5 pr and main\n"))
    )
    main_view = BASE.replace("line 4\n", "line 4 main\n").replace("line 6\n", "line 6 main\n")
    main_commit(repo, {"src/view/A.tsx": main_view})
    head = repo.merge_main()

    assert ok_carries(carry(repo, approved, head), HOLD_PATTERNS)


def test_a_whitespace_only_hold_resolution_needs_a_fresh_ok(repo: Repo) -> None:
    approved_label = 'export const label = "approved value";\n'
    approved = held_pr(
        repo,
        {
            "lib/a.py": BASE.replace("line 5\n", "line 5 pr\n"),
            "src/view/A.tsx": BASE.replace("line 2\n", approved_label),
        },
    )
    main_commit(
        repo,
        {
            "lib/a.py": BASE.replace("line 5\n", "line 5 main\n"),
            "src/view/A.tsx": BASE.replace("line 2\n", "line  2\n"),
        },
    )

    def resolve(r: Repo) -> None:
        r.write("lib/a.py", BASE.replace("line 5\n", "line 5 pr and main\n"))
        r.write(
            "src/view/A.tsx",
            BASE.replace("line 2\n", 'export const label = "approvedvalue";\n'),
        )

    head = repo.merge_main(resolve)

    assert not ok_carries(carry(repo, approved, head), HOLD_PATTERNS)


def test_a_clean_merge_amended_to_change_a_hold_file_needs_a_fresh_ok(repo: Repo) -> None:
    approved = held_pr(repo, PR_FILES)
    main_commit(repo, {"other.py": "x = 2\n"})
    repo.merge_main()
    repo.write("src/view/A.tsx", PR_FILES["src/view/A.tsx"].replace("line 9\n", "line 9 x\n"))
    repo.git("add", "-A")
    repo.git("commit", "-q", "--amend", "--no-edit")
    head = repo.git("rev-parse", "HEAD")

    assert not ok_carries(carry(repo, approved, head), HOLD_PATTERNS)


def qa_branch(repo: Repo) -> str:
    """A PR that edits one line of app.py, adds a 3-line file and a binary
    file, on a base main has since moved past."""
    repo.git("checkout", "-q", "-b", "pr", repo.base)
    repo.edit("app.py", "line 2\n", "line 2 pr\n")
    repo.write("new.txt", "a\nb\nc\n")
    (repo.root / "img.bin").write_bytes(b"\x00\x01\x02")
    head = repo.commit("pr")
    repo.git("checkout", "-q", "main")
    repo.edit("other.py", "1", "2")
    repo.commit("main moved")
    return head


def diff(repo: Repo, since: str | None, head: str) -> HeadDiff:
    return head_diff(repo.gitdir, since, head, "refs/heads/main")


def test_763_since_none_lists_the_prs_own_files_from_its_fork_with_a_binary_as_one_line(
    repo: Repo,
) -> None:
    head = qa_branch(repo)

    assert diff(repo, None, head).files == (("app.py", 2), ("img.bin", 1), ("new.txt", 3))


def test_763_since_a_head_lists_only_what_changed_after_it(repo: Repo) -> None:
    first = qa_branch(repo)
    repo.git("checkout", "-q", "pr")
    repo.write("new.txt", "a\nb\nc\nd\n")
    second = repo.commit("pr again")

    assert diff(repo, first, second).files == (("new.txt", 1),)


def test_763_the_same_patch_on_two_heads_has_the_same_patch_id(repo: Repo) -> None:
    head = qa_branch(repo)
    repo.git("checkout", "-q", "-b", "rebased", "main")
    repo.git("cherry-pick", head)
    rebased = repo.git("rev-parse", "HEAD")
    repo.edit("new.txt", "c\n", "c changed\n")
    changed = repo.commit("another patch")

    patch_id = diff(repo, None, head).patch_id
    assert rebased != head and patch_id is not None
    assert diff(repo, None, rebased).patch_id == patch_id
    assert diff(repo, None, changed).patch_id != patch_id


def test_763_a_head_with_no_change_from_base_has_no_patch_id(repo: Repo) -> None:
    assert diff(repo, None, repo.base) == HeadDiff(patch_id=None, files=())


def test_763_an_unknown_head_raises(repo: Repo) -> None:
    with pytest.raises(ValueError):
        diff(repo, None, "f" * 40)


def moved(repo: Repo, ok: str, head: str, base_ref: str = "refs/heads/main"):
    return moved_head_probe(repo.gitdir, base_ref, ok, head)


def test_1310_a_pure_merge_of_main_reads_as_one(repo: Repo) -> None:
    ok, main = repo.branches([("app.py", "line 2\n", "line 2 pr\n")], [("other.py", "1", "2")])
    head = repo.merge_main()

    probe = moved(repo, ok, head)

    assert probe == MovedHead(
        parents=(ok, main),
        second_parent_on_base=True,
        remerge_empty=True,
        paths=frozenset({"other.py"}),
        scan=ScanResult("clean", ()),
    )


def test_1310_a_hand_resolved_merge_has_a_remerge_diff(repo: Repo) -> None:
    ok, main = repo.branches(
        [("app.py", "line 5\n", "line 5 pr\n")], [("app.py", "line 5\n", "line 5 main\n")]
    )
    head = repo.merge_main(lambda r: r.write("app.py", BASE.replace("line 5\n", "line 5 both\n")))

    probe = moved(repo, ok, head)

    assert probe.parents == (ok, main)
    assert probe.second_parent_on_base
    assert not probe.remerge_empty


def test_1310_a_clean_merge_amended_with_an_edit_has_a_remerge_diff(repo: Repo) -> None:
    ok, _ = repo.branches([("app.py", "line 2\n", "line 2 pr\n")], [("other.py", "1", "2")])
    repo.merge_main()
    repo.edit("app.py", "line 9\n", "line 9 sneaked\n")
    repo.git("commit", "-q", "-a", "--amend", "--no-edit")
    head = repo.git("rev-parse", "HEAD")

    probe = moved(repo, ok, head)

    assert not probe.remerge_empty
    assert probe.paths == {"app.py", "other.py"}


def test_1310_a_second_commit_is_one_parent_and_not_empty(repo: Repo) -> None:
    ok, _ = repo.branches([("app.py", "line 2\n", "line 2 pr\n")], [])
    repo.git("checkout", "-q", "pr")
    repo.write("new.py", "y = 1\n")
    head = repo.commit("second")

    probe = moved(repo, ok, head)

    assert probe.parents == (ok,)
    assert not probe.second_parent_on_base
    assert not probe.remerge_empty
    assert probe.paths == {"new.py"}


def test_1310_a_merge_of_a_branch_off_main_is_off_base(repo: Repo) -> None:
    ok, _ = repo.branches([("app.py", "line 5\n", "line 5 pr\n")], [("other.py", "1", "2")])
    repo.git("checkout", "-q", "-b", "side", repo.base)
    repo.write("side.py", "s = 1\n")
    side = repo.commit("side work")
    repo.git("checkout", "-q", "pr")
    repo.git("merge", "-q", "--no-ff", "-m", "merge side", "side")
    head = repo.git("rev-parse", "HEAD")

    probe = moved(repo, ok, head)

    assert probe.parents == (ok, side)
    assert not probe.second_parent_on_base
    assert probe.remerge_empty
    assert probe.paths == {"side.py"}


def test_1310_a_rename_lists_both_sides(repo: Repo) -> None:
    ok, _ = repo.branches([], [])
    repo.git("checkout", "-q", "pr")
    repo.git("mv", "other.py", "moved.py")
    head = repo.commit("rename")

    assert moved(repo, ok, head).paths == {"other.py", "moved.py"}


def test_1310_a_merge_that_brings_in_a_token_is_found(repo: Repo) -> None:
    token = "gh" + "p_" + "A1b2C3d4E5" * 4
    ok, _ = repo.branches([("app.py", "line 2\n", "line 2 pr\n")], [("other.py", "x = 1\n", f"x = '{token}'\n")])
    head = repo.merge_main()

    scan = moved(repo, ok, head).scan

    assert scan == ScanResult("found", (SecretHit("other.py", "github-token"),))


def test_1310_an_unknown_ok_raises(repo: Repo) -> None:
    repo.branches([("app.py", "line 2\n", "line 2 pr\n")], [("other.py", "1", "2")])
    head = repo.merge_main()

    with pytest.raises(ValueError):
        moved(repo, "f" * 40, head)


def test_1310_a_missing_base_ref_raises(repo: Repo) -> None:
    ok, _ = repo.branches([("app.py", "line 2\n", "line 2 pr\n")], [("other.py", "1", "2")])
    head = repo.merge_main()

    with pytest.raises(ValueError):
        moved(repo, ok, head, "refs/remotes/origin/main")


def _merge_bringing_in_a_token(repo: Repo) -> tuple[str, str]:
    token = "gh" + "p_" + "A" * 36
    ok, _ = repo.branches([], [("other.py", "x = 1\n", token + "\n")])
    return ok, repo.merge_main()


def _merge_amended_with_an_edit(repo: Repo) -> tuple[str, str]:
    ok, _ = repo.branches([("app.py", "line 2\n", "line 2 pr\n")], [("other.py", "1", "2")])
    repo.merge_main()
    repo.edit("app.py", "line 9\n", "line 9 manual edit\n")
    repo.git("commit", "-q", "-a", "--amend", "--no-edit")
    return ok, repo.git("rev-parse", "HEAD")


def _merge_amended_with_a_gitlink(repo: Repo) -> tuple[str, str]:
    ok, _ = repo.branches([("app.py", "line 2\n", "line 2 pr\n")], [("other.py", "1", "2")])
    repo.merge_main()
    repo.git("update-index", "--add", "--cacheinfo", f"160000,{repo.base},dep")
    repo.git("commit", "-q", "--amend", "--no-edit")
    return ok, repo.git("rev-parse", "HEAD")


def _mask_python_diffs(repo: Repo) -> None:
    (repo.gitdir / "info" / "attributes").write_text("*.py diff=mask\n")
    repo.git("config", "diff.mask.textconv", "true")


CLONE_SETTINGS = {
    "color.ui=always": lambda r: r.git("config", "color.ui", "always"),
    "color.diff=always": lambda r: r.git("config", "color.diff", "always"),
    "textconv": _mask_python_diffs,
    "diff.ignoreSubmodules=all": lambda r: r.git("config", "diff.ignoreSubmodules", "all"),
    "diff.noprefix": lambda r: r.git("config", "diff.noprefix", "true"),
    "diff.mnemonicPrefix": lambda r: r.git("config", "diff.mnemonicPrefix", "true"),
    "diff.relative": lambda r: r.git("config", "diff.relative", "true"),
    "diff.external": lambda r: r.git("config", "diff.external", "true"),
    "diff.submodule=log": lambda r: r.git("config", "diff.submodule", "log"),
}


@pytest.mark.parametrize("setting", CLONE_SETTINGS)
@pytest.mark.parametrize(
    "history",
    [_merge_bringing_in_a_token, _merge_amended_with_an_edit, _merge_amended_with_a_gitlink],
)
def test_1310_a_clone_setting_never_changes_what_the_probe_reads(
    repo: Repo, setting: str, history
) -> None:
    ok, head = history(repo)
    expected = moved(repo, ok, head)

    CLONE_SETTINGS[setting](repo)

    assert moved(repo, ok, head) == expected


def test_1310_each_clone_setting_history_moves_the_probe_by_default(repo: Repo, tmp_path: Path) -> None:
    ok, head = _merge_bringing_in_a_token(repo)
    assert moved(repo, ok, head).scan.status == "found"
    other = Repo(tmp_path / "amended")
    ok, head = _merge_amended_with_an_edit(other)
    assert not moved(other, ok, head).remerge_empty
    linked = Repo(tmp_path / "linked")
    ok, head = _merge_amended_with_a_gitlink(linked)
    probe = moved(linked, ok, head)
    assert not probe.remerge_empty
    assert probe.paths == {"dep", "other.py"}


@pytest.mark.parametrize("setting", CLONE_SETTINGS)
def test_763_a_clone_setting_never_changes_the_qa_paths(repo: Repo, setting: str) -> None:
    ok, head = _merge_amended_with_a_gitlink(repo)
    expected = diff(repo, ok, head).files
    assert "dep" in dict(expected)

    CLONE_SETTINGS[setting](repo)

    assert diff(repo, ok, head).files == expected
