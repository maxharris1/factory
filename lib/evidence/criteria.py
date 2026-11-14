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

When the source has an "Acceptance criteria" heading outside any code block,
the text is the first such section, up to the next heading of the same or a
higher level outside any code block. Code blocks follow CommonMark: they do
not nest, and a fence closes only on the opening character, at least as long,
with no info string. A fence or list marker indented four or more spaces past
its list item's content is indented code, and a fence opened in a list item
ends with the item. A code example inside the section stays in the text, and
a heading or list item inside a code example is ignored. Its list items are
the criteria. Without one, the text is the whole source and only its checkbox
items outside code blocks are criteria. Criteria are numbered `ac-1`, `ac-2`,
... in order, so the same text always gets the same ids.

The text is capped at TEXT_LIMIT characters, the cap lib/brief.py holds.
A longer text is cut from the end, at a line break when there is one, and
ends with a note of how many characters and criteria were cut.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from lib.brief import TEXT_LIMIT
from lib.dispatcher import Comment, trusted_bots
from lib.handoff import HandoffError, parse_comment

ORCHESTRATOR = "grok-bot"
SOURCES = ("brief", "issue", "pr", "none")

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_AC_HEADING = re.compile(r"^acceptance criteria\b", re.IGNORECASE)
_TICKET_HEADING = re.compile(r"^ticket$", re.IGNORECASE)
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
_CHECKBOX = re.compile(r"^\s*[-*+]\s+\[[ xX]\]\s+(.+?)\s*$")
_ITEM = re.compile(r"^\s*(?:[-*+]\s+(?:\[[ xX]\]\s+)?|[0-9]+[.)]\s+)(.+?)\s*$")
_MARKER = re.compile(r"^([-*+]|[0-9]{1,9}[.)])( *)(?=\S|$)")
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
        text, pattern = "\n".join(lines).strip(), _CHECKBOX
    else:
        text, pattern = "\n".join(section).strip(), _ITEM
    total = _items(text, pattern)
    cut = None
    if len(text) > limit:
        budget = max(limit - _NOTE_ROOM, 0)
        end = text.rfind("\n", 0, budget + 1)
        kept = text[: end if end > 0 else budget].rstrip()
        items = _items(kept, pattern)
        cut = f"[cut {len(text) - len(kept)} characters and {len(total) - len(items)} criteria from the end]"
        text = f"{kept}\n{cut}"
    else:
        items = total
    criteria = tuple(Criterion(f"ac-{n}", item) for n, item in enumerate(items, start=1))
    return text, criteria, cut


def _quoted_ticket(body: str) -> str:
    lines = body.splitlines()
    code, blocks = _scan(lines)
    for index, line in enumerate(lines):
        heading = _HEADING.match(line)
        if heading is not None and not code[index] and _TICKET_HEADING.match(heading.group(2)) and index + 1 in blocks:
            return "\n".join(lines[index + 2 : blocks[index + 1]])
    return body


def _section(lines: list[str]) -> list[str] | None:
    code, _ = _scan(lines)
    headings = [(index, _HEADING.match(line)) for index, line in enumerate(lines)]
    starts = [index for index, heading in headings if heading and not code[index] and _AC_HEADING.match(heading.group(2))]
    if not starts:
        return None
    start = starts[0]
    level = len(headings[start][1].group(1))
    for index in range(start + 1, len(lines)):
        heading = headings[index][1]
        if heading is not None and not code[index] and len(heading.group(1)) <= level:
            return lines[start:index]
    return lines[start:]


def _scan(lines: list[str]) -> tuple[list[bool], dict[int, int]]:
    """Which lines start no list item or heading, and each fenced block's opening line to its end.

    Those lines are code and lazy paragraph lines. A fence or list marker
    counts only when indented at most three spaces past the content column of
    the list item it sits in; deeper, it is indented code or paragraph text.
    A fence opened inside a list item ends with that item. A block's end is
    its closing fence line, else the first line past it; an unclosed block
    runs to the end.
    """
    inert = [False] * len(lines)
    blocks: dict[int, int] = {}
    columns: list[int] = []
    fence: tuple[int, str, int] | None = None
    blank_before = True
    for index, raw in enumerate(lines):
        line = raw.expandtabs(4)
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        blank = not stripped
        if fence is not None:
            start, marker, column = fence
            if blank or indent >= column:
                inert[index] = True
                close = _FENCE.match(stripped)
                if (
                    close is not None
                    and indent - column <= 3
                    and close.group(1)[0] == marker[0]
                    and len(close.group(1)) >= len(marker)
                    and not close.group(2).strip()
                ):
                    blocks[start] = index
                    fence = None
                continue
            blocks[start] = index
            fence = None
        if blank:
            blank_before = True
            continue
        opener = _FENCE.match(stripped)
        item = _MARKER.match(stripped)
        outer = [column for column in columns if column <= indent]
        if blank_before or (
            (opener or item or _HEADING.match(stripped)) and indent - (outer[-1] if outer else 0) <= 3
        ):
            columns = outer
        blank_before = False
        base = columns[-1] if columns else 0
        if indent < base or indent - base > 3:
            inert[index] = True
        elif opener is not None and not (opener.group(1)[0] == "`" and "`" in opener.group(2)):
            inert[index] = True
            fence = (index, opener.group(1), base)
        elif item is not None:
            gap = len(item.group(2))
            columns.append(indent + len(item.group(1)) + (gap if 1 <= gap <= 4 else 1))
    if fence is not None:
        blocks[fence[0]] = len(lines)
    return inert, blocks


def _items(text: str, pattern: re.Pattern[str]) -> list[str]:
    lines = text.splitlines()
    inert, _ = _scan(lines)
    return [match.group(1) for line, skip in zip(lines, inert) if not skip and (match := pattern.match(line))]
