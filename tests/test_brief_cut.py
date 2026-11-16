"""A brief carries only the text its job needs, each rule once (#504)."""

from __future__ import annotations

import json
import re

import pytest

from lib.brief import render_brief
from lib.factory_md import parse_factory_context
from lib.pstack_models import brief_lines, load_map
from lib.schema import HARNESSES
from tests.regenerate_brief_snapshots import (
    BOX_FACTORY_ROOT,
    CASES,
    FACTORY_MD,
    HEAD,
    ROUND_ONE_VERDICT,
    _job,
    evidence_bundle,
    render_snapshot,
)

REVIEW_RULES = "## Review rules"
NO_FOLLOW_UP_FIXES = "Builders do not fix `[follow-up]` findings."
USER_IMPACT_MD = FACTORY_MD.replace("review:\n", "review:\n  block_on: user-impact\n")
BUILD_BRIEF_BYTES_BEFORE = 10_373

ONCE = {
    "test scope": re.compile(r"the tests (?:for|affected by) the files you touched"),
    "no CI polling": re.compile(r"poll, watch or wait on|never wait on it"),
    "blocking": re.compile(r"only when (?:they|you) have run it and seen it fail"),
    "user-impact bar": re.compile(r"a bug a user or the factory would hit"),
}


def _halves(kind: str) -> list[str]:
    box, remote = render_snapshot(kind).split("===== remote (")
    return [box.removeprefix("===== box =====\n"), remote]


def _without_factory_md(text: str) -> str:
    return text.split("## Additional context from factory.md")[0]


def _section(text: str, heading: str) -> str:
    start = text.index(f"\n{heading}\n")
    end = text.find("\n## ", start + 1)
    return text[start : end if end >= 0 else len(text)]


def _render(action: str, recipient: str, extra: dict[str, object], factory_md: str = FACTORY_MD) -> str:
    head = None if action in ("build", "plan") else HEAD
    context, findings = parse_factory_context(factory_md)
    assert context is not None, findings
    return render_brief(
        _job(action, recipient, 1, head, extra),
        bot=f"{recipient}-bot",
        context=context,
        context_ref="dev",
        factory_root=BOX_FACTORY_ROOT,
    )


BUILDER_JOBS = {
    "plan": ("plan", {}),
    "build": ("build", {"gitBranchName": "factory/issue-42"}),
    "fix": ("fix", {"instruction": ROUND_ONE_VERDICT.url, "round": "2/3"}),
}


@pytest.mark.parametrize("kind", sorted(kind for kind in CASES if not kind.startswith("review")))
def test_a_builder_brief_has_no_review_rules(kind: str) -> None:
    for text in _halves(kind):
        assert REVIEW_RULES not in text


@pytest.mark.parametrize("kind", sorted(kind for kind in CASES if kind.startswith("review")))
def test_a_review_brief_keeps_its_review_rules(kind: str) -> None:
    for text in _halves(kind):
        assert REVIEW_RULES in text


@pytest.mark.parametrize("name", sorted(BUILDER_JOBS))
def test_a_builder_brief_says_once_not_to_fix_follow_ups_only_when_they_are_filed(name: str) -> None:
    action, extra = BUILDER_JOBS[name]
    filed = _render(action, "claude", extra, USER_IMPACT_MD)
    assert REVIEW_RULES not in filed
    assert filed.count(NO_FOLLOW_UP_FIXES) == 1
    assert NO_FOLLOW_UP_FIXES not in _render(action, "claude", extra)


def test_a_review_brief_says_once_not_to_fix_follow_ups() -> None:
    box = _halves("review-user-impact")[0]
    assert box.count(NO_FOLLOW_UP_FIXES) == 1


def _helper_block(text: str) -> str:
    return _section(text, "## Skills").split("```\n")[1]


@pytest.mark.parametrize(
    ("harness", "action"),
    [("claude", "build"), ("claude", "review"), ("codex", "build"), ("codex", "review")],
)
def test_a_brief_lists_the_model_lines_of_its_own_harness_only(harness: str, action: str) -> None:
    extra = {"gitBranchName": "factory/issue-42"} if action == "build" else {"round": "1/3"}
    block = _helper_block(_render(action, harness, extra))
    assert block == brief_lines(harness)
    own = {line.split(": ", 1)[1] for line in block.splitlines()}
    models = load_map()
    for other in HARNESSES:
        if other != harness:
            for line in models[other]:
                for value in line.render().split(": ", 1)[1].split(", "):
                    if value not in own:
                        assert value not in block, f"{other} line {value!r} in a {harness} brief"


@pytest.mark.parametrize("kind", sorted(CASES))
def test_each_repeated_rule_is_stated_at_most_once(kind: str) -> None:
    for text in _halves(kind):
        body = _without_factory_md(text)
        for rule, pattern in ONCE.items():
            count = len(pattern.findall(body))
            assert count <= 1, f"{kind}: the {rule} rule is stated {count} times"


def test_a_round_two_fix_brief_shows_each_open_finding_once() -> None:
    evidence = evidence_bundle(2)
    text = _render("fix", "claude", {
        "instruction": ROUND_ONE_VERDICT.url,
        "round": "2/3",
        "evidence": evidence,
    })
    task = _section(text, "## What to do")
    data = _section(text, "## Job data")
    findings = evidence["findings"]["findings"]
    assert findings
    for finding in findings:
        for field in ("text", "rule"):
            value = finding[field]
            assert task.count(value) >= 1, f"{finding['id']} {field} is not quoted"
            assert data.count(json.dumps(value)[1:-1]) <= 1, f"{finding['id']} {field} repeats in Job data"
        assert text.count(finding["text"]) == 1


def test_the_build_brief_is_at_least_15_percent_smaller() -> None:
    box = _halves("build")[0]
    assert len(box.encode()) <= 0.85 * BUILD_BRIEF_BYTES_BEFORE
