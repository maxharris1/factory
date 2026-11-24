from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib.brief import render_brief
from lib.dispatch.rebase import RebaseStep
from lib.dispatch.types import StackParent
from lib.factory_md import parse_factory_context
from tests.regenerate_brief_snapshots import CASES, FACTORY_MD, HEAD, _job
from tests.test_dispatcher import factory, setup, ticket

PARENT = "factory/issue-1449"
PARENT_SHA = "5a1c" * 10
STACK = {"pr": 1449, "branch": PARENT, "sha": PARENT_SHA}
SENTENCE = (
    f"Stacked on #1449 (`{PARENT}` @ {PARENT_SHA}). This diff is `{PARENT}...{HEAD}`, this PR's own work. "
    "Parent code is reviewed on #1449; raise it only where this change breaks it, as [follow-up]."
)


def render(kind: str, **extra: object) -> str:
    action, recipient, attempt, head, base = CASES[kind]
    context, findings = parse_factory_context(FACTORY_MD)
    assert context is not None, findings
    job = _job(action, recipient, attempt, head, {**base, **extra})
    return render_brief(job, bot=f"{recipient}-bot", context=context, context_ref="dev", factory_root="/srv/f")


@pytest.mark.parametrize("kind", ["review", "fix", "rebase"])
def test_a_stacked_brief_names_the_parent_and_its_own_diff(kind: str) -> None:
    text = render(kind, stack=STACK, base_branch=PARENT)

    assert SENTENCE in text
    assert f"- base branch: {PARENT} (stacked on #1449 at {PARENT_SHA})\n" in text


def test_a_stacked_review_reads_only_its_own_files() -> None:
    assert f"git diff --stat origin/{PARENT}...{HEAD}" in render("review", stack=STACK, base_branch=PARENT)


@pytest.mark.parametrize("kind", ["review", "fix", "rebase"])
def test_an_unstacked_brief_says_nothing_of_a_stack(kind: str) -> None:
    assert "Stacked on" not in render(kind)
    assert "stacked on" not in render(kind)


@pytest.mark.parametrize(
    "stack",
    [{"pr": 0, "branch": PARENT, "sha": PARENT_SHA}, {"pr": 1449, "branch": "bad branch", "sha": PARENT_SHA},
     {"pr": 1449, "branch": PARENT, "sha": "nothex"}, "1449"],
)
def test_a_malformed_stack_is_ignored(stack: object) -> None:
    assert "tacked on" not in render("review", stack=stack, base_branch=PARENT)


def stacked() -> object:
    return replace(
        ticket(head=HEAD),
        factory=replace(factory(), base_branch=PARENT),
        pr_number="1450",
        pr_base=PARENT,
        stack=StackParent(1449, PARENT, PARENT_SHA),
    )


def test_fix_and_rebase_details_carry_the_stack(tmp_path: Path) -> None:
    dispatcher, _ = setup(tmp_path)
    fix = json.loads(dispatcher._job_detail(stacked(), action="fix", head=HEAD))
    rebase = json.loads(
        dispatcher._job_detail(stacked(), action="fix", head=HEAD, rebase_step=RebaseStep("reviewing", HEAD, 1))
    )

    for detail in (fix, rebase):
        assert detail["stack"] == STACK
        assert detail["base_branch"] == PARENT
    assert rebase["kind"] == "rebase"


def test_an_unstacked_detail_has_no_stack(tmp_path: Path) -> None:
    dispatcher, _ = setup(tmp_path)

    assert "stack" not in json.loads(dispatcher._job_detail(ticket(head=HEAD), action="fix", head=HEAD))
