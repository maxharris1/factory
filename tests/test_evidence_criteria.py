"""Evidence bundle: the issue and its acceptance criteria (#162)."""

from __future__ import annotations

import pytest

from lib.dispatcher import Comment
from lib.evidence import TEXT_LIMIT, Criterion, criteria_evidence

TRUSTED = {"github": {"factory-app[bot]": frozenset({"grok-bot"}), "claude-app[bot]": frozenset({"claude-bot"})}}

TICKET = """# Some ticket

## What
Do the thing.

## Acceptance criteria

- [ ] First criterion.
- [x] Second criterion.

## Notes
- not a criterion
"""


def _brief(author: str, body: str, *, sender: str = "grok-bot", at: float = 1.0, cid: str = "1") -> Comment:
    header = f"<!-- orch:v1 kind=brief from={sender} role=orchestrator risk=high -->"
    return Comment(
        id=cid,
        author=author,
        body=f"{header}\n{body}",
        created_at=at,
        url=f"https://github.com/o/r/pull/7#issuecomment-{cid}",
    )


def _bundle(**overrides):
    args = {
        "issue_key": "162",
        "issue_url": "https://github.com/o/r/issues/162",
        "comments": (),
        "trusted_logins": TRUSTED,
        "issue_body": None,
        "pr_body": None,
        "ready_starts_review": False,
    }
    args.update(overrides)
    return criteria_evidence(**args)


def test_issue_fields_carry_number_and_url():
    bundle = _bundle(issue_body=TICKET)
    assert bundle.issue_key == "162"
    assert bundle.issue_url == "https://github.com/o/r/issues/162"


def test_linear_key_is_the_issue_number():
    bundle = _bundle(issue_key="AUT0-12", issue_url="https://linear.app/aut0/issue/AUT0-12", issue_body=TICKET)
    assert bundle.issue_key == "AUT0-12"


def test_trusted_brief_wins_over_issue_and_pr():
    brief = _brief("factory-app[bot]", "## Acceptance criteria\n- [ ] From the brief.\n")
    bundle = _bundle(
        comments=(brief,), issue_body=TICKET, pr_body="- [ ] From the PR.", ready_starts_review=True
    )
    assert bundle.source == "brief"
    assert bundle.source_url == brief.url
    assert bundle.criteria == (Criterion("ac-1", "From the brief."),)


def test_issue_body_when_no_trusted_brief():
    bundle = _bundle(issue_body=TICKET, pr_body="- [ ] From the PR.", ready_starts_review=True)
    assert bundle.source == "issue"
    assert bundle.source_url == "https://github.com/o/r/issues/162"
    assert [c.text for c in bundle.criteria] == ["First criterion.", "Second criterion."]


def test_pr_description_only_for_ready_starts_review():
    pr = "## Acceptance criteria\n1. From the PR.\n"
    assert _bundle(pr_body=pr, ready_starts_review=True).source == "pr"
    assert _bundle(pr_body=pr, ready_starts_review=True).criteria == (Criterion("ac-1", "From the PR."),)
    none = _bundle(pr_body=pr, ready_starts_review=False)
    assert none.source == "none"
    assert none.criteria == ()
    assert none.text == ""


def test_blank_source_falls_through():
    brief = _brief("factory-app[bot]", "   \n")
    bundle = _bundle(comments=(brief,), issue_body="  ", pr_body=TICKET, ready_starts_review=True)
    assert bundle.source == "pr"


def test_outsider_brief_before_trusted_one_picks_trusted():
    outsider = _brief("mallory", "## Acceptance criteria\n- [ ] Ship it now.\n", at=1.0, cid="1")
    trusted = _brief("factory-app[bot]", "## Acceptance criteria\n- [ ] The real one.\n", at=2.0, cid="2")
    bundle = _bundle(comments=(outsider, trusted), issue_body=TICKET)
    assert bundle.source == "brief"
    assert bundle.source_url == trusted.url
    assert bundle.criteria == (Criterion("ac-1", "The real one."),)


def test_trusted_login_speaking_for_another_bot_is_not_the_brief():
    forged = _brief("claude-app[bot]", "## Acceptance criteria\n- [ ] Forged.\n", sender="grok-bot")
    other = _brief("factory-app[bot]", "## Acceptance criteria\n- [ ] Forged.\n", sender="claude-bot")
    bundle = _bundle(comments=(forged, other), issue_body=TICKET)
    assert bundle.source == "issue"


def test_first_trusted_brief_is_canonical():
    first = _brief("factory-app[bot]", "- [ ] First brief.\n", at=1.0, cid="1")
    second = _brief("factory-app[bot]", "- [ ] Second brief.\n", at=2.0, cid="2")
    assert _bundle(comments=(second, first)).criteria == (Criterion("ac-1", "First brief."),)


def test_non_brief_handoffs_and_plain_comments_are_skipped():
    ready = Comment(id="1", author="factory-app[bot]", body="<!-- orch:v1 kind=go from=grok-bot -->\n- [ ] no", created_at=1.0)
    plain = Comment(id="2", author="factory-app[bot]", body="kind=brief\n- [ ] no", created_at=2.0)
    assert _bundle(comments=(ready, plain), issue_body=TICKET).source == "issue"


def test_criteria_ids_are_stable_and_ordered():
    text = "## Acceptance criteria\n- [ ] a\n* [X] b\n- c\n2) d\n"
    first = _bundle(issue_body=text)
    second = _bundle(issue_body=text)
    assert first.criteria == second.criteria
    assert [c.id for c in first.criteria] == ["ac-1", "ac-2", "ac-3", "ac-4"]
    assert [c.text for c in first.criteria] == ["a", "b", "c", "d"]


def test_section_stops_at_next_heading_and_closing_fence():
    brief_body = "### Ticket\n```\n# T\n\n## Acceptance criteria\n\n- [ ] Inside.\n```\n\n- [ ] Outside.\n"
    bundle = _bundle(comments=(_brief("factory-app[bot]", brief_body),))
    assert bundle.criteria == (Criterion("ac-1", "Inside."),)
    assert "Outside" not in bundle.text
    assert "not a criterion" not in _bundle(issue_body=TICKET).text


def test_without_a_section_only_checkboxes_count():
    bundle = _bundle(issue_body="Intro\n- plain bullet\n- [ ] Box one.\n")
    assert bundle.criteria == (Criterion("ac-1", "Box one."),)
    assert bundle.text.startswith("Intro")


def test_short_text_is_not_cut():
    bundle = _bundle(issue_body=TICKET)
    assert bundle.cut is None
    assert len(bundle.text) < TEXT_LIMIT


@pytest.mark.parametrize("limit", [TEXT_LIMIT, 200])
def test_long_text_is_cut_from_the_end_and_says_what_it_cut(limit):
    items = [f"- [ ] Criterion number {n} with some padding text." for n in range(1, 2001)]
    body = "## Acceptance criteria\n" + "\n".join(items) + "\n"
    bundle = _bundle(issue_body=body, limit=limit)
    assert len(bundle.text) <= limit
    assert bundle.text.startswith("## Acceptance criteria\n- [ ] Criterion number 1 ")
    kept = len(bundle.criteria)
    assert 0 < kept < 2000
    assert bundle.criteria[-1] == Criterion(f"ac-{kept}", f"Criterion number {kept} with some padding text.")
    assert bundle.cut is not None
    assert f"{2000 - kept} criteria" in bundle.cut
    assert bundle.text.endswith(bundle.cut)
    assert "from the end" in bundle.cut
    full = len(body.strip())
    cut_chars = int(bundle.cut.split()[1])
    assert cut_chars == full - len(bundle.text[: -len(bundle.cut)].rstrip("\n"))


def test_one_long_line_is_cut_mid_line():
    body = "x" * (TEXT_LIMIT * 2)
    bundle = _bundle(issue_body=body)
    assert len(bundle.text) <= TEXT_LIMIT
    assert bundle.text.startswith("xxx")
    assert bundle.cut is not None


def test_default_limit_is_the_existing_cap():
    from lib.brief import TEXT_LIMIT as BRIEF_LIMIT

    assert TEXT_LIMIT == BRIEF_LIMIT == 20_000
