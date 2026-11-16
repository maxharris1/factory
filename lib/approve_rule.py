"""An APPROVE ships only with ac=met and no open [blocking] item (#508).

The parser accepts a verdict=APPROVE with no ac key, so verdicts accepted
before this rule still read the same. The rule runs only where a new
verdict is accepted: `orch job done` (lib/handoff_check.py) and T4 for
comments posted after the approve_rule_floor time.

An open blocking item is a list item whose leading paragraph opens with
`[blocking]`, read as CommonMark outside code blocks and blockquotes, as
lib/evidence/findings.py reads findings. `[closed]`, `[minor]` and
`[follow-up]` items do not count.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from markdown_it import MarkdownIt

_MARKDOWN = MarkdownIt("commonmark")
_BLOCKING = re.compile(r"^\[blocking\]\s*[*_`]*(?P<id>(?:f|ac)-[1-9][0-9]*)?", re.IGNORECASE)


@dataclass(frozen=True)
class ApproveRefusal:
    outcome: Literal["rejected-approve-no-ac", "rejected-approve-open-blocking"]
    message: str


def approve_refusal(fields: Mapping[str, str], body: str) -> ApproveRefusal | None:
    if fields.get("kind") != "verdict" or fields.get("verdict") != "APPROVE":
        return None
    if fields.get("ac") != "met":
        return ApproveRefusal(
            "rejected-approve-no-ac",
            "verdict=APPROVE needs ac=met in the header. Add ac=met if every "
            "acceptance criterion is met at this head. If one is not, post "
            "verdict=REVISE with ac=unmet.",
        )
    blocking = open_blocking(body)
    if blocking:
        named = ", ".join(blocking)
        return ApproveRefusal(
            "rejected-approve-open-blocking",
            f"verdict=APPROVE lists open [blocking] items ({named}). Post "
            "verdict=REVISE for them. If a finding no longer blocks, close it "
            "as `[closed] f-<n>. <what shows it closed>.`",
        )
    return None


def open_blocking(body: str) -> tuple[str, ...]:
    tokens = _MARKDOWN.parse(body)
    found: list[str] = []
    quoted = 0
    for index, token in enumerate(tokens):
        if token.type == "blockquote_open":
            quoted += 1
        elif token.type == "blockquote_close":
            quoted -= 1
        elif (
            not quoted
            and token.type == "inline"
            and index >= 2
            and tokens[index - 2].type == "list_item_open"
            and tokens[index - 1].type == "paragraph_open"
        ):
            match = _BLOCKING.match(" ".join(token.content.split()))
            if match is not None:
                found.append(match["id"] or "an untagged item")
    return tuple(found)
