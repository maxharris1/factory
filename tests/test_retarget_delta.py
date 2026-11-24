from __future__ import annotations

from pathlib import Path

import pytest

from lib.update_check import RetargetDelta, retarget_delta
from tests.test_spec_stack_retarget import LINES, Repo, _edit

BASE = "refs/heads/main"


def _landed(repo: Repo) -> dict[str, str]:
    repo.commit("base", "main.txt", ["main"])
    repo.git("switch", "-q", "-c", "parent")
    repo.commit("parent-files", "f.txt", LINES)
    onto_from = repo.commit("parent-more", "p.txt", ["parent"])
    repo.git("switch", "-q", "-c", "child")
    repo.commit("child-one", "f.txt", _edit(LINES, 1, "child"))
    from_head = repo.commit("child-two", "c.txt", ["child"])
    repo.git("switch", "-q", "parent")
    parent_head = repo.commit("parent-late", "q.txt", ["late"])
    repo.squash_into_main("parent")
    repo.git("switch", "-q", "-c", "rebased", from_head)
    repo.git("rebase", "-q", "--onto", "main", onto_from)
    return {
        "onto_from": onto_from,
        "from_head": from_head,
        "parent_head": parent_head,
        "new_head": repo.head(),
    }


def _delta(repo: Repo, shas: dict[str, str]) -> RetargetDelta:
    return retarget_delta(
        repo.path / ".git",
        shas["onto_from"],
        shas["from_head"],
        shas["parent_head"],
        shas["new_head"],
        BASE,
    )


def test_an_unchanged_child_over_a_squash_merged_parent_keeps_its_patch_id(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "repo")
    shas = _landed(repo)

    delta = _delta(repo, shas)

    assert delta.own_patch_before is not None
    assert delta.own_patch_before == delta.own_patch_after
    assert delta.child_files == frozenset({"c.txt", "f.txt"})


def test_a_child_commit_edited_in_the_replay_changes_its_patch_id(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "repo")
    shas = _landed(repo)
    (repo.path / "c.txt").write_text("child\nedited\n", encoding="utf-8")
    repo.git("commit", "-q", "-a", "--amend", "--no-edit")
    shas["new_head"] = repo.head()

    delta = _delta(repo, shas)

    assert delta.own_patch_before is not None
    assert delta.own_patch_after is not None
    assert delta.own_patch_before != delta.own_patch_after


def test_the_parent_delta_lists_its_files_from_the_old_base_to_its_head_at_merge(
    tmp_path: Path,
) -> None:
    repo = Repo(tmp_path / "repo")
    shas = _landed(repo)

    assert _delta(repo, shas).parent_files == frozenset({"q.txt"})

    shas["onto_from"] = repo.git("rev-parse", "main~1")
    assert _delta(repo, shas).parent_files == frozenset({"f.txt", "p.txt", "q.txt"})


def test_a_commit_the_clone_lacks_raises(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "repo")
    shas = _landed(repo)
    shas["parent_head"] = "f" * 40

    with pytest.raises(ValueError):
        _delta(repo, shas)
