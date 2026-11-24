"""The stack bounds probe and the `--onto` rebase it feeds (#1492, SPEC section 4.4 Retarget)."""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

import pytest

from lib.update_check import StackUnbounded, stack_bounds
from tests.test_rebase_brief import BRANCH, render, section
from tests.regenerate_brief_snapshots import _job
from tests.test_spec_stack_retarget import LINES, Repo, _edit


def _clone(repo: Repo) -> Path:
    return repo.path / ".git"


def _own(repo: Repo, old_head: str, parent_heads: list[str]) -> list[str]:
    return repo.git("rev-list", old_head, "--not", *parent_heads).split()


def _stack(repo: Repo) -> tuple[str, str]:
    repo.commit("base", "main.txt", ["main"])
    repo.git("switch", "-q", "-c", "parent")
    parent = repo.commit("parent-v1", "f.txt", LINES)
    repo.git("switch", "-q", "-c", BRANCH)
    repo.commit("child-one", "f.txt", _edit(LINES, 1, "child"))
    child = repo.commit("child-two", "c.txt", ["child"])
    return parent, child


def _child_merges_parent_update(repo: Repo) -> tuple[str, str, str]:
    p1, _ = _stack(repo)
    repo.git("switch", "-q", "parent")
    p2 = repo.commit("parent-v2", "f.txt", _edit(LINES, 50, "parent"))
    repo.git("switch", "-q", BRANCH)
    repo.git("merge", "-q", "--no-ff", "--no-edit", "parent")
    return p1, p2, repo.head()


def _squash_rewritten_parent(repo: Repo) -> tuple[str, str]:
    repo.git("switch", "-q", "parent")
    repo.git("reset", "-q", "--soft", "main")
    rewritten = repo.commit("parent-rewritten", "p.txt", ["rewritten"])
    return rewritten, repo.squash_into_main("parent")


def test_a_plain_child_is_bounded_by_its_feed_head(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "repo")
    parent, child = _stack(repo)

    old_base = stack_bounds(_clone(repo), child, [parent, parent])

    assert old_base == parent
    assert repo.git("rev-list", f"{old_base}..{child}").split() == _own(repo, child, [parent])


def test_a_parent_update_the_child_merged_moves_the_old_base(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "repo")
    p1, p2, old_head = _child_merges_parent_update(repo)
    rewritten, _ = _squash_rewritten_parent(repo)
    heads = [p1, rewritten, p2]

    old_base = stack_bounds(_clone(repo), old_head, heads)

    assert old_base == p2
    assert repo.git("rev-list", f"{old_base}..{old_head}").split() == _own(repo, old_head, heads)


def test_a_rewritten_parent_keeps_the_feed_head_as_the_old_base(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "repo")
    p1, child = _stack(repo)
    repo.git("switch", "-q", "main")
    repo.commit("main-v2", "main.txt", ["main", "main 2"])
    repo.git("switch", "-q", "parent")
    repo.git("rebase", "-q", "main")

    assert stack_bounds(_clone(repo), child, [p1, repo.head(), p1]) == p1


def test_short_names_resolve_to_the_full_old_base(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "repo")
    parent, _ = _stack(repo)

    assert stack_bounds(_clone(repo), BRANCH, ["parent"]) == parent


def test_refuses_a_merge_that_resolved_a_conflict(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "repo")
    repo.commit("base", "f", ["base"])
    repo.git("switch", "-q", "-c", "parent")
    p1 = repo.commit("parent-v1", "p.txt", ["parent"])
    repo.git("switch", "-q", "-c", BRANCH)
    repo.commit("child-only", "f", ["child"])
    repo.git("switch", "-q", "parent")
    p2 = repo.commit("parent-v2", "f", ["parent"])
    repo.git("switch", "-q", BRANCH)
    assert repo.run("merge", "-q", "--no-edit", "parent").returncode != 0, "merging the parent update conflicts"
    old_head = repo.commit("resolve", "f", ["resolved-parent-and-child"])

    with pytest.raises(StackUnbounded, match="merge"):
        stack_bounds(_clone(repo), old_head, [p1, p2])


def test_refuses_a_clean_merge_amended_with_an_edit(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "repo")
    p1, p2, _ = _child_merges_parent_update(repo)
    merged = repo.git("show", "HEAD:f.txt").splitlines()
    (repo.path / "f.txt").write_text("\n".join(_edit(merged, 30, "evil")) + "\n", encoding="utf-8")
    repo.git("commit", "-q", "-a", "--amend", "--no-edit")

    with pytest.raises(StackUnbounded, match="merge"):
        stack_bounds(_clone(repo), repo.head(), [p1, p2])


def test_refuses_two_merged_parent_rewrites(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "repo")
    p1, p2, _ = _child_merges_parent_update(repo)
    repo.git("switch", "-q", "parent")
    repo.git("reset", "-q", "--hard", p1)
    rewritten = repo.commit("parent-v2-rewritten", "f.txt", _edit(LINES, 40, "parent"))
    repo.git("switch", "-q", BRANCH)
    repo.git("merge", "-q", "--no-ff", "--no-edit", "parent")

    with pytest.raises(StackUnbounded, match="range"):
        stack_bounds(_clone(repo), repo.head(), [p1, rewritten, p2])


@pytest.mark.parametrize("heads", [[], ["unrelated"]], ids=["no-heads", "unrelated-head"])
def test_refuses_heads_that_bound_no_range(tmp_path: Path, heads: list[str]) -> None:
    repo = Repo(tmp_path / "repo")
    _, child = _stack(repo)
    repo.git("switch", "-q", "--orphan", "unrelated")
    repo.commit("unrelated", "u.txt", ["u"])

    with pytest.raises(StackUnbounded, match="range"):
        stack_bounds(_clone(repo), child, heads)


def test_an_unknown_commit_is_an_error(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "repo")
    parent, _ = _stack(repo)

    with pytest.raises(ValueError):
        stack_bounds(_clone(repo), "f" * 40, [parent])


def _land_merge_commit(repo: Repo, feed: str) -> list[str]:
    repo.git("switch", "-q", "main")
    repo.git("merge", "-q", "--no-ff", "--no-edit", "parent")
    return [feed, repo.git("rev-parse", "parent")]


def _land_squash(repo: Repo, feed: str) -> list[str]:
    repo.git("switch", "-q", "parent")
    p2 = repo.commit("parent-v2", "f.txt", _edit(LINES, 50, "parent"))
    repo.squash_into_main("parent")
    return [feed, p2]


def _land_after_parent_rebase(repo: Repo, feed: str) -> list[str]:
    repo.git("switch", "-q", "main")
    repo.commit("main-v2", "main.txt", ["main", "main 2"])
    repo.git("switch", "-q", "parent")
    repo.git("rebase", "-q", "main")
    rebased = repo.head()
    repo.git("switch", "-q", "main")
    repo.git("merge", "-q", "--no-ff", "--no-edit", "parent")
    return [feed, rebased, feed]


def _land_squash_after_child_merged_update(repo: Repo, feed: str) -> list[str]:
    repo.git("switch", "-q", "parent")
    p2 = repo.commit("parent-v2", "f.txt", _edit(LINES, 50, "parent"))
    repo.git("switch", "-q", BRANCH)
    repo.git("merge", "-q", "--no-ff", "--no-edit", "parent")
    rewritten, _ = _squash_rewritten_parent(repo)
    return [feed, rewritten, p2]


LANDINGS = {
    "merge-commit": _land_merge_commit,
    "squash": _land_squash,
    "after-parent-rebase": _land_after_parent_rebase,
    "squash-after-child-merged-update": _land_squash_after_child_merged_update,
}


def _patch_ids(repo: Repo, revisions: str) -> list[str]:
    ids = []
    for commit in repo.git("rev-list", "--reverse", "--no-merges", *revisions.split()).split():
        shown = repo.git("show", "--format=", commit)
        patch = subprocess.run(
            ["git", "patch-id", "--stable"], input=shown + "\n", capture_output=True, text=True, check=True
        )
        ids.append(patch.stdout.split()[0])
    return ids


def _brief_commands(old_head: str, onto_from: str) -> dict[str, list[str]]:
    rebase = {"from_state": "review", "from_head": old_head, "round": 1, "onto_from": onto_from}
    job = _job("fix", "claude", 1, old_head, {
        "kind": "rebase", "base_branch": "main", "gitBranchName": BRANCH, "rebase": rebase,
    })
    method = section(render(job, "box"), "Method")
    commands = {}
    for prefix in ("git rebase --onto ", "git rev-list --merges "):
        (command,) = [span for span in re.findall(r"`([^`\n]+)`", method) if span.startswith(prefix)]
        commands[prefix.split()[1]] = shlex.split(command)
    return commands


@pytest.mark.parametrize("landing", list(LANDINGS))
def test_replay_leaves_only_the_childs_own_commits(tmp_path: Path, landing: str) -> None:
    repo = Repo(tmp_path / "repo")
    feed, _ = _stack(repo)
    parent_heads = LANDINGS[landing](repo, feed)
    repo.git("update-ref", "refs/remotes/origin/main", "main")
    repo.git("switch", "-q", BRANCH)
    old_head = repo.head()
    own_before = _patch_ids(repo, f"{old_head} --not {' '.join(parent_heads)}")
    assert own_before, "the child has commits of its own"

    onto_from = stack_bounds(_clone(repo), old_head, parent_heads)
    commands = _brief_commands(old_head, onto_from)
    assert commands["rebase"] == ["git", "rebase", "--onto", "origin/main", onto_from]
    repo.git(*commands["rebase"][1:])

    assert repo.head() != old_head
    assert _patch_ids(repo, "origin/main..HEAD") == own_before
    assert repo.git(*commands["rev-list"][1:]) == ""
    assert repo.git("show", "HEAD:f.txt").splitlines()[0] == "child line 1", "the child's edit survived"
