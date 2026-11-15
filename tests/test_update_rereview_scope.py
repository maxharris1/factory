"""#260: a re-review after a merge-only base update covers the merge
resolution and any later commits, not the whole PR again."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.brief import render_brief
from lib.update_check import UpdateClass, UpdateScope
from tests.regenerate_brief_snapshots import BOX_FACTORY_ROOT, HEAD, UPDATE, _job
from tests.test_update_check import Repo, run
from tests.test_update_from_main import conflicted
from tests.merge_queue_support import sha

LAST = "a" * 40
MERGE = "b" * 40


def review_brief(**extra: object) -> str:
    return render_brief(
        _job("review", "codex", 1, HEAD, extra),
        bot="codex-bot",
        context=None,
        factory_root=BOX_FACTORY_ROOT,
    )


def test_update_brief_names_the_reviewed_head_and_remerge_diff() -> None:
    brief = review_brief(round="1/3", **UPDATE)
    assert f"The code at `{LAST}` was already reviewed" in brief
    assert f"`git show --remerge-diff {MERGE}`" in brief
    assert "read the whole diff" not in brief.lower()
    assert "Close the earlier rounds" not in brief
    assert "came after the merge" not in brief


def test_update_brief_lists_later_commits_for_a_normal_review() -> None:
    commits = ["c" * 40, "d" * 40]
    brief = review_brief(round="1/3", **{**UPDATE, "update_commits": commits})
    assert (
        f"get a normal review: `{commits[0]}` `{commits[1]}`. "
        f"Read their diff (`git diff {MERGE} {HEAD}`)."
    ) in brief


def test_update_brief_caps_a_long_commit_list() -> None:
    commits = [f"{n:040x}" for n in range(1, 61)]
    brief = review_brief(round="1/3", **{**UPDATE, "update_commits": commits})
    assert f"`{commits[49]}`" in brief
    assert f"`{commits[50]}`" not in brief
    assert "60 commits in all" in brief


@pytest.mark.parametrize(
    "broken",
    [
        {"kind": "merge-only"},
        {"last_reviewed_head": "not-a-sha"},
        {"update_merge": None},
        {"update_commits": "c" * 40},
        {"update_commits": ["c" * 40, "zz"]},
    ],
)
def test_malformed_update_detail_falls_back_to_the_full_review(broken: dict) -> None:
    brief = review_brief(round="1/3", **{**UPDATE, **broken})
    assert "Read the whole diff" in brief
    assert "remerge-diff" not in brief


def test_a_fix_round_review_is_unchanged() -> None:
    brief = review_brief(round="2/3")
    assert "Read the whole diff" in brief
    assert "Close the earlier rounds first" in brief
    assert "remerge-diff" not in brief


def test_classify_scopes_a_merge_followed_by_commits(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "r")
    approved, _main = repo.branches(
        [("app.py", "line 5\n", "pr 5\n")], [("app.py", "line 5\n", "main 5\n")]
    )
    merge = repo.merge_main(lambda r: r.write("app.py", "resolved\n"))
    repo.write("other.py", "x = 2\n")
    first = repo.commit("after one")
    repo.write("other.py", "x = 3\n")
    second = repo.commit("after two")

    verdict = run(repo, approved, second)
    assert not verdict.trivial
    assert verdict.scope == UpdateScope(approved, merge, (first, second))


def test_classify_scopes_a_non_trivial_merge_alone(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "r")
    approved, _main = repo.branches(
        [("app.py", "line 5\n", "pr 5\n")], [("app.py", "line 5\n", "main 5\n")]
    )
    merge = repo.merge_main(lambda r: r.write("app.py", "resolved\n"))

    verdict = run(repo, approved, merge)
    assert not verdict.trivial
    assert verdict.scope == UpdateScope(approved, merge, ())


def test_a_trivial_update_has_no_scope(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "r")
    approved, _main = repo.branches([("app.py", "line 5\n", "pr 5\n")], [("other.py", "x = 1\n", "x = 9\n")])
    verdict = run(repo, approved, repo.merge_main())
    assert verdict.trivial
    assert verdict.scope is None


def test_a_rebase_has_no_scope(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "r")
    approved, _main = repo.branches([("app.py", "line 5\n", "pr 5\n")], [("other.py", "x = 1\n", "x = 9\n")])
    repo.git("checkout", "-q", "-b", "rebased", "main")
    repo.edit("app.py", "line 5\n", "pr 5\n")
    rebased = repo.commit("pr rebased")
    verdict = run(repo, approved, rebased)
    assert not verdict.trivial
    assert verdict.scope is None


def test_a_second_base_merge_has_no_scope(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "r")
    approved, _main = repo.branches([("app.py", "line 5\n", "pr 5\n")], [("other.py", "x = 1\n", "x = 9\n")])
    repo.merge_main()
    repo.git("checkout", "-q", "main")
    repo.edit("app.py", "line 20\n", "main 20\n")
    repo.commit("main again")
    head = repo.merge_main()
    verdict = run(repo, approved, head)
    assert not verdict.trivial
    assert verdict.scope is None


def test_a_scoped_update_queues_an_update_review_at_the_approved_round(tmp_path: Path) -> None:
    board, pr, _approve = conflicted(tmp_path)
    board.verdict = UpdateClass(
        False, "edits outside conflict resolution", UpdateScope(sha(1), sha(3), (sha(2),))
    )
    board.finish("claude")
    pr.push(sha(2))
    ready = board.ready(pr, sha(2))
    board.poll()

    assert board.outcome(ready) == "queued-review"
    detail = json.loads(board.jobs("review")[-1]["detail"])
    assert detail["round"] == "1/3"
    assert detail["kind"] == "update"
    assert detail["last_reviewed_head"] == sha(1)
    assert detail["update_merge"] == sha(3)
    assert detail["update_commits"] == [sha(2)]


def test_an_unscoped_update_queues_the_normal_review(tmp_path: Path) -> None:
    board, pr, _approve = conflicted(tmp_path)
    board.verdict = UpdateClass(False, "not a merge of the approved head and base")
    board.finish("claude")
    pr.push(sha(2))
    ready = board.ready(pr, sha(2))
    board.poll()

    assert board.outcome(ready) == "queued-review"
    detail = json.loads(board.jobs("review")[-1]["detail"])
    assert detail["round"] == "1/3"
    assert "kind" not in detail
    assert "last_reviewed_head" not in detail
