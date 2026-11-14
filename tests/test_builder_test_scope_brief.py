"""Builder briefs run only the touched tests plus a smoke set and never wait on the full suite (#238)."""

from __future__ import annotations

import pytest

from tests.regenerate_brief_snapshots import render_snapshot

SCOPE = (
    "Tests: run only the tests for the files you touched plus a short smoke set, in the foreground. "
    "Then push and post `ready`. CI runs the full suite and is the gate: a red CI comes back to you "
    "as a fix job. Print mode stops background tasks after your last turn, and unpushed work is lost."
)
NEVER_FULL_SUITE = (
    "Never run the whole test suite locally, in the background, or in chunks, and never poll, "
    "watch or wait on that suite, on GitHub Actions or on any other CI from inside this job. "
    "CI after `ready` is the full-suite gate."
)
BUILDER_KINDS = ["build", "fix", "ci-fix", "merge-only", "resume", "retry"]


def sections(kind: str) -> list[str]:
    variants = render_snapshot(kind).split("===== ")[1:]
    assert len(variants) == 2
    return [v.split("## What to do\n", 1)[1].split("\n## ", 1)[0] for v in variants]


@pytest.mark.parametrize("kind", BUILDER_KINDS)
def test_builder_briefs_scope_tests_to_touched_files_and_never_wait_on_the_suite(kind: str) -> None:
    for section in sections(kind):
        lines = section.splitlines()
        assert SCOPE in lines
        assert NEVER_FULL_SUITE in lines
        assert "however long they take" not in section


def test_build_step_names_the_scope() -> None:
    for section in sections("build"):
        assert (
            "3. Run only the tests for the files you touched plus a short smoke set. "
            "Before `ready`, run the router's \"Before ready\" steps."
        ) in section.splitlines()


@pytest.mark.parametrize("kind", ["fix", "ci-fix"])
def test_fix_step_names_the_scope(kind: str) -> None:
    for section in sections(kind):
        assert "3. Run the tests." not in section
        assert (
            "3. Run only the new test and the tests for the files you touched, plus a short smoke set. "
            "Then run the router's \"Before ready\" steps."
        ) in section.splitlines()


@pytest.mark.parametrize("kind", ["plan", "review"])
def test_plan_and_review_briefs_carry_neither_line(kind: str) -> None:
    for section in sections(kind):
        assert SCOPE not in section
        assert NEVER_FULL_SUITE not in section
