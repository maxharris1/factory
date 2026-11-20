"""The issue and its acceptance criteria, for the evidence bundle (#162).

A pure function: the caller reads the issue, its comments and the PR, and
passes them in. The criteria text comes from the first source that has any:

1. the issue body, when its "Acceptance criteria" section lists any, so an edit
   made after the brief went up, such as a scope cut, is what gets reviewed
   (#631);
2. the canonical brief: the latest `kind=brief` comment with `from=grok-bot`
   and `role=orchestrator` whose author trusted_logins lets speak for
   grok-bot (with the four GitHub Apps, the Factory App login). A brief from
   anyone else is skipped, even when it comes last. The poll picks it once
   (TrackedTicket.brief_comment), so when the issue body has no criteria
   section, the builder's brief and the reviewer's criteria name the same
   comment (#495);
3. the issue body without that section;
4. the PR description, only for a `ready_starts_review` project, whose PRs
   are built from a packet with no brief.

A brief that quotes the ticket in a code block under a "Ticket" heading, as
the feeder's does, is read as that quoted ticket alone, so the factory.md
prose after it never supplies criteria.

The source is parsed as CommonMark. When it has an "Acceptance criteria"
heading outside any list, quote or code block, the text is the first such
section, up to the next such heading of the same or a higher level, or the
next "Out of scope" or "Assumed environment" heading of level 2 to 4. A
top-level paragraph whose first line is only a bold "Acceptance criteria"
label, as lib.orchestrate.brief_comment writes, counts as a heading below
level 6: its section ends at the next heading of any level or the next bold
label. A code
example inside the section stays in the text. The criteria are the section's
list items, each read from the first line of its leading paragraph, or from
its source line after the list marker when it opens with another block such
as a quote, heading or nested list; an item that opens with a code block, and
any item inside a code block, is not one, and a line yields one item at most.
Without the heading, the text is the whole source and only its bulleted
checkbox items are criteria. Criteria are numbered `ac-1`, `ac-2`, ... in
order, so the same text always gets the same ids.

The text is capped at TEXT_LIMIT characters, the cap lib/brief.py holds.
A longer text is cut from the end, at a line break when there is one, and
ends with a note of how many characters and criteria were cut.

When no source has any text, the source is "none" and the text is EMPTY.

qa_criteria holds the ids under the brief's `### QA criteria`, () when its QA
plan lists none, and None when there is no brief, it has no valid QA plan, or
the issue body lists criteria that no longer match the plan's QA criteria.
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
from lib.journeys import QaPlan

ORCHESTRATOR = "grok-bot"
SOURCES = ("brief", "issue", "pr", "none")

_AC_HEADING = re.compile(r"^acceptance criteria\b", re.IGNORECASE)
_END_HEADING = re.compile(r"^(?:\*\*|__)?(assumed\s+environment|out\s+of\s+scope)(?:\*\*|__)?:?$", re.IGNORECASE)
_TICKET_HEADING = re.compile(r"^ticket$", re.IGNORECASE)
_CHECKBOX = re.compile(r"^\[[ xX]\]\s+(.+)$")
_BULLETS = frozenset("-*+")
_MARKER = re.compile(r"^(?:[ \t]*>)*[ \t]*(?:[-*+]|\d{1,9}[.)])")
_MARKDOWN = MarkdownIt("commonmark")
_NOTE_ROOM = 80
_LABEL_LEVEL = 7
EMPTY = (
    "No acceptance criteria: no trusted brief, and the issue body and "
    "PR description are empty or were not read."
)


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
    qa_criteria: tuple[str, ...] | None = None


def criteria_evidence(
    *,
    issue_key: str,
    issue_url: str | None,
    brief: Comment | None,
    issue_body: str | None,
    pr_body: str | None,
    ready_starts_review: bool,
    pr_url: str | None = None,
    limit: int = TEXT_LIMIT,
) -> CriteriaEvidence:
    """The issue fields and acceptance criteria. See the module doc."""
    candidates: list[tuple[str, str | None, str | None]] = []
    qa_criteria = None
    listed = _lists_criteria(issue_body)
    if listed:
        candidates.append(("issue", issue_url, issue_body))
    if brief is not None:
        brief_body = parse_comment(brief.body).body
        plan = QaPlan.parse(brief_body)
        if plan is not None and not (listed and plan.edited((c.id, c.text) for c in numbered(issue_body))):
            qa_criteria = tuple(check.ac for check in plan.checks)
        candidates.append(("brief", brief.url, quoted_ticket(brief_body)))
    candidates.append(("issue", issue_url, issue_body))
    if ready_starts_review:
        candidates.append(("pr", pr_url, pr_body))
    for source, url, body in candidates:
        if body is not None and body.strip():
            text, criteria, cut = _criteria(body, limit)
            return CriteriaEvidence(issue_key, issue_url, source, url, text, criteria, cut, qa_criteria)
    return CriteriaEvidence(issue_key, issue_url, "none", None, EMPTY, (), None, qa_criteria)


def numbered(body: str) -> tuple[Criterion, ...]:
    """The evidence ids criteria_evidence gives body's criteria, each with its whole leading paragraph."""
    return _criteria(body, TEXT_LIMIT, whole=True)[1]


def issue_criteria(body: str | None) -> tuple[Criterion, ...] | None:
    """body's numbered criteria when its Acceptance criteria section lists any, else None."""
    return numbered(body) if body is not None and _lists_criteria(body) else None


def _lists_criteria(body: str | None) -> bool:
    section = _section(body.splitlines()) if body is not None else None
    return section is not None and bool(_items(_trim(section), False))


def canonical_brief(
    comments: Sequence[Comment],
    trusted_logins: Mapping[str, Mapping[str, frozenset[str]]],
) -> Comment | None:
    """The latest brief a trusted orchestrator login posted, else None."""
    latest: Comment | None = None
    for comment in sorted(comments, key=lambda item: (item.created_at, item.id)):
        try:
            fields = parse_comment(comment.body).as_dict()
        except HandoffError:
            continue
        if (
            fields["kind"] == "brief"
            and fields.get("from") == ORCHESTRATOR
            and fields.get("role") == "orchestrator"
            and ORCHESTRATOR in trusted_bots(trusted_logins, comment.author)
        ):
            latest = comment
    return latest


def _criteria(body: str, limit: int, *, whole: bool = False) -> tuple[str, tuple[Criterion, ...], str | None]:
    lines = body.splitlines()
    section = _section(lines)
    if section is None:
        text, checkboxes = _trim(lines), True
    else:
        text, checkboxes = _trim(section), False
    total = _items(text, checkboxes, whole=whole)
    cut = None
    if len(text) > limit:
        budget = max(limit - _NOTE_ROOM, 0)
        end = text.rfind("\n", 0, budget + 1)
        kept = text[: end if end > 0 else budget].rstrip()
        items = _items(kept, checkboxes, whole=whole)
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


def quoted_ticket(body: str) -> str:
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


def _labels(tokens: list[Token]) -> list[tuple[int, int, int, str]]:
    """Each top-level paragraph whose first line is only one bold span, as a heading below level 6."""
    labels = []
    for index, token in enumerate(tokens[:-1]):
        if token.type != "paragraph_open" or token.level != 0 or token.map is None:
            continue
        children = [
            child
            for child in tokens[index + 1].children or []
            if child.type != "text" or child.content
        ]
        kinds = [child.type for child in children[:4]]
        if kinds[:3] == ["strong_open", "text", "strong_close"] and kinds[3:] in ([], ["softbreak"]):
            labels.append((index, token.map[0], _LABEL_LEVEL, children[1].content.strip()))
    return labels


def _section(lines: list[str]) -> list[str] | None:
    tokens = _parse(lines)
    headings = sorted(_headings(tokens) + _labels(tokens), key=lambda item: item[1])
    starts = [(line, level) for _, line, level, title in headings if _AC_HEADING.match(title)]
    if not starts:
        return None
    start, level = starts[0]
    for _, line, other, title in headings:
        if line > start and (other <= level or (2 <= other <= 4 and _END_HEADING.match(title))):
            return lines[start:line]
    return lines[start:]


def _items(text: str, checkboxes: bool, *, whole: bool = False) -> list[str]:
    lines = text.splitlines()
    tokens = _parse(lines)
    items = []
    seen = set()
    for index, token in enumerate(tokens[:-1]):
        block = tokens[index + 1]
        if token.type != "list_item_open" or token.map is None or token.map[0] in seen:
            continue
        if block.type in ("list_item_close", "fence", "code_block"):
            continue
        if checkboxes and token.markup not in _BULLETS:
            continue
        seen.add(token.map[0])
        if block.type == "paragraph_open":
            content = tokens[index + 2].content
            first = content.split("\n", 1)[0].strip()
        else:
            content = None
            first = _MARKER.sub("", lines[token.map[0]], count=1).strip()
        if _CHECKBOX.match(first) is None and (checkboxes or not first):
            continue
        item = " ".join(line.strip() for line in content.split("\n")) if whole and content is not None else first
        box = _CHECKBOX.match(item)
        items.append(box.group(1).strip() if box is not None else item)
    return items
