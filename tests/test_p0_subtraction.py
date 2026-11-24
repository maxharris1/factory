"""Regression checks for PR 0.5 dead-code subtraction and repo hygiene."""

from __future__ import annotations

import importlib
from dataclasses import replace
from pathlib import Path

import pytest

from lib import github_actions, usage_readings
from lib.orchestrate import RouteError, choose_route
from lib.pstack_pin import load_pin
from tests.test_orchestrate import request_for

ROOT = Path(__file__).resolve().parents[1]


def test_dead_usage_reserve_and_adapters_are_removed() -> None:
    assert not (ROOT / "lib" / "usage.py").exists()
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("lib.usage")
    assert not hasattr(usage_readings, "TelemetryUsage")
    assert not hasattr(github_actions, "review_actions_for")


def test_choose_route_enforces_cross_family_without_usage_module() -> None:
    base = request_for("examples/cleffy/factory.md")
    plain = choose_route(base)
    assert (plain.builder_harness, plain.reviewer_harness) == ("claude", "codex")
    assert plain.builder_using_reserve is False
    assert plain.reviewer_using_reserve is False
    assert plain.reviewer_fallback is False

    # When Codex built, reviewer falls back to a cross-family harness (Claude).
    swapped = choose_route(replace(base, built_by=("codex", None)))
    assert (swapped.builder_harness, swapped.reviewer_harness) == ("codex", "claude")
    assert swapped.reviewer_fallback is True

    with pytest.raises(RouteError):
        choose_route(replace(base, built_by=("grok", None)))


def test_model_check_sh_uses_vendored_statechart_and_matches_pstack_pin() -> None:
    check_sh = (ROOT / "model" / "check.sh").read_text(encoding="utf-8")
    pin = load_pin(ROOT)
    assert f"pin={pin.commit}" in check_sh
    assert "vendored=$root/skills/pstack/skills/statechart/scripts/statechart" in check_sh
    assert (ROOT / "skills" / "pstack" / "skills" / "statechart" / "scripts" / "statechart").exists()


def test_ephemeral_screenshot_bloat_is_purged_and_ignored() -> None:
    assert not (ROOT / "docs" / "screenshots").exists()
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "docs/screenshots/" in gitignore
    assert "docs/decisions/*-trail.tsv" in gitignore
    assert "!docs/decisions/694-trail.tsv" in gitignore
    assert (ROOT / "docs" / "decisions" / "694-trail.tsv").exists()
