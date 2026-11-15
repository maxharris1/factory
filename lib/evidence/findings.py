"""Earlier blocking findings and the reviewed head, for the evidence bundle (#164).

A pure function: the caller reads the PR's comments and the base it recorded
for each review round, and passes them in. Round 1 has nothing to carry.

Only the reviewer's earlier verdicts count: a `kind=verdict` comment with
`role=reviewer`, `from=<reviewer>` and a `round` before the current one,
whose author trusted_logins lets speak for that reviewer. When one round has
more than one verdict, the last one counts, as a corrected repost replaces a
rejected one. The caller passes only verdicts the dispatcher accepted.

Each verdict is read in round order as CommonMark. A list item whose leading
paragraph opens with `[blocking] f-<n>` raises or repeats that finding; one
that opens with `[closed] f-<n>` closes it. Markup around the id, as in
`**f-2: ...**`, is allowed. `[minor]` items and `ac-<n>` blockers are not
findings, and nothing inside a code block is read. The finding's text is that
paragraph after the tag, and its rule is what follows `Rule:` in it, up to the
end of that sentence. An APPROVE closes every finding open before it. A
finding raised again keeps its id and takes the later round's text and rule.

The open findings come out ordered by number. The reviewed head is the last
counted verdict's head, and the reviewed base is the base recorded for that
verdict's round, else None.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from markdown_it import MarkdownIt
from markdown_it.token import Token

from lib.dispatcher import Comment, trusted_bots
from lib.handoff import HandoffError, parse_comment

_TAGGED = re.compile(r"^\[(blocking|closed)\]\s+([*_`]*f-([1-9][0-9]*)(?=[*_`]*(?:\W|$)).*)$", re.IGNORECASE)
_RULE = re.compile(r"\brule:\s*(.+?)(?:\.(?:\s|$)|$)", re.IGNORECASE)
_ROUND = re.compile(r"^([1-9][0-9]*)")
_MARKDOWN = MarkdownIt("commonmark")
_CRITERION = re.compile(
    r"^(?P<tag>\[blocking\]\s+)?[*_`]*ac-(?P<n>[1-9][0-9]*)(?![0-9])[*_`]*\s*"
    r"(?:[:,|\u2013\u2014-]\s*)?[*_`]*(?P<status>unmet\b)?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Finding:
    id: str
    rule: str | None
    text: str
    round: int
    url: str | None


@dataclass(frozen=True)
class FindingsEvidence:
    findings: tuple[Finding, ...]
    reviewed_head: str | None
    reviewed_base: str | None
    verdict_url: str | None


def findings_evidence(
    *,
    comments: Sequence[Comment],
    trusted_logins: Mapping[str, Mapping[str, frozenset[str]]],
    reviewer: str,
    current_round: int,
    bases: Mapping[int, str],
) -> FindingsEvidence:
    """The open earlier findings and the last reviewed head. See the module doc."""
    verdicts = _verdicts(comments, trusted_logins, reviewer, current_round)
    open_findings: dict[int, Finding] = {}
    for number, (fields, body, comment) in sorted(verdicts.items()):
        if fields.get("verdict") == "APPROVE":
            open_findings.clear()
        for tag, n, text in _tagged_items(body):
            if tag == "closed":
                open_findings.pop(n, None)
                continue
            rule = _RULE.search(text)
            open_findings[n] = Finding(
                id=f"f-{n}",
                rule=rule.group(1).strip() if rule else None,
                text=text,
                round=number,
                url=comment.url,
            )
    if not verdicts:
        return FindingsEvidence((), None, None, None)
    last_round = max(verdicts)
    fields, _, comment = verdicts[last_round]
    return FindingsEvidence(
        findings=tuple(open_findings[n] for n in sorted(open_findings)),
        reviewed_head=fields.get("head"),
        reviewed_base=bases.get(last_round),
        verdict_url=comment.url,
    )


def _verdicts(
    comments: Sequence[Comment],
    trusted_logins: Mapping[str, Mapping[str, frozenset[str]]],
    reviewer: str,
    current: int,
) -> dict[int, tuple[dict[str, str], str, Comment]]:
    """The last trusted reviewer verdict of each earlier round, keyed by round."""
    found: dict[int, tuple[dict[str, str], str, Comment]] = {}
    for comment in sorted(comments, key=lambda item: item.created_at):
        try:
            handoff = parse_comment(comment.body)
        except HandoffError:
            continue
        fields = handoff.as_dict()
        number = _ROUND.match(fields.get("round", ""))
        if (
            fields["kind"] == "verdict"
            and fields.get("role") == "reviewer"
            and fields.get("from") == reviewer
            and reviewer in trusted_bots(trusted_logins, comment.author)
            and number is not None
            and int(number.group(1)) < current
        ):
            found[int(number.group(1))] = (fields, handoff.body, comment)
    return found


def blocking_ids(body: str) -> frozenset[str]:
    found: set[str] = set()
    for entry in _entries(body):
        finding = _TAGGED.match(entry)
        if finding is not None and finding.group(1).lower() == "blocking":
            found.add(f"f-{finding.group(3)}")
        criterion = _CRITERION.match(entry)
        if criterion is not None and (criterion["tag"] or criterion["status"]):
            found.add(f"ac-{criterion['n']}")
    return frozenset(found)


def _entries(body: str) -> list[str]:
    tokens = _MARKDOWN.parse(body)
    entries: list[str] = []
    quoted = 0
    for index, token in enumerate(tokens):
        if token.type == "blockquote_open":
            quoted += 1
        elif token.type == "blockquote_close":
            quoted -= 1
        elif token.type == "inline" and not quoted:
            entries.extend(_list_item_lead(tokens, index) + _table_rows(token))
    return entries


def _list_item_lead(tokens: list[Token], index: int) -> list[str]:
    opener = [token.type for token in tokens[max(index - 2, 0) : index]]
    opens_item = opener == ["list_item_open", "paragraph_open"]
    return [" ".join(tokens[index].content.split())] if opens_item else []


def _table_rows(inline: Token) -> list[str]:
    if inline.level != 1:
        return []
    rows = (line.strip() for line in inline.content.splitlines())
    return [row[1:].strip() for row in rows if row.startswith("|")]


def _tagged_items(body: str) -> list[tuple[str, int, str]]:
    """Each `[blocking]` or `[closed]` finding item as (tag, number, text)."""
    tokens = _MARKDOWN.parse(body)
    items = []
    for index, token in enumerate(tokens[:-2]):
        if token.type != "list_item_open" or tokens[index + 1].type != "paragraph_open":
            continue
        paragraph = " ".join(tokens[index + 2].content.split())
        match = _TAGGED.match(paragraph)
        if match is not None:
            items.append((match.group(1).lower(), int(match.group(3)), match.group(2)))
    return items
