"""The live scenarios a builder fixed before the code existed, for QA (#761).

A pure function: the caller passes only comments it already trusts, and this
decides nothing about trust. The scenarios come from the latest `kind=plan`
comment's `## Verify, live` section. When there is no plan, or its section is
missing or empty, they come from the latest `kind=ready` comment's. A plan
whose section is invalid is returned as invalid; a later ready does not
replace it.

The section starts at the first line that is exactly `## Verify, live`
(trailing spaces allowed) outside a ``` or ~~~ code fence, and runs to the
next level 1 or 2 ATX heading outside a fence, or the end. In it, a line
starting `- ` opens an entry, an indented line continues the current one,
and a blank line is skipped. Any other line makes the section invalid at
that line. An entry's text is its lines stripped and joined with one space,
and is one of:

- `- qa-<n> (ac-<n>): <steps> Pass: <test> Saves: <list>`. `Pass:` is
  required; `Saves:` is optional, and its list is split on commas without a
  trailing period. The pack is the id after the word `pack` in the steps.
  A repeated `qa-<n>` makes the section invalid at the repeat.
- `- Regression lane: <text>`, naming the `qa-<n>` ids to rerun on base.
- `- Not live: <text>`, listing `ac-<n> (<reason>)` pairs. A reason may hold
  balanced parentheses; any text outside the pairs, beyond one `.`, `,` or
  `;` after each, makes the section invalid.

Any other entry makes the section invalid at its first line. A text with no
section reads as no scenarios.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace

from lib.dispatcher import Comment, _comment_order
from lib.handoff import HandoffError, parse_comment

HEADING = "## Verify, live"
SOURCE_KINDS = ("plan", "ready")

_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*$")
_SECTION_END = re.compile(r"^ {0,3}#{1,2}(?:[ \t]|$)")
_PACK = re.compile(r"\bpack ([A-Za-z0-9][\w-]*)")
_QA_ID = re.compile(r"\bqa-[1-9][0-9]*\b")
_NOT_LIVE_PAIR = re.compile(r"(ac-[1-9][0-9]*) \(")
_NOT_LIVE_GAP = re.compile(r"[.,;]?(?:\s+|$)")
_NOT_A_PAIR = "not an ac-<n> (<reason>) pair"
_UNKNOWN_ENTRY = "not a scenario, regression lane or not-live entry"
_STRAY_LINE = "not a `- ` entry or an indented line continuing one"


@dataclass(frozen=True)
class Scenario:
    id: str
    criterion: str
    steps: str
    passes: str
    pack: str | None
    saves: tuple[str, ...]


@dataclass(frozen=True)
class NotLive:
    criterion: str
    reason: str


@dataclass(frozen=True)
class LiveScenarios:
    scenarios: tuple[Scenario, ...] = ()
    regression: tuple[str, ...] = ()
    not_live: tuple[NotLive, ...] = ()
    source_url: str | None = None


@dataclass(frozen=True)
class InvalidSection:
    line: int
    text: str
    reason: str
    source_url: str | None = None


@dataclass(frozen=True)
class _Entry:
    line: int
    first: str
    text: str


def _scenario(match: re.Match[str]) -> LiveScenarios | str:
    id, criterion, rest = match.groups()
    steps, has_pass, after = rest.partition("Pass:")
    if not has_pass:
        return f"{id} has no Pass: line"
    steps = steps.strip()
    passes, _, saves = after.partition("Saves:")
    pack = _PACK.search(steps)
    return LiveScenarios(
        scenarios=(
            Scenario(
                id=id,
                criterion=criterion,
                steps=steps,
                passes=passes.strip(),
                pack=pack.group(1) if pack else None,
                saves=tuple(item.strip() for item in saves.strip().removesuffix(".").split(",") if item.strip()),
            ),
        )
    )


def _regression(match: re.Match[str]) -> LiveScenarios | str:
    return LiveScenarios(regression=tuple(_QA_ID.findall(match.group(1))))


def _not_live(match: re.Match[str]) -> LiveScenarios | str:
    body = match.group(1).strip()
    if not body:
        return f"{_NOT_A_PAIR}: nothing after Not live:"
    pairs: list[NotLive] = []
    at = 0
    while at < len(body):
        pair = _NOT_LIVE_PAIR.match(body, at)
        if pair is None:
            return f"{_NOT_A_PAIR}: {body[at:]}"
        depth = 0
        for close in range(pair.end() - 1, len(body)):
            depth += {"(": 1, ")": -1}.get(body[close], 0)
            if depth == 0:
                break
        else:
            return f"unclosed ( in {body[at:]}"
        pairs.append(NotLive(pair.group(1), body[pair.end() : close]))
        gap = _NOT_LIVE_GAP.match(body, close + 1)
        if gap is None:
            return f"{_NOT_A_PAIR}: {body[close + 1 :]}"
        at = gap.end()
    return LiveScenarios(not_live=tuple(pairs))


_SHAPES: tuple[tuple[re.Pattern[str], Callable[[re.Match[str]], LiveScenarios | str]], ...] = (
    (re.compile(r"- (qa-[1-9][0-9]*) \((ac-[1-9][0-9]*)\):(.*)"), _scenario),
    (re.compile(r"- Regression lane:(.*)"), _regression),
    (re.compile(r"- Not live:(.*)"), _not_live),
)


def parse_live(body: str) -> LiveScenarios | InvalidSection:
    """The `## Verify, live` section of one text. See the module doc."""
    found = LiveScenarios()
    for entry in _entries(_section(body.splitlines())):
        if isinstance(entry, InvalidSection):
            return entry
        part: LiveScenarios | str = _UNKNOWN_ENTRY
        for pattern, handler in _SHAPES:
            match = pattern.fullmatch(entry.text)
            if match is not None:
                part = handler(match)
                break
        if isinstance(part, str):
            return InvalidSection(entry.line, entry.first, part)
        seen = {scenario.id for scenario in found.scenarios}
        repeated = [scenario.id for scenario in part.scenarios if scenario.id in seen]
        if repeated:
            return InvalidSection(entry.line, entry.first, f"duplicate id {repeated[0]}")
        found = LiveScenarios(
            scenarios=found.scenarios + part.scenarios,
            regression=found.regression + part.regression,
            not_live=found.not_live + part.not_live,
        )
    return found


def live_scenarios(comments: Sequence[Comment]) -> LiveScenarios | InvalidSection:
    """The latest plan's section, else the latest ready's. See the module doc."""
    latest: dict[str, Comment] = {}
    for comment in sorted(comments, key=_comment_order):
        try:
            kind = parse_comment(comment.body).as_dict()["kind"]
        except HandoffError:
            continue
        if kind in SOURCE_KINDS:
            latest[kind] = comment
    for kind in SOURCE_KINDS:
        comment = latest.get(kind)
        if comment is None:
            continue
        found = parse_live(comment.body)
        if found != LiveScenarios():
            return replace(found, source_url=comment.url)
    return LiveScenarios()


def _section(lines: list[str]) -> list[tuple[int, str]]:
    section: list[tuple[int, str]] | None = None
    fence: str | None = None
    for number, line in enumerate(lines, start=1):
        outside = fence is None
        fence = _fence_after(fence, line)
        if section is not None:
            if outside and _SECTION_END.match(line):
                return section
            section.append((number, line))
        elif outside and line.rstrip() == HEADING:
            section = []
    return section or []


def _fence_after(fence: str | None, line: str) -> str | None:
    if fence is None:
        opened = _FENCE_OPEN.match(line)
        return opened.group(1) if opened else None
    closed = _FENCE_CLOSE.match(line)
    if closed and closed.group(1)[0] == fence[0] and len(closed.group(1)) >= len(fence):
        return None
    return fence


def _entries(section: list[tuple[int, str]]) -> Iterator[_Entry | InvalidSection]:
    current: list[tuple[int, str]] = []
    for number, line in section:
        if not line.strip():
            continue
        if line.startswith("- "):
            if current:
                yield _entry(current)
            current = [(number, line)]
        elif line[0] in " \t" and current:
            current.append((number, line))
        else:
            if current:
                yield _entry(current)
            yield InvalidSection(number, line, _STRAY_LINE)
            return
    if current:
        yield _entry(current)


def _entry(lines: list[tuple[int, str]]) -> _Entry:
    return _Entry(lines[0][0], lines[0][1], " ".join(line.strip() for _, line in lines))
