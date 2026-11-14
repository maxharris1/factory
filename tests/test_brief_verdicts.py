"""Every brief defines `ac` and the three verdicts, without the old contract text (#168)."""

from __future__ import annotations

import re

import pytest

from lib.handoff import AC_VALUES, VERDICTS
from tests.regenerate_brief_snapshots import CASES, render_snapshot

GONE = ("factory-review-rules", "second AC-unmet", "demonstrated failure")


def _briefs(kind: str) -> list[str]:
    box, remote = render_snapshot(kind).split("===== remote (")
    return [box, remote]


@pytest.mark.parametrize("kind", sorted(CASES))
def test_no_brief_carries_the_old_contract_text(kind: str) -> None:
    for text in _briefs(kind):
        for phrase in GONE:
            assert phrase not in text


@pytest.mark.parametrize("kind", sorted(CASES))
def test_every_brief_defines_ac_and_the_three_verdicts(kind: str) -> None:
    for text in _briefs(kind):
        section = text[text.index("## Review rules (docs/SPEC.md section 8)") :]
        section = section[: section.index("\n## ")]
        defined_ac = re.findall(r"^- `ac=(\w+)`:", section, re.MULTILINE)
        defined_verdicts = re.findall(r"^- `verdict=(\w+)`:", section, re.MULTILINE)
        assert sorted(defined_ac) == sorted(AC_VALUES)
        assert sorted(defined_verdicts) == sorted(VERDICTS)
        assert "- `verdict=BLOCKED`: you could not review at all; say what you need." in section


@pytest.mark.parametrize("kind", sorted(CASES))
def test_every_brief_states_the_blocking_rule(kind: str) -> None:
    for text in _briefs(kind):
        assert (
            "That reviewer marks a finding `[blocking]` only when they have run it and seen it fail."
            in text
        )


def test_the_review_brief_tells_the_reviewer_when_a_finding_blocks() -> None:
    for text in _briefs("review"):
        assert "Mark a finding `[blocking]` only when you have run it and seen it fail" in text
        assert "Pick `ac` and the verdict by the definitions in the Review rules below." in text
