"""#130: factory.md finish_line: ready-for-review | merge, defaulting from ship."""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.factory_md import FactoryConfig, load_factory_md, parse_factory_md

ROOT = Path(__file__).resolve().parents[1]

FRONT = """---
base_branch: main
tickets: github
builder:
  harness: claude
reviewer:
  harness: codex
review:
  rounds:
    low: 1
    high: 3
  verify: none
ship: {ship}
skills: []
{extra}---
"""


def parse(ship: str, extra: str = ""):
    return parse_factory_md(FRONT.format(ship=ship, extra=extra))


def errors(findings) -> list[str]:
    return [item.message for item in findings if item.severity == "error"]


@pytest.mark.parametrize(
    ("ship", "expected"),
    [
        ("merge", "merge"),
        ("handoff", "ready-for-review"),
        ("after:tracy-site", "ready-for-review"),
    ],
)
def test_omitted_finish_line_follows_ship(ship: str, expected: str) -> None:
    config, findings = parse(ship)
    assert errors(findings) == []
    assert config is not None and config.finish_line == expected


@pytest.mark.parametrize(
    ("ship", "value"),
    [
        ("merge", "merge"),
        ("handoff", "merge"),
        ("handoff", "ready-for-review"),
        ("after:tracy-site", "merge"),
        ("after:tracy-site", "ready-for-review"),
    ],
)
def test_explicit_finish_line_is_kept(ship: str, value: str) -> None:
    config, findings = parse(ship, f"finish_line: {value}\n")
    assert errors(findings) == []
    assert config is not None and config.finish_line == value


@pytest.mark.parametrize("value", ["done", "Merge", "true", "[merge]", "1"])
def test_unknown_finish_line_is_refused(value: str) -> None:
    config, findings = parse("handoff", f"finish_line: {value}\n")
    assert config is None
    assert errors(findings) == ["finish_line must be ready-for-review or merge"]


def test_ship_merge_with_ready_for_review_is_refused() -> None:
    config, findings = parse("merge", "finish_line: ready-for-review\n")
    assert config is None
    assert any("contradicts" in message for message in errors(findings))


def test_a_config_built_in_code_gets_the_same_default() -> None:
    common = dict(
        base_branch="main",
        tickets="github",
        builder_harness="claude",
        reviewer_harness="codex",
        rounds_low=1,
        rounds_high=3,
        verify="none",
        skills=(),
    )
    assert FactoryConfig(ship="merge", **common).finish_line == "merge"
    assert FactoryConfig(ship="handoff", **common).finish_line == "ready-for-review"
    assert FactoryConfig(ship="handoff", finish_line="merge", **common).finish_line == "merge"


def test_this_repo_finish_line_is_merge() -> None:
    config, findings = load_factory_md(ROOT / "factory.md")
    assert errors(findings) == []
    assert config is not None
    assert (config.ship, config.finish_line) == ("handoff", "merge")


def test_aut0_example_still_stops_at_ready_for_review() -> None:
    config, findings = load_factory_md(ROOT / "examples/aut0/factory.md")
    assert errors(findings) == []
    assert config is not None and config.finish_line == "ready-for-review"
