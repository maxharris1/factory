"""The merge-only brief runs the touched tests, pushes, and never waits on CI (#226)."""

from __future__ import annotations

import pytest

from lib.brief.common import TEST_SCOPE
from tests.regenerate_brief_snapshots import render_snapshot

DO_NOT_POLL = "Never poll, watch or wait on GitHub Actions or any other CI from inside this job."


def variants() -> list[str]:
    return render_snapshot("merge-only").split("===== ")[1:]


def method(text: str) -> str:
    return text.split("## Method\n", 1)[1].split("\n## ", 1)[0]


@pytest.fixture(params=["box", "remote (mac)"])
def section(request: pytest.FixtureRequest) -> str:
    (text,) = [v for v in variants() if v.startswith(request.param + " =====")]
    return method(text)


def test_runs_only_touched_tests_plus_a_smoke_set(section: str) -> None:
    assert (
        "4. Run only the tests for the files you touched while resolving the merge, plus a short "
        f"smoke set, {TEST_SCOPE} With no conflicts to resolve, the smoke set is enough."
    ) in section.splitlines()
    assert "full suite unless" not in section
    assert "then run the tests." not in section


def test_says_not_to_poll_or_wait_on_ci(section: str) -> None:
    assert DO_NOT_POLL in section.splitlines()
    assert section.count("wait on") == 1


def test_pushes_posts_ready_and_finishes(section: str) -> None:
    assert "5. Push, post the ready handoff below with the head you pushed, and finish the job." in section


def test_keeps_merge_commit_no_rebase_no_force_push(section: str) -> None:
    assert "2. `git merge origin/dev`: a merge commit only, no rebase, no force-push." in section
    assert "Change nothing besides the merge resolution." in section


def test_rules_and_finish_sections_stay() -> None:
    for text in variants():
        assert "Never start another factory job." in text
        assert text.index("## Method") < text.index("## Job") < text.index("## Finish the job")


def test_task_steps_stay_in_order(section: str) -> None:
    steps = ["1. Check out", "2. `git merge", "3. Resolve", "4. Run only", "5. Push", DO_NOT_POLL]
    positions = [section.index(step) for step in steps]
    assert positions == sorted(positions)
