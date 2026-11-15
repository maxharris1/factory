"""The review brief: requirements first, rounds converge, repeat= and unsat= in the header (#186)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import tests.regenerate_brief_snapshots as snapshots
from lib.brief import render_brief
from lib.factory_md import parse_factory_context
from lib.handoff import parse_header_line

ROOT = Path(__file__).resolve().parents[1]


def review_brief(round_text: str) -> str:
    action, recipient, attempt, head, extra = snapshots.CASES["review"]
    job = snapshots._job(action, recipient, attempt, head, {**extra, "round": round_text})
    context, findings = parse_factory_context(snapshots.FACTORY_MD)
    assert context is not None, findings
    return render_brief(
        job, bot="codex-bot", context=context, context_ref="dev", factory_root=snapshots.BOX_FACTORY_ROOT
    )


def steps(brief: str) -> list[str]:
    task = brief.split("## What to do\n", 1)[1].split("\n## ", 1)[0]
    return [line for line in task.splitlines() if re.match(r"\d+\. ", line)]


def header(brief: str) -> str:
    return next(line for line in brief.splitlines() if line.startswith("<!-- orch:v1 kind=verdict"))


@pytest.mark.parametrize("round_text", ["1/3", "3/3"])
def test_criteria_come_first_with_four_states(round_text: str) -> None:
    first = steps(review_brief(round_text))[0]
    assert first.startswith("1. Requirements first. Before the diff, list the ticket's acceptance criteria by `ac-<n>`")
    for state in ("`met`", "`unmet`", "`unverified`", "`unsatisfiable`"):
        assert state in first
    assert "evidence" in first


def test_round_one_has_no_earlier_rounds_and_round_three_closes_them_by_id() -> None:
    round_one = "\n".join(steps(review_brief("1/3")))
    round_three = "\n".join(steps(review_brief("3/3")))
    for later_only in ("Close the earlier rounds first", "`git range-diff`", "set `repeat=<id>` in the header"):
        assert later_only not in round_one
        assert later_only in round_three
    assert "is `[minor]` unless it fails a criterion" in round_three


@pytest.mark.parametrize("round_text", ["1/3", "3/3"])
def test_every_finding_carries_an_id_and_a_rule(round_text: str) -> None:
    brief = review_brief(round_text)
    assert any("Give each finding an id `f-<n>` and name the rule it breaks" in step for step in steps(brief))
    assert "numbered findings: `f-<n>`, `[blocking]` or `[minor]`, the rule it breaks" in brief


@pytest.mark.parametrize("round_text", ["1/3", "3/3"])
def test_header_template_names_repeat_and_unsat(round_text: str) -> None:
    brief = review_brief(round_text)
    assert header(brief).endswith(" repeat=<id> unsat=<id> -->")
    assert any("set `unsat=<id>` in the header, for example `unsat=ac-3`" in step for step in steps(brief))


def test_the_filled_template_parses() -> None:
    template = header(review_brief("3/3"))
    filled = (
        template.replace("ac=<met|unmet>", "ac=unmet")
        .replace("verdict=<APPROVE|REVISE|BLOCKED>", "verdict=REVISE")
        .replace("repeat=<id>", "repeat=f-2")
        .replace("unsat=<id>", "unsat=ac-3")
    )
    fields = parse_header_line(filled)
    assert (fields["repeat"], fields["unsat"]) == ("f-2", "ac-3")
    dropped = re.sub(r" (repeat|unsat)=<id>", "", template.replace("ac=<met|unmet>", "ac=met")).replace(
        "verdict=<APPROVE|REVISE|BLOCKED>", "verdict=APPROVE"
    )
    assert "repeat" not in parse_header_line(dropped)


def test_merge_conflict_and_unknown_verify() -> None:
    brief = review_brief("1/3")
    assert "A merge conflict with the base is `mergeable: no` in the verdict, not a finding." in brief
    assert "A verify result you cannot read (unknown) is `verdict=BLOCKED`." in brief


def test_reviewer_skill_puts_repeat_in_a_header_that_parses() -> None:
    skill = (ROOT / "skills/factory/factory-reviewer/SKILL.md").read_text(encoding="utf-8")
    example = next(line for line in skill.splitlines() if line.startswith("<!-- orch:v1 kind=verdict"))
    assert parse_header_line(example)["repeat"] == "ac-2"
    assert "`unsat=<id>` in the header" in skill
