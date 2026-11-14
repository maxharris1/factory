"""The issue and its acceptance criteria, for the evidence bundle (#162).

A pure function: the caller reads the issue, its comments and the PR, and
passes them in. The criteria text comes from the first source that has any:

1. the canonical brief: the first `kind=brief` comment with `from=grok-bot`
   whose author trusted_logins lets speak for grok-bot (with the four GitHub
   Apps, the Factory App login). A brief from anyone else is skipped, even
   when it comes first;
2. the issue body;
3. the PR description, only for a `ready_starts_review` project, whose PRs
   are built from a packet with no brief.

A brief that quotes the ticket in a code block under a "Ticket" heading, as
the feeder's does, is read as that quoted ticket alone, so the factory.md
prose after it never supplies criteria.

The source is parsed as CommonMark. When it has an "Acceptance criteria"
heading outside any list, quote or code block, the text is the first such
section, up to the next such heading of the same or a higher level. A code
example inside the section stays in the text. The criteria are the section's
list items, each read from the first line of its leading paragraph; an item
that opens with a code block, and any item inside a code block, is not one.
Without the heading, the text is the whole source and only its bulleted
checkbox items are criteria. Criteria are numbered `ac-1`, `ac-2`, ... in
order, so the same text always gets the same ids.

The text is capped at TEXT_LIMIT characters, the cap lib/brief.py holds.
A longer text is cut from the end, at a line break when there is one, and
ends with a note of how many characters and criteria were cut.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from markdown_it import MarkdownIt
from markdown_it.token import Token

from lib.brief import TEXT_LIMIT
from lib.dispatcher import Comment, trusted_bots
from lib.handoff import HandoffError, parse_comment

ORCHESTRATOR = "grok-bot"
SOURCES = ("brief", "issue", "pr", "none")

_AC_HEADING = re.compile(r"^acceptance criteria\b", re.IGNORECASE)
_TICKET_HEADING = re.compile(r"^ticket$", re.IGNORECASE)
_CHECKBOX = re.compile(r"^\[[ xX]\]\s+(.+)$")
_BULLETS = frozenset("-*+")
_MARKDOWN = MarkdownIt("commonmark")
_NOTE_ROOM = 80


@dataclass(frozen=True)
class Criterion:
    id: str
    text: str


@dataclass(frozen=True)
class CriteriaEvidence:
    issue_key: str
    issue_url: str | None
    source: str
    source_url: str | None
    text: str
    criteria: tuple[Criterion, ...]
    cut: str | None


def criteria_evidence(
    *,
    issue_key: str,
    issue_url: str | None,
    comments: Sequence[Comment],
    trusted_logins: Mapping[str, Mapping[str, frozenset[str]]],
    issue_body: str | None,
    pr_body: str | None,
    ready_starts_review: bool,
    pr_url: str | None = None,
    limit: int = TEXT_LIMIT,
) -> CriteriaEvidence:
    """The issue fields and acceptance criteria. See the module doc."""
    candidates: list[tuple[str, str | None, str | None]] = []
    brief = canonical_brief(comments, trusted_logins)
    if brief is not None:
        candidates.append(("brief", brief.url, _quoted_ticket(parse_comment(brief.body).body)))
    candidates.append(("issue", issue_url, issue_body))
    if ready_starts_review:
        candidates.append(("pr", pr_url, pr_body))
    for source, url, body in candidates:
        if body is not None and body.strip():
            text, criteria, cut = _criteria(body, limit)
            return CriteriaEvidence(issue_key, issue_url, source, url, text, criteria, cut)
    return CriteriaEvidence(issue_key, issue_url, "none", None, "", (), None)


def canonical_brief(
    comments: Sequence[Comment],
    trusted_logins: Mapping[str, Mapping[str, frozenset[str]]],
) -> Comment | None:
    """The first brief a trusted orchestrator login posted, else None."""
    for comment in sorted(comments, key=lambda item: item.created_at):
        try:
            fields = parse_comment(comment.body).as_dict()
        except HandoffError:
            continue
        if (
            fields["kind"] == "brief"
            and fields.get("from") == ORCHESTRATOR
            and ORCHESTRATOR in trusted_bots(trusted_logins, comment.author)
        ):
            return comment
    return None


def _criteria(body: str, limit: int) -> tuple[str, tuple[Criterion, ...], str | None]:
    lines = body.splitlines()
    section = _section(lines)
    if section is None:
        text, checkboxes = _trim(lines), True
    else:
        text, checkboxes = _trim(section), False
    total = _items(text, checkboxes)
    cut = None
    if len(text) > limit:
        budget = max(limit - _NOTE_ROOM, 0)
        end = text.rfind("\n", 0, budget + 1)
        kept = text[: end if end > 0 else budget].rstrip()
        items = _items(kept, checkboxes)
        cut = f"[cut {len(text) - len(kept)} characters and {len(total) - len(items)} criteria from the end]"
        text = f"{kept}\n{cut}"
    else:
        items = total
    criteria = tuple(Criterion(f"ac-{n}", item) for n, item in enumerate(items, start=1))
    return text, criteria, cut


def _trim(lines: list[str]) -> str:
    """The lines without blank lines at either end, keeping the first line's indentation."""
    kept = list(lines)
    while kept and not kept[0].strip():
        kept.pop(0)
    return "\n".join(kept).rstrip()


def _parse(lines: list[str]) -> list[Token]:
    return _MARKDOWN.parse("\n".join(lines))


def _headings(tokens: list[Token]) -> list[tuple[int, int, int, str]]:
    """Each top-level heading as (token index, first line, level, title)."""
    return [
        (index, token.map[0], int(token.tag[1]), tokens[index + 1].content.strip())
        for index, token in enumerate(tokens)
        if token.type == "heading_open" and token.level == 0 and token.map is not None
    ]


def _quoted_ticket(body: str) -> str:
    tokens = _parse(body.splitlines())
    for index, line, _, title in _headings(tokens):
        quote = tokens[index + 3] if index + 3 < len(tokens) else None
        if (
            _TICKET_HEADING.match(title)
            and quote is not None
            and quote.type == "fence"
            and quote.map is not None
            and quote.map[0] == line + 1
        ):
            return quote.content.rstrip("\n")
    return body


def _section(lines: list[str]) -> list[str] | None:
    headings = _headings(_parse(lines))
    starts = [(line, level) for _, line, level, title in headings if _AC_HEADING.match(title)]
    if not starts:
        return None
    start, level = starts[0]
    for _, line, other, _ in headings:
        if line > start and other <= level:
            return lines[start:line]
    return lines[start:]


def _items(text: str, checkboxes: bool) -> list[str]:
    tokens = _parse(text.splitlines())
    items = []
    for index, token in enumerate(tokens[:-2]):
        if token.type != "list_item_open" or tokens[index + 1].type != "paragraph_open":
            continue
        if checkboxes and token.markup not in _BULLETS:
            continue
        first = tokens[index + 2].content.split("\n", 1)[0].strip()
        box = _CHECKBOX.match(first)
        if box is not None:
            items.append(box.group(1).strip())
        elif not checkboxes and first:
            items.append(first)
    return items
