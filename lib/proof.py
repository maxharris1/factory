"""The proof a builder's ready owes for each acceptance criterion (#1032).

The ticket's builder criteria (lib/feeder.py parse_intake) each need one
entry in the ready's `## Proof` section, keyed by the criterion's id: its
own `ac-<n>:` prefix, else `ac-<position>` among the builder criteria. A
`review` criterion's entry gives the command in a code span and its output
in a fenced block. A `qa` criterion's entry gives the verify skill's command
and output and names each journey the criterion binds (lib/journeys.py
qa_tag), or says `unverified` and gives the failed doctor command and its
output instead. A bare `qa` tag binds the ticket's Features journeys, read
from whole lines of the brief's ticket quote, else from the source issue's
full body. When neither shows a Features line, its journeys are None and only
the `unverified` form proves it.

The dispatcher stores what a ticket requires in the job detail under
`proof`; `orch job done` refuses a ready that misses an entry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, get_args

from markdown_it import MarkdownIt
from markdown_it.token import Token

from lib.journeys import qa_tag

Kind = Literal["review", "qa"]

_ID = re.compile(r"^ac-\d+$")
_PREFIX = re.compile(r"^(ac-\d+)\s*:\s*", re.IGNORECASE)
_ENTRY_ID = re.compile(r"^\s*(ac-\d+)\b", re.IGNORECASE)
_UNVERIFIED = re.compile(r"\bunverified\b", re.IGNORECASE)
_CUT = re.compile(r"\n\[cut at \d+ characters\]\Z")
_HEADING = "proof"
_MARKDOWN = MarkdownIt("commonmark")


@dataclass(frozen=True)
class Required:
    id: str
    kind: Kind
    journeys: tuple[str, ...] | None = ()


@dataclass
class _Entry:
    codes: int = 0
    outputs: int = 0
    text: list[str] = field(default_factory=list)
    prose: list[str] = field(default_factory=list)


def required(brief: str, issue_body: str | None = None) -> tuple[Required, ...]:
    """The proof each builder criterion of the brief's ticket needs, in order."""
    from lib.evidence.criteria import quoted_ticket
    from lib.feeder import parse_intake

    ticket = quoted_ticket(brief)
    template = parse_intake(ticket, ())
    every = _journeys(_whole_lines(ticket)) or _journeys(issue_body or "") or None
    items: list[Required] = []
    for position, item in enumerate(_listed_above_quote(brief, ticket) or template.criteria, start=1):
        prefix = _PREFIX.match(item)
        criterion = prefix.group(1).lower() if prefix else f"ac-{position}"
        tag = qa_tag(item[prefix.end():] if prefix else item)
        if tag is None:
            items.append(Required(criterion, "review"))
        else:
            items.append(Required(criterion, "qa", tag.journeys or every))
    return tuple(items)


def _listed_above_quote(brief: str, ticket: str) -> tuple[str, ...]:
    from lib.feeder import parse_intake

    return parse_intake(brief[:brief.find(ticket)], ()).criteria if ticket != brief else ()


def _journeys(ticket: str) -> tuple[str, ...]:
    from lib.feeder import parse_intake

    return tuple(journey.id for journey in parse_intake(ticket, ()).journeys)


def _whole_lines(ticket: str) -> str:
    cut = _CUT.search(ticket)
    return ticket[:cut.start()].rpartition("\n")[0] if cut else ticket


def to_detail(items: tuple[Required, ...]) -> list[dict]:
    return [
        {"id": item.id, "kind": item.kind, "journeys": None if item.journeys is None else list(item.journeys)}
        for item in items
    ]


def from_detail(value: object) -> tuple[Required, ...] | None:
    """The items a job detail's `proof` holds; None when it holds none or is malformed."""
    if not isinstance(value, list):
        return None
    items: list[Required] = []
    for entry in value:
        if not isinstance(entry, dict):
            return None
        criterion, kind, journeys = entry.get("id"), entry.get("kind"), entry.get("journeys")
        if (
            not isinstance(criterion, str)
            or not _ID.fullmatch(criterion)
            or kind not in get_args(Kind)
            or not (journeys is None and kind == "qa" or isinstance(journeys, list))
            or not all(isinstance(journey, str) for journey in journeys or ())
        ):
            return None
        items.append(Required(criterion, kind, None if journeys is None else tuple(journeys)))
    return tuple(items)


def missing(items: tuple[Required, ...], ready_body: str) -> list[str]:
    """Why the ready's Proof section does not prove items; empty when it does."""
    entries = _entries(ready_body)
    absent = [item.id for item in items if item.id not in entries]
    found = [f"ready has no Proof entry for {', '.join(absent)}"] if absent else []
    for item in items:
        entry = entries.get(item.id)
        if entry is not None:
            found.extend(_problems(item, entry))
    return found


def _problems(item: Required, entry: _Entry) -> list[str]:
    name = f"Proof entry {item.id} ({item.kind})"
    shown = entry.codes > 0 and entry.outputs > 0
    text = "\n".join(entry.text)
    if item.kind == "review":
        return [] if shown else [f"{name} needs the command in backticks and its output in a fenced block"]
    if _UNVERIFIED.search("\n".join(entry.prose)):
        return [] if shown else [
            f"{name} is unverified but lacks the doctor command in backticks and its output in a fenced block"
        ]
    if item.journeys is None:
        return [
            f"{name} binds journeys the brief's ticket quote does not show, so write it as unverified "
            "with the doctor command in backticks and its output in a fenced block"
        ]
    found = [] if shown else [
        f"{name} needs the verify-skill command in backticks and its output in a fenced block"
    ]
    unnamed = [
        journey for journey in item.journeys
        if not re.search(rf"(?<![a-z0-9-]){re.escape(journey)}(?![a-z0-9-])", text)
    ]
    if unnamed:
        found.append(f"{name} does not name journey {', '.join(unnamed)}")
    return found


def _entries(body: str) -> dict[str, _Entry]:
    tokens = _MARKDOWN.parse(body or "")
    entries: dict[str, _Entry] = {}
    for start, end in _top_level_items(tokens, _section(tokens)):
        first = tokens[start + 2] if tokens[start + 1].type == "paragraph_open" else None
        match = _ENTRY_ID.match(_inline_text(first)) if first is not None else None
        if match is None or match.group(1).lower() in entries:
            continue
        id_span = _leading_code(first)
        entry = _Entry()
        for token in tokens[start:end]:
            if token.type == "inline":
                entry.codes += sum(
                    child.type == "code_inline" and child is not id_span for child in token.children or ()
                )
                entry.text.append(_inline_text(token))
                entry.prose.append(_inline_text(token, ("text", "softbreak", "hardbreak")))
            elif token.type in ("fence", "code_block"):
                entry.outputs += bool(token.content.strip())
                entry.text.append(token.content)
        entries[match.group(1).lower()] = entry
    return entries


def _section(tokens: list[Token]) -> range:
    level = None
    begin = len(tokens)
    for index, token in enumerate(tokens):
        if token.type != "heading_open" or token.level != 0:
            continue
        depth = int(token.tag[1])
        if level is not None and depth <= level:
            return range(begin, index)
        if level is None and tokens[index + 1].content.strip().lower() == _HEADING:
            level, begin = depth, index + 3
    return range(begin, len(tokens))


def _top_level_items(tokens: list[Token], section: range) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = None
    for index in section:
        token = tokens[index]
        if token.level != 1:
            continue
        if token.type == "list_item_open":
            start = index
        elif token.type == "list_item_close" and start is not None:
            spans.append((start, index))
            start = None
    return spans


def _leading_code(token: Token) -> Token | None:
    first = next((child for child in token.children or () if child.content), None)
    return first if first is not None and first.type == "code_inline" else None


def _inline_text(
    token: Token, kinds: tuple[str, ...] = ("text", "code_inline", "softbreak", "hardbreak")
) -> str:
    return "".join(
        "\n" if child.type in ("softbreak", "hardbreak") else child.content
        for child in token.children or ()
        if child.type in kinds
    )
