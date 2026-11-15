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

from lib.dispatcher import Comment, trusted_bots
from lib.handoff import HandoffError, parse_comment

_TAGGED = re.compile(r"^\[(blocking|closed)\]\s+([*_`]*f-([1-9][0-9]*)(?=[*_`]*(?:\W|$)).*)$", re.IGNORECASE)
_RULE = re.compile(r"\brule:\s*(.+?)(?:\.(?:\s|$)|$)", re.IGNORECASE)
_ROUND = re.compile(r"^([1-9][0-9]*)")
_MARKDOWN = MarkdownIt("commonmark")
_UNMET_CRITERION = re.compile(
    r"^(?:\|\s*)?(?:\[blocking\]\s+)?[*_`]*ac-([1-9][0-9]*)(?![0-9])(?=.*\bunmet\b)", re.IGNORECASE
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
    """The f-<n> ids a verdict body marks [blocking], and the ac-<n> ids a list
    item or table row opens with and calls unmet. Code blocks are not read."""
    found = {f"f-{n}" for tag, n, _ in _tagged_items(body) if tag == "blocking"}
    for token in _MARKDOWN.parse(body):
        if token.type != "inline":
            continue
        for line in token.content.splitlines():
            match = _UNMET_CRITERION.match(line.strip())
            if match is not None:
                found.add(f"ac-{match.group(1)}")
    return frozenset(found)


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
