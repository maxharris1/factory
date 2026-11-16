from __future__ import annotations

import pytest

from lib.approve_rule import approve_refusal

APPROVE = {"kind": "verdict", "verdict": "APPROVE", "ac": "met"}


@pytest.mark.parametrize(
    ("fields", "outcome"),
    [
        ({"kind": "verdict", "verdict": "APPROVE"}, "rejected-approve-no-ac"),
        (APPROVE, None),
        ({"kind": "verdict", "verdict": "REVISE"}, None),
        ({"kind": "verdict", "verdict": "BLOCKED"}, None),
        ({"kind": "plan", "ac": "met"}, None),
        ({"kind": "ready"}, None),
    ],
)
def test_the_header_decides_the_ac_refusal(fields: dict[str, str], outcome: str | None) -> None:
    refusal = approve_refusal(fields, "")
    assert (refusal and refusal.outcome) == outcome


@pytest.mark.parametrize(
    ("body", "named"),
    [
        ("1. [blocking] f-2. The ship path skips CI.\n", "f-2"),
        ("- [BLOCKING] **f-3**: no test.\n", "f-3"),
        ("- [blocking] ac-3 unmet.\n", "ac-3"),
        ("- [blocking] the queue drops jobs.\n", "an untagged item"),
        ("1. [closed] f-1. Fixed.\n2. [blocking] f-4. Still open.\n", "f-4"),
    ],
)
def test_an_open_blocking_item_refuses_the_approve(body: str, named: str) -> None:
    refusal = approve_refusal(APPROVE, body)
    assert refusal is not None
    assert refusal.outcome == "rejected-approve-open-blocking"
    assert f"({named})" in refusal.message
    assert "verdict=REVISE" in refusal.message


@pytest.mark.parametrize(
    "body",
    [
        "1. [closed] f-2. Fixed at abc1234.\n",
        "- [minor] f-5. A typo.\n- [follow-up] f-6. Later.\n",
        "```\n- [blocking] f-2. quoted in a fence\n```\n",
        "> - [blocking] f-2. quoted earlier\n",
        "Nothing here is [blocking] any more.\n",
        "    - [blocking] f-2. indented code\n",
    ],
)
def test_closed_minor_and_quoted_items_do_not_refuse(body: str) -> None:
    assert approve_refusal(APPROVE, body) is None


def test_the_ac_message_says_what_to_change() -> None:
    refusal = approve_refusal({"kind": "verdict", "verdict": "APPROVE"}, "- [blocking] f-1.\n")
    assert refusal is not None
    assert refusal.outcome == "rejected-approve-no-ac"
    assert "ac=met" in refusal.message and "ac=unmet" in refusal.message
