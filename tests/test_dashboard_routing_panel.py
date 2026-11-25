import re
from pathlib import Path

import pytest

from lib.dashboard import render_pages
from lib.role_router import (
    BUDGETS, ROLE_SLOTS, ROUTING_PATH, RoutingError, parse_routing_yml, resolve_slot, role_matrix,
)
from lib.pstack_pin import ROOT
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
  qa: inherit
budget_overrides:
  small:
    build:
      - harness: claude
        model: claude-sonnet-4-6
        effort: high
    review: inherit
"""


def _matrix(tmp_path: Path, text: str = ROUTING) -> dict:
    path = tmp_path / "routing.yml"
    path.write_text(text, encoding="utf-8")
    return role_matrix(path)


def _row(matrix: dict, role: str) -> dict:
    return next(row for row in matrix["roles"] if row["role"] == role)


def test_matrix_clamps_effort_to_the_budget_ceiling_and_keeps_the_configured_value(tmp_path: Path) -> None:
    matrix = _matrix(tmp_path)
    assert (matrix["state"], matrix["budget"], matrix["ceiling"]) == ("ok", "medium", "high")
    assert _row(matrix, "build") == {"role": "build", "source": "roles", "candidates": [
        {"rank": 1, "harness": "claude", "model": "claude-opus-4-6", "family": "anthropic",
         "effort": "high", "configured": "max"},
        {"rank": 2, "harness": "codex", "model": "gpt-5.4", "family": "openai", "effort": "low", "configured": "low"},
        {"rank": 3, "harness": "gemini", "model": "gemini-2.5-pro", "family": "google",
         "effort": None, "configured": None},
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
             "effort": "high", "configured": "high"},
        ]}


def test_matrix_shows_inherit_and_unset_slots_with_no_candidates(tmp_path: Path) -> None:
    medium = _matrix(tmp_path)
    assert _row(medium, "qa") == {"role": "qa", "source": "inherit", "candidates": []}
    assert _row(medium, "helper:panel") == {"role": "helper:panel", "source": "unset", "candidates": []}
    small = _matrix(tmp_path, ROUTING.replace("budget: medium", "budget: small"))
    assert _row(small, "review") == {"role": "review", "source": "inherit", "candidates": []}


def test_matrix_has_one_row_per_role_in_order(tmp_path: Path) -> None:
    assert [row["role"] for row in _matrix(tmp_path)["roles"]] == list(ROLE_SLOTS)


def test_matrix_of_a_missing_file_is_missing(tmp_path: Path) -> None:
    assert role_matrix(tmp_path / "routing.yml") == {"state": "missing", "roles": []}


def test_matrix_of_a_broken_file_is_invalid_with_the_parser_errors(tmp_path: Path) -> None:
    matrix = _matrix(tmp_path, ROUTING.replace("harness: gemini", "harness: grok"))
    assert matrix["state"] == "invalid" and matrix["roles"] == []
    assert [e for e in matrix["errors"] if "roles.build[2].harness" in e], matrix["errors"]
    assert _matrix(tmp_path, "budget: [")["state"] == "invalid"


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
