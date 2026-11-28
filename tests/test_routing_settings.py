from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType

import pytest

from lib.role_router import Candidate, RoutingConfig, central_pool, central_routing
from lib.routing_settings import ROLES, HarnessRow, Seat, editable_harnesses

SHIPPED = central_routing()


def _rows(config: RoutingConfig) -> dict[str, HarnessRow]:
    return {row.harness: row for row in editable_harnesses(config)}


def test_roles_are_the_draw_roles():
    assert ROLES == ("build", "review")


def test_shipped_table_lists_every_harness_with_its_models():
    assert SHIPPED is not None and SHIPPED.budget == "medium"
    assert editable_harnesses(SHIPPED) == (
        HarnessRow("claude", Seat("claude-opus-5-5", "anthropic"), Seat("claude-opus-5-5", "anthropic")),
        HarnessRow("codex", Seat("gpt-6.2-astra", "openai"), Seat("gpt-6.2-sol", "openai")),
        HarnessRow("cursor", Seat("claude-opus-4-6", "anthropic"), Seat("gpt-5.4", "openai")),
        HarnessRow("gemini", Seat("gemini-4-neon", "google"), Seat("gemini-4-neon", "google")),
    )


def test_small_tier_drops_cursor_and_uses_the_tier_models():
    assert editable_harnesses(replace(SHIPPED, budget="small")) == (
        HarnessRow("claude", Seat("claude-sonnet-5-5", "anthropic"), Seat("claude-sonnet-5-5", "anthropic")),
        HarnessRow("codex", Seat("gpt-6-luna", "openai"), Seat("gpt-6-luna", "openai")),
        HarnessRow("gemini", Seat("gemini-3.8-flash", "google"), Seat("gemini-3.8-flash", "google")),
    )


def test_unlimited_tier_keeps_cursor_as_a_builder_only():
    cursor = _rows(replace(SHIPPED, budget="unlimited"))["cursor"]
    assert cursor == HarnessRow("cursor", Seat("claude-opus-4-6", "anthropic"), None)


def test_inherited_review_gives_no_review_seat():
    roles = MappingProxyType({**SHIPPED.roles, "review": "inherit"})
    rows = editable_harnesses(replace(SHIPPED, roles=roles))
    assert [row.harness for row in rows] == ["claude", "codex", "cursor", "gemini"]
    assert all(row.review is None for row in rows)


def test_build_seat_falls_through_to_a_typed_pool():
    config = RoutingConfig(
        budget="medium",
        roles=MappingProxyType({
            "build": (Candidate("claude", "claude-opus-5-5"),),
            "build:refactor": (Candidate("codex", "gpt-6.2-astra"),),
            "build:perf": (Candidate("codex", "gpt-6-luna"),),
        }),
    )
    assert editable_harnesses(config) == (
        HarnessRow("claude", Seat("claude-opus-5-5", "anthropic"), None),
        HarnessRow("codex", Seat("gpt-6.2-astra", "openai"), None),
    )


def test_central_pool_reads_the_active_tier():
    small = replace(SHIPPED, budget="small")
    assert central_pool(small, "build:feature") == small.budget_overrides["small"]["build"]
    assert central_pool(small, "plan") == SHIPPED.roles["plan"]
    assert central_pool(SHIPPED, "build:perf") == SHIPPED.roles["build:perf"]
    assert central_pool(SHIPPED, "review") == SHIPPED.roles["review"]


@pytest.mark.parametrize("roles", [{"review": "inherit"}, {}])
def test_central_pool_is_empty_for_an_inherited_or_absent_slot(roles):
    config = RoutingConfig(budget="medium", roles=MappingProxyType(roles))
    assert central_pool(config, "review") == ()
