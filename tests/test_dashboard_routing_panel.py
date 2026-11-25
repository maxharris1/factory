from pathlib import Path

import pytest

from lib.role_router import (
    BUDGETS, ROLE_SLOTS, ROUTING_PATH, RoutingError, parse_routing_yml, resolve_slot, role_matrix,
)
from lib.pstack_pin import ROOT

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
