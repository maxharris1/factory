"""Builder briefs run only the touched tests plus a smoke set and never wait on the full suite (#238)."""

from __future__ import annotations

import pytest

from tests.regenerate_brief_snapshots import render_snapshot

SCOPE = (
    "CI runs the full suite and is the gate: a red CI comes back to you "
    "as a fix job. Print mode stops background tasks after your last turn, and unpushed work is lost."
)
NEVER_FULL_SUITE = "Never poll, watch or wait on GitHub Actions or any other CI from inside this job."
FOREGROUND = "in the foreground; never"
BUILDER_KINDS = ["build", "fix", "ci-fix", "merge-only", "resume", "retry"]


def sections(kind: str) -> list[str]:
    variants = render_snapshot(kind).split("===== ")[1:]
    assert len(variants) == 2
    return [v.split("## Method\n", 1)[1].split("\n## ", 1)[0] for v in variants]


@pytest.mark.parametrize("kind", BUILDER_KINDS)
def test_builder_briefs_scope_tests_to_touched_files_and_never_wait_on_the_suite(kind: str) -> None:
    for section in sections(kind):
        lines = section.splitlines()
        assert SCOPE in lines
        assert (NEVER_FULL_SUITE in lines) is (kind == "merge-only")
        assert section.count("in the foreground") == 1
        assert section.count("wait on") == (kind == "merge-only")
        step = next(line for line in lines if FOREGROUND in line)
        assert step.startswith(("3. ", "4. ")) and "in the background or in chunks" in step
        assert "however long they take" not in section


def test_build_step_names_the_scope() -> None:
    for section in sections("build"):
        assert (
            "3. Run only the tests for the files you touched plus a short smoke set, in the foreground; "
            "never the whole suite, in the background or in chunks. "
            "Before `ready`, run the router's \"Before ready\" steps."
        ) in section.splitlines()


@pytest.mark.parametrize("kind", ["fix", "ci-fix"])
def test_fix_step_names_the_scope(kind: str) -> None:
    for section in sections(kind):
        assert "3. Run the tests." not in section
        step = next(line for line in section.splitlines() if line.startswith("3. "))
        assert "the tests for the files you touched, plus a short smoke set" in step
        assert "in the foreground; never the whole suite, in the background or in chunks." in step
        assert "Then run the router's \"Before ready\" steps." in step


@pytest.mark.parametrize("kind", ["plan", "review"])
def test_plan_and_review_briefs_carry_neither_line(kind: str) -> None:
    for section in sections(kind):
        assert SCOPE not in section
        assert NEVER_FULL_SUITE not in section
