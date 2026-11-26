import builtins
import json
import os
import re
from pathlib import Path

import pytest

from lib import dashboard
from lib.dashboard import render_pages
from lib.role_router import (
    BUDGETS, KNOWN_HARNESSES, ROLE_SLOTS, ROUTING_PATH, WAITER_BOTS, RoutingError, parse_routing_yml, resolve_slot,
    role_matrix,
)
from lib.pstack_pin import ROOT
from lib.wait import OWNING_BOTS
from tests.test_dashboard import snapshot_for
from tests.test_dashboard_efficiency import _inner
from tests.test_dashboard_layout import _run_page, _text, needs_node

ROUTING = """\
budget: medium
roles:
  build:
    - harness: claude
      model: claude-opus-4-6
      effort: max
    - harness: codex
      model: gpt-5.4
      effort: low
    - harness: gemini
      model: gemini-2.5-pro
  review:
    - harness: codex
      model: gpt-5.4
      effort: high
  plan:
    - harness: claude
      model: claude-sonnet-4-6
  qa: inherit
budget_overrides:
  small:
    build:
      - harness: claude
        model: claude-sonnet-4-6
        effort: high
    review: inherit
"""


CLAUDE_WAITER = ("claude", "-p", "--effort", "medium")
GEMINI_WAITER = ("gemini", "-p", "", "--yolo")
WRAPPED_WAITER = ("env", "FOO=1", "claude", "-p", "--model", "claude-haiku-4-5")


def _matrix(tmp_path: Path, text: str = ROUTING, waiters: dict | None = None) -> dict:
    path = tmp_path / "routing.yml"
    path.write_text(text, encoding="utf-8")
    return role_matrix(path, waiters)


def _routed(model: str | None, effort: str | None) -> dict:
    return {"model": model, "model_source": "unknown" if model is None else "routing.yml",
            "effort": effort, "effort_source": "unknown" if effort is None else "routing.yml",
            "replaced": {}, "applied": True}


def _effective(matrix: dict, role: str, rank: int) -> dict:
    return _row(matrix, role)["candidates"][rank - 1]["effective"]


def _row(matrix: dict, role: str) -> dict:
    return next(row for row in matrix["roles"] if row["role"] == role)


def test_matrix_clamps_effort_to_the_budget_ceiling_and_keeps_the_configured_value(tmp_path: Path) -> None:
    matrix = _matrix(tmp_path)
    assert (matrix["state"], matrix["budget"], matrix["ceiling"]) == ("ok", "medium", "high")
    assert _row(matrix, "build") == {"role": "build", "source": "roles", "candidates": [
        {"rank": 1, "harness": "claude", "model": "claude-opus-4-6", "family": "anthropic",
         "effort": "high", "configured": "max", "effective": _routed("claude-opus-4-6", "high")},
        {"rank": 2, "harness": "codex", "model": "gpt-5.4", "family": "openai", "effort": "low", "configured": "low",
         "effective": _routed("gpt-5.4", "low")},
        {"rank": 3, "harness": "gemini", "model": "gemini-2.5-pro", "family": "google",
         "effort": None, "configured": None, "effective": _routed("gemini-2.5-pro", None)},
    ]}


def test_matrix_falls_back_from_a_build_kind_to_build(tmp_path: Path) -> None:
    matrix = _matrix(tmp_path)
    assert _row(matrix, "build:feature")["candidates"] == _row(matrix, "build")["candidates"]
    assert _row(matrix, "build:feature")["source"] == "roles"


def test_matrix_takes_the_tier_override_unclamped(tmp_path: Path) -> None:
    matrix = _matrix(tmp_path, ROUTING.replace("budget: medium", "budget: small"))
    assert (matrix["budget"], matrix["ceiling"]) == ("small", "medium")
    for role in ("build", "build:perf"):
        assert _row(matrix, role) == {"role": role, "source": "budget_overrides", "candidates": [
            {"rank": 1, "harness": "claude", "model": "claude-sonnet-4-6", "family": "anthropic",
             "effort": "high", "configured": "high", "effective": _routed("claude-sonnet-4-6", "high")},
        ]}


def test_matrix_shows_inherit_and_unset_slots_with_no_candidates(tmp_path: Path) -> None:
    medium = _matrix(tmp_path)
    assert _row(medium, "qa") == {"role": "qa", "source": "inherit", "candidates": [], "fallback": []}
    assert _row(medium, "helper:panel") == {"role": "helper:panel", "source": "unset", "candidates": [], "fallback": []}
    small = _matrix(tmp_path, ROUTING.replace("budget: medium", "budget: small"))
    assert _row(small, "review") == {"role": "review", "source": "inherit", "candidates": [], "fallback": []}


def test_matrix_has_one_row_per_role_in_order(tmp_path: Path) -> None:
    assert [row["role"] for row in _matrix(tmp_path)["roles"]] == list(ROLE_SLOTS)


def test_matrix_of_a_missing_file_is_missing(tmp_path: Path) -> None:
    assert role_matrix(tmp_path / "routing.yml") == {"state": "missing", "roles": [], "waiters": {}}
    assert role_matrix(tmp_path / "routing.yml", {"claude": None})["waiters"] == {"claude": "missing"}


def test_matrix_of_a_broken_file_is_invalid_with_the_parser_errors(tmp_path: Path) -> None:
    matrix = _matrix(tmp_path, ROUTING.replace("harness: gemini", "harness: grok"))
    assert matrix["state"] == "invalid" and matrix["roles"] == [] and matrix["waiters"] == {}
    assert [e for e in matrix["errors"] if "roles.build[2].harness" in e], matrix["errors"]
    assert _matrix(tmp_path, "budget: [")["state"] == "invalid"


def test_a_routed_value_replaces_the_one_the_waiter_pins(tmp_path: Path) -> None:
    matrix = _matrix(tmp_path, waiters={"claude": CLAUDE_WAITER})
    assert _effective(matrix, "build", 1) == {
        "model": "claude-opus-4-6", "model_source": "routing.yml", "effort": "high", "effort_source": "routing.yml",
        "replaced": {"effort": "medium"}, "applied": True}
    assert matrix["waiters"] == {"claude": "ok"}


def test_an_unrouted_value_comes_from_the_waiter_or_the_cli(tmp_path: Path) -> None:
    matrix = _matrix(tmp_path, waiters={"claude": CLAUDE_WAITER, "gemini": GEMINI_WAITER})
    assert _effective(matrix, "build", 3) == {
        "model": "gemini-2.5-pro", "model_source": "routing.yml", "effort": None, "effort_source": "CLI default",
        "replaced": {}, "applied": True}
    assert _effective(matrix, "plan", 1) == {
        "model": "claude-sonnet-4-6", "model_source": "routing.yml",
        "effort": "medium", "effort_source": "waiter-claude-bot.yml", "replaced": {}, "applied": True}


def test_a_routed_gemini_effort_never_reaches_the_cli(tmp_path: Path) -> None:
    text = ROUTING.replace("      model: gemini-2.5-pro\n", "      model: gemini-2.5-pro\n      effort: high\n", 1)
    matrix = _matrix(tmp_path, text, {"gemini": GEMINI_WAITER})
    assert (_effective(matrix, "build", 3)["effort"], _effective(matrix, "build", 3)["effort_source"]) == (
        None, "CLI default")


def test_a_missing_waiter_leaves_only_what_routing_names(tmp_path: Path) -> None:
    matrix = _matrix(tmp_path, waiters={"claude": None})
    assert _effective(matrix, "build", 1) == _routed("claude-opus-4-6", "high")
    assert _effective(matrix, "plan", 1) == _routed("claude-sonnet-4-6", None)
    assert matrix["waiters"] == {"claude": "missing"}


def test_a_wrapped_waiter_gets_no_routing_flags(tmp_path: Path) -> None:
    matrix = _matrix(tmp_path, waiters={"claude": WRAPPED_WAITER})
    assert _effective(matrix, "build", 1) == {
        "model": "claude-haiku-4-5", "model_source": "waiter-claude-bot.yml",
        "effort": None, "effort_source": "CLI default", "replaced": {}, "applied": False}


def test_a_slot_with_no_candidates_falls_back_to_each_readable_waiter(tmp_path: Path) -> None:
    matrix = _matrix(tmp_path, waiters={"gemini": GEMINI_WAITER, "codex": None, "claude": CLAUDE_WAITER})
    assert _row(matrix, "qa")["fallback"] == [
        {"harness": "claude", "model": None, "model_source": "CLI default",
         "effort": "medium", "effort_source": "waiter-claude-bot.yml"},
        {"harness": "gemini", "model": None, "model_source": "CLI default", "effort": None, "effort_source": "CLI default"},
    ]
    assert "fallback" not in _row(matrix, "build")
    assert matrix["waiters"] == {"gemini": "ok", "codex": "missing", "claude": "ok"}


def test_no_waiters_looked_at_means_routing_alone(tmp_path: Path) -> None:
    matrix = _matrix(tmp_path)
    assert matrix["waiters"] == {}
    for row in matrix["roles"]:
        for cand in row["candidates"]:
            assert cand["effective"] == _routed(cand["model"], cand["effort"]), row["role"]


def test_waiter_bots_are_the_owning_bots_inverted() -> None:
    assert dict(WAITER_BOTS) == {harness: bot for bot, harness in OWNING_BOTS.items()}
    assert tuple(WAITER_BOTS) == KNOWN_HARNESSES


def test_matrix_of_the_shipped_file_is_ok_for_every_role() -> None:
    matrix = role_matrix()
    assert matrix["state"] == "ok"
    assert [row["role"] for row in matrix["roles"]] == list(ROLE_SLOTS)


@pytest.mark.parametrize("budget", BUDGETS)
def test_matrix_first_choice_is_what_the_router_resolves(tmp_path: Path, budget: str) -> None:
    text = (ROOT / ROUTING_PATH).read_text(encoding="utf-8").replace("budget: medium", f"budget: {budget}")
    matrix = _matrix(tmp_path, text)
    config, _ = parse_routing_yml(text)
    for row in matrix["roles"]:
        if not row["candidates"]:
            with pytest.raises(RoutingError):
                resolve_slot(row["role"], routing=config)
            continue
        first = row["candidates"][0]
        slot = resolve_slot(row["role"], routing=config)
        assert (slot.harness, slot.model, slot.effort, slot.family) == (
            first["harness"], first["model"], first["effort"], first["family"]), row["role"]


BOTH = ["server", pytest.param("script", marks=needs_node)]


@pytest.fixture(scope="module")
def base(tmp_path_factory: pytest.TempPathFactory) -> dict:
    return snapshot_for(tmp_path_factory.mktemp("site"))


def _with(base: dict, matrix: dict) -> dict:
    return {**base, "routing": matrix}


def _panel(tmp_path: Path, snapshot: dict, render: str) -> str:
    if render == "server":
        return _inner(render_pages(snapshot)["efficiency.html"], "routing")
    return _run_page(tmp_path, snapshot, "efficiency", ["routing"])["html"]["routing"]


def _role(panel: str, role: str) -> str:
    match = re.search(rf'<tr><th scope="row">{re.escape(role)}</th>(.*?)</tr>', panel)
    assert match, role
    return match.group(1)


def test_the_snapshot_carries_the_shipped_matrix(base: dict) -> None:
    assert base["routing"] == role_matrix()


@needs_node
@pytest.mark.parametrize("text", [ROUTING, None, ROUTING.replace("harness: gemini", "harness: grok")],
                         ids=["ok", "missing", "invalid"])
def test_server_and_script_draw_the_same_panel(tmp_path: Path, base: dict, text: str | None) -> None:
    matrix = role_matrix(tmp_path / "absent.yml") if text is None else _matrix(tmp_path, text)
    snapshot = _with(base, matrix)
    assert _panel(tmp_path, snapshot, "script") == _panel(tmp_path, snapshot, "server")


@pytest.mark.parametrize("render", BOTH)
def test_panel_heads_with_the_budget_and_its_ceiling(tmp_path: Path, base: dict, render: str) -> None:
    panel = _panel(tmp_path, _with(base, _matrix(tmp_path)), render)
    assert panel.startswith(
        '<h2><span>Role routing</span><span class="count">medium budget · effort up to high</span></h2>')


@pytest.mark.parametrize("render", BOTH)
def test_panel_marks_a_clamped_effort_with_its_configured_value(tmp_path: Path, base: dict, render: str) -> None:
    build = _role(_panel(tmp_path, _with(base, _matrix(tmp_path)), render), "build")
    assert '<span class="rt-e clamped">max → high</span>' in build
    assert '<span class="rt-e">low</span>' in build
    small = _matrix(tmp_path, ROUTING.replace("budget: medium", "budget: small"))
    assert '<span class="rt-e">high</span>' in _role(_panel(tmp_path, _with(base, small), render), "build")


@pytest.mark.parametrize("render", BOTH)
def test_panel_lists_candidates_in_preference_order(tmp_path: Path, base: dict, render: str) -> None:
    build = _role(_panel(tmp_path, _with(base, _matrix(tmp_path)), render), "build")
    assert '<ol class="rt-pool">' in build
    assert [_text(li) for li in re.findall(r"<li>(.*?)</li>", build)] == [
        "claude · claude-opus-4-6 · max → high", "codex · gpt-5.4 · low", "gemini · gemini-2.5-pro"]


@pytest.mark.parametrize("render", BOTH)
def test_panel_names_the_source_and_the_fallback_of_an_empty_slot(tmp_path: Path, base: dict, render: str) -> None:
    small = _panel(tmp_path, _with(base, _matrix(tmp_path, ROUTING.replace("budget: medium", "budget: small"))), render)
    assert _role(small, "build").startswith("<td>budget override</td>")
    fallback = '<td><span class="muted">falls back to factory.md {}</span></td>'
    assert _role(small, "review") == "<td>inherit</td>" + fallback.format("reviewer")
    assert _role(small, "helper:panel") == "<td>not set</td>" + fallback.format("builder")
    medium = _panel(tmp_path, _with(base, _matrix(tmp_path)), render)
    assert _role(medium, "build").startswith("<td>roles</td>")


@pytest.mark.parametrize("render", BOTH)
def test_panel_says_the_file_is_missing(tmp_path: Path, base: dict, render: str) -> None:
    panel = _panel(tmp_path, _with(base, role_matrix(tmp_path / "absent.yml")), render)
    assert panel == '<h2><span>Role routing</span></h2><p class="empty">config/routing.yml not found</p>'


@pytest.mark.parametrize("render", BOTH)
def test_panel_lists_the_errors_of_a_broken_file_escaped(tmp_path: Path, base: dict, render: str) -> None:
    matrix = _matrix(tmp_path, "<script>x()</script>: 1\n" + ROUTING.replace("harness: gemini", "harness: grok"))
    panel = _panel(tmp_path, _with(base, matrix), render)
    assert panel.startswith('<h2><span>Role routing</span></h2><p class="empty">config/routing.yml is invalid:</p>')
    errors = re.findall(r"<li>(.*?)</li>", panel)
    assert "unknown field &#x27;&lt;script&gt;x()&lt;/script&gt;&#x27;" in errors
    assert [e for e in errors if e.startswith("roles.build[2].harness")]
    assert "<script>" not in panel


def test_panel_holds_no_control(tmp_path: Path, base: dict) -> None:
    for matrix in (_matrix(tmp_path), role_matrix(tmp_path / "absent.yml")):
        panel = _inner(render_pages(_with(base, matrix))["efficiency.html"], "routing")
        assert not re.search(r"<(form|button|input|select|textarea)\b", panel)
