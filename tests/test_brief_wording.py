from __future__ import annotations

import re
from pathlib import Path

import pytest

from lib.brief import render_brief
from lib.brief.common import TEST_SCOPE
from tests.regenerate_brief_snapshots import CASES, _job

SNAPSHOTS = Path(__file__).resolve().parent / "snapshots" / "brief"
OUTRANKS = "Where this brief and the router skill or pstack differ, this brief wins."
TITLE_SHAPE = "type(scope): subject"
REVIEW_TIME = "Near it, post the verdict you can support, or a `questions` handoff."
DO_NOT_POLL = "Never poll, watch or wait"
HEADER = re.compile(r"^===== .* =====$", re.MULTILINE)


def briefs() -> list[tuple[str, str]]:
    found = []
    for path in sorted(SNAPSHOTS.glob("*.txt")):
        parts = [part for part in HEADER.split(path.read_text(encoding="utf-8")) if part.strip()]
        assert len(parts) == 2, path.name
        found.extend((f"{path.stem}#{index}", part) for index, part in enumerate(parts))
    return found


BRIEFS = briefs()


def kind_of(text: str) -> str:
    return re.search(r"^# Factory job brief: (\S+)$", text, re.MULTILINE).group(1)


def title_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if TITLE_SHAPE in line]


def test_every_snapshot_is_checked() -> None:
    assert {name.split("#")[0] for name, _ in BRIEFS} == set(CASES)


@pytest.mark.parametrize(("name", "text"), BRIEFS, ids=[name for name, _ in BRIEFS])
def test_one_wording_per_brief(name: str, text: str) -> None:
    assert text.count(OUTRANKS) == 1
    assert "Max's answers" not in text
    assert "T6" not in text
    if kind_of(text) == "review":
        assert REVIEW_TIME in text
        assert "push what you have" not in text
        assert title_lines(text) == []
    else:
        assert text.index("\n## Method\n") < text.index("\n## Job\n")
        (line,) = title_lines(text)
        assert line.startswith("Title the PR ")


@pytest.mark.parametrize("name", ["merge-only#0", "merge-only#1"])
def test_merge_only_shares_the_test_scope_and_says_no_polling_once(name: str) -> None:
    text = dict(BRIEFS)[name]
    assert text.count(TEST_SCOPE) == 1
    assert text.count(DO_NOT_POLL) == 1
    assert "full suite unless" not in text


def test_the_build_brief_uses_the_same_test_scope() -> None:
    for name in ("build#0", "build#1"):
        assert dict(BRIEFS)[name].count(TEST_SCOPE) == 1


@pytest.mark.parametrize("case", ["build", "plan", "review"])
def test_a_blocked_brief_still_says_the_brief_wins_and_asks_for_no_title(case: str) -> None:
    action, recipient, attempt, head, extra = CASES[case]
    text = render_brief(
        _job(action, recipient, attempt, head, extra),
        bot=f"{recipient}-bot",
        context=None,
        factory_root="/srv/REPLACE_WITH_ROOT",
    )
    assert "Do not start the work." in text
    assert text.count(OUTRANKS) == 1
    assert title_lines(text) == []
