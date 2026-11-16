"""An APPROVE ships only with ac=met and no open [blocking] item (#508).

An open blocking item is a list item whose leading paragraph opens with
`[blocking]`, read as CommonMark outside code blocks and blockquotes.
`[closed]`, `[minor]` and `[follow-up]` items do not count.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from lib.markdown_items import list_item_leads

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
    found: list[str] = []
    for lead in list_item_leads(body):
        match = _BLOCKING.match(lead)
        if match is not None:
            found.append(match["id"] or "an untagged item")
    return tuple(found)
