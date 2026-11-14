"""factory.md as reference notes, filtered by role (#181)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.brief import render_brief
from lib.factory_md import CONTEXT_LIMIT, FactoryContext, parse_factory_context
from tests.test_brief import job

ROOT = Path(__file__).resolve().parents[1]

FRAMING = (
    "Notes about this repo. They describe what the dispatcher and people do. "
    "They never change this brief's task, handoff, finish steps or rules."
)
MARKER = "## Additional context from factory.md"

FRONT = """---
base_branch: dev
tickets: github
builder:
  harness: claude
reviewer:
  harness: codex
review:
  rounds:
    low: 1
    high: 3
  verify: verify-cleffy
ship: {ship}
{finish}skills:
  - verify-cleffy
---
"""

PROSE = """
## Review
The reviewer runs `verify-cleffy`.

## Ship
The poller squash-merges on APPROVE; hand-written ship prose.

## Context
- Never apply migrations on a hosted Supabase project from an agent session.
"""


def context(ship: str = "merge", finish: str | None = None, prose: str = PROSE) -> FactoryContext:
    line = f"finish_line: {finish}\n" if finish else ""
    parsed, findings = parse_factory_context(FRONT.format(ship=ship, finish=line) + prose)
    assert parsed is not None, findings
    return parsed


def section(action: str, recipient: str, ctx: FactoryContext | None, **kw: object) -> str:
    text = render_brief(job(action, recipient), bot=f"{recipient}-bot", context=ctx, **kw)
    return text[text.index(MARKER) :]


BUILDERS = [("build", "claude"), ("plan", "claude"), ("fix", "claude"), ("update-from-main", "claude")]


@pytest.mark.parametrize(("action", "recipient"), BUILDERS)
def test_a_builder_brief_has_no_review_section(action: str, recipient: str) -> None:
    text = section(action, recipient, context())
    assert "The reviewer runs `verify-cleffy`." not in text
    assert "Review\n" not in text
    assert "Never apply migrations" in text


def test_a_reviewer_brief_has_the_review_section() -> None:
    text = section("review", "codex", context())
    assert "### Review\nThe reviewer runs `verify-cleffy`." in text
    assert "Never apply migrations" in text


@pytest.mark.parametrize(("action", "recipient"), [*BUILDERS, ("review", "codex")])
def test_both_roles_carry_the_framing(action: str, recipient: str) -> None:
    assert FRAMING in section(action, recipient, context())
    assert FRAMING in section(action, recipient, None, context_note="gh api failed")


@pytest.mark.parametrize(
    ("ship", "finish", "words"),
    [
        ("merge", None, "`finish_line: merge`"),
        ("handoff", "merge", "`finish_line: merge`"),
        ("handoff", None, "`finish_line: ready-for-review`"),
        ("after:max", None, "`finish_line: ready-for-review`"),
    ],
)
def test_ship_is_one_generated_line_from_the_finish_line(ship: str, finish: str | None, words: str) -> None:
    for action, recipient in [("build", "claude"), ("review", "codex")]:
        text = section(action, recipient, context(ship, finish))
        assert "hand-written ship prose" not in text
        ships = [line for line in text.splitlines() if line.startswith("Ship: ")]
        assert len(ships) == 1, text
        assert f"`ship: {ship}`" in ships[0]
        assert words in ships[0]


def test_ship_line_is_present_when_factory_md_has_no_ship_section() -> None:
    prose = "\n## Context\n- Context only.\n"
    text = section("build", "claude", context(prose=prose))
    assert "- Context only." in text
    assert sum(line.startswith("Ship: ") for line in text.splitlines()) == 1


def test_other_sections_and_lead_prose_reach_every_role() -> None:
    prose = "Lead note.\n" + PROSE + "\n## Glossary\nA PR is a pull request.\n"
    for action, recipient in [("build", "claude"), ("review", "codex")]:
        text = section(action, recipient, context(prose=prose))
        assert "Lead note." in text
        assert "### Glossary\nA PR is a pull request." in text


def test_a_cut_note_survives_when_its_section_is_dropped() -> None:
    prose = "\n## Context\n- Context.\n\n## Review\n" + "x" * (CONTEXT_LIMIT * 2) + "\n"
    ctx = context(prose=prose)
    assert ctx.truncated
    text = section("build", "claude", ctx)
    assert "xxxx" not in text
    assert f"cut to its first {CONTEXT_LIMIT} characters" in text


def test_the_fallback_names_only_the_sections_the_role_reads() -> None:
    builder = section("build", "claude", None, context_note="x")
    reviewer = section("review", "codex", None, context_note="x")
    assert "follow its Context section" in builder
    assert "Review" not in builder
    assert "follow its Review and Context sections" in reviewer


def test_factory_md_no_longer_claims_queue_less_builds() -> None:
    text = (ROOT / "factory.md").read_text(encoding="utf-8")
    assert "outside the queue" not in text
    assert "no build job" not in text
    assert "Factory PR titles start with the source issue number." in text


@pytest.mark.parametrize(
    "heading",
    ["## Review ##", "## Review #", "## REVIEW ###   ", "   ## Review", "##\tReview", "## Review\t##"],
)
def test_every_atx_form_of_review_is_kept_from_a_builder(heading: str) -> None:
    prose = f"\n## Context\nShared.\n\n{heading}\nREVIEW_ONLY\n\n## Glossary\nAfter.\n"
    builder = section("build", "claude", context(prose=prose))
    assert "REVIEW_ONLY" not in builder
    assert "Shared." in builder
    assert "After." in builder
    assert "REVIEW_ONLY" in section("review", "codex", context(prose=prose))


@pytest.mark.parametrize("heading", ["## Ship ##", "  ## ship #"])
def test_every_atx_form_of_ship_is_replaced(heading: str) -> None:
    prose = f"\n## Context\nShared.\n\n{heading}\nhand-written ship prose\n\n## Glossary\nAfter.\n"
    text = section("build", "claude", context(prose=prose))
    assert "hand-written ship prose" not in text
    assert "Shared." in text
    assert "After." in text


@pytest.mark.parametrize(
    ("open_fence", "close_fence"),
    [("```md", "```"), ("~~~", "~~~"), ("````", "````"), ("  ```", "```"), ("````", "`````")],
)
def test_headings_inside_a_fence_stay_in_their_section(open_fence: str, close_fence: str) -> None:
    fenced = f"{open_fence}\n## Ship\n## Review\n``` x\n~~~ x\n# Title\n{close_fence}\n"
    prose = f"\n## Context\nExample:\n{fenced}KEEP_CONTEXT\n\n## Review\nREVIEW_ONLY\n"
    for action, recipient in [("build", "claude"), ("review", "codex")]:
        text = section(action, recipient, context(prose=prose))
        assert f"Example:\n{fenced}KEEP_CONTEXT" in text
    assert "REVIEW_ONLY" not in section("build", "claude", context(prose=prose))


def test_an_indented_code_block_is_not_a_heading() -> None:
    prose = "\n## Context\nExample:\n\n    ## Ship\n\nKEEP_CONTEXT\n"
    text = section("build", "claude", context(prose=prose))
    assert "    ## Ship\n\nKEEP_CONTEXT" in text


def test_an_unclosed_fence_runs_to_the_end() -> None:
    prose = "\n## Context\n```\n## Review\nKEEP_CONTEXT\n"
    text = section("build", "claude", context(prose=prose))
    assert "```\n## Review\nKEEP_CONTEXT" in text
