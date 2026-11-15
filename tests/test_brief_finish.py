"""The finish marker, the post command and the hygiene lines every brief carries (#169)."""

from __future__ import annotations

import re

import pytest

from lib.github_actions import TOKEN_VARIABLES
from tests.regenerate_brief_snapshots import CASES, render_snapshot

BUILDER_KINDS = sorted(kind for kind, case in CASES.items() if case[0] != "review")
QUESTIONS_LINES = (
    "If something left open needs a decision, post a `questions` handoff.",
    "If the ticket lists a dependency that has not merged, post `questions` before any work.",
)


def _briefs(kind: str) -> tuple[str, str]:
    box, remote = render_snapshot(kind).split("===== remote (")
    return box, remote


def test_the_remote_marker_is_plain_text_with_nothing_after_it() -> None:
    _, remote = _briefs("build")
    lines = remote.splitlines()
    for marker in ("ORCH-HANDOFF: <comment-id>", "ORCH-HANDOFF: failed"):
        assert marker in lines
        for line in lines:
            if marker in line:
                assert line == marker
    assert "`ORCH-HANDOFF" not in remote


@pytest.mark.parametrize("kind", sorted(CASES))
def test_the_post_command_unsets_every_token_and_prints_the_id(kind: str) -> None:
    for text in _briefs(kind):
        posts = [line for line in text.splitlines() if " gh api " in line]
        assert posts, "no gh api post command"
        for line in posts:
            assert "repos/owner/repo/issues/42/comments" in line
            assert line.rstrip("`").endswith("--jq .id")
            for name in TOKEN_VARIABLES:
                assert f"-u {name} " in line


@pytest.mark.parametrize("kind", sorted(CASES))
def test_every_gh_line_unsets_all_four_tokens(kind: str) -> None:
    for text in _briefs(kind):
        for match in re.finditer(r"`env (?:-u \S+ )*gh\b", text):
            for name in TOKEN_VARIABLES:
                assert f"-u {name} " in match.group(0)


@pytest.mark.parametrize("kind", sorted(CASES))
def test_no_bare_prove_it_works_no_deslop_and_no_timeout_line(kind: str) -> None:
    for text in _briefs(kind):
        assert not re.search(r"(?<!principle-)prove-it-works", text)
        assert "deslop" not in text
        assert not re.search(r"^- timeout:", text, re.MULTILINE)


@pytest.mark.parametrize("kind", ["build", "fix", "ci-fix", "retry", "resume"])
def test_builder_self_check_names_the_vendored_skill(kind: str) -> None:
    for text in _briefs(kind):
        assert "principle-prove-it-works" in text


@pytest.mark.parametrize("kind", BUILDER_KINDS)
def test_builder_briefs_carry_both_questions_lines(kind: str) -> None:
    for text in _briefs(kind):
        for line in QUESTIONS_LINES:
            assert line in text


def test_the_review_brief_carries_no_builder_questions_lines() -> None:
    for text in _briefs("review"):
        for line in QUESTIONS_LINES:
            assert line not in text
