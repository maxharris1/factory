from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType

import pytest

from lib.role_router import (
    BuildShare,
    Candidate,
    ReviewShare,
    RoutingConfig,
    ShareTable,
    central_pool,
    central_routing,
    defaults_table,
)
from lib.routing_settings import (
    DIFFICULTIES,
    REASONS,
    ROLES,
    HarnessRow,
    HarnessSetting,
    OverrideRefused,
    Seat,
    editable_harnesses,
    parse_override,
)

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


SPLIT = {
    "claude": {"difficulty": "hard", "build": 50, "review": 50},
    "codex": {"difficulty": "any", "build": 25, "review": 50},
    "gemini": {"difficulty": "easy", "build": 25, "review": 0},
}


def test_a_split_with_cursor_off_builds_the_table():
    table = parse_override(SPLIT, SHIPPED).table(version=3)
    assert table == ShareTable(
        build=(BuildShare("claude", 0.5, "hard"), BuildShare("codex", 0.25, "any"), BuildShare("gemini", 0.25, "easy")),
        review=(ReviewShare("claude", 0.5), ReviewShare("codex", 0.5), ReviewShare("gemini", 0.0)),
        build_off=frozenset({"cursor"}),
        review_off=frozenset({"cursor"}),
        version=3,
    )


def test_settings_give_each_harness_its_difficulty_and_percentages():
    value = {**SPLIT, "cursor": {"difficulty": "any"}, "claude": {"difficulty": "hard", "build": 50}}
    value["codex"] = {**SPLIT["codex"], "review": 100}
    assert parse_override(value, SHIPPED).settings == {
        "claude": HarnessSetting("hard", 50, None),
        "codex": HarnessSetting("any", 25, 100),
        "cursor": HarnessSetting("any", None, None),
        "gemini": HarnessSetting("easy", 25, 0),
    }


def test_key_order_does_not_change_the_table():
    reordered = {h: dict(reversed(SPLIT[h].items())) for h in reversed(SPLIT)}
    assert list(reordered) != list(SPLIT)
    assert parse_override(reordered, SHIPPED).table(version=1) == parse_override(SPLIT, SHIPPED).table(version=1)


def test_shipped_defaults_as_an_override_match_the_default_gemini_entries():
    value = {
        "claude": {"difficulty": "hard", "build": 75, "review": 0},
        "codex": {"difficulty": "hard", "build": 0, "review": 75},
        "cursor": {"difficulty": "hard", "build": 0, "review": 0},
        "gemini": {"difficulty": "hard", "build": 25, "review": 25},
    }
    table = parse_override(value, SHIPPED).table(version=1)
    defaults = defaults_table(SHIPPED)
    assert table.build_off == table.review_off == frozenset()
    assert [e for e in table.build if e.harness == "gemini"] == [e for e in defaults.build if e.harness == "gemini"]
    assert [e for e in table.review if e.harness == "gemini"] == [e for e in defaults.review if e.harness == "gemini"]


def test_reasons_and_difficulties_are_exposed_in_order():
    assert REASONS == (
        "bad-shape",
        "unknown-role",
        "unknown-difficulty",
        "bad-share",
        "unknown-harness",
        "build-total",
        "review-total",
    )
    assert DIFFICULTIES == ("hard", "any", "easy")


def _with(harness, **block):
    return {**SPLIT, harness: block}


REFUSALS = [
    ("bad-shape", ["claude"]),
    ("bad-shape", {1: {"difficulty": "any"}}),
    ("bad-shape", {"claude": [("difficulty", "any")]}),
    ("bad-shape", _with("claude", build=50, review=50)),
    ("bad-shape", {"claude": {"difficulty": "any", 1: 50}}),
    ("unknown-role", _with("claude", difficulty="hard", build=50, review=50, qa=10)),
    ("unknown-difficulty", _with("claude", difficulty="medium", build=50, review=50)),
    ("bad-share", _with("claude", difficulty="hard", build=True, review=50)),
    ("bad-share", _with("claude", difficulty="hard", build=50.0, review=50)),
    ("bad-share", _with("claude", difficulty="hard", build="50", review=50)),
    ("bad-share", _with("claude", difficulty="hard", build=None, review=50)),
    ("bad-share", _with("claude", difficulty="hard", build=101, review=50)),
    ("bad-share", _with("claude", difficulty="hard", build=-1, review=50)),
    ("unknown-harness", {**SPLIT, "grok": {"difficulty": "any", "build": 0}}),
    ("build-total", _with("claude", difficulty="hard", build=40, review=50)),
    ("build-total", _with("claude", difficulty="hard", build=60, review=50)),
    ("review-total", _with("claude", difficulty="hard", build=50, review=40)),
]


@pytest.mark.parametrize(("reason", "value"), REFUSALS)
def test_a_refused_value_names_one_reason(reason, value):
    with pytest.raises(OverrideRefused) as refused:
        parse_override(value, SHIPPED)
    assert isinstance(refused.value, ValueError)
    assert refused.value.reason == reason
    assert str(refused.value) == reason
    assert refused.value.args == (reason,)


def test_a_review_share_for_cursor_is_refused_when_unlimited():
    value = {**SPLIT, "cursor": {"difficulty": "any", "review": 0}}
    parse_override(value, SHIPPED)
    with pytest.raises(OverrideRefused) as refused:
        parse_override(value, replace(SHIPPED, budget="unlimited"))
    assert refused.value.reason == "unknown-harness"


def test_a_block_with_no_role_key_is_accepted_for_an_unlisted_harness():
    override = parse_override({**SPLIT, "grok": {"difficulty": "easy"}}, SHIPPED)
    assert override.settings["grok"] == HarnessSetting("easy", None, None)
    assert "grok" not in override.build_off | override.review_off


NEIGHBOURS = [
    ({"difficulty": "any", 1: 0}, {"difficulty": "any", "qa": 0}, "bad-shape"),
    ({"difficulty": "any", "qa": 0}, {"difficulty": "nope"}, "unknown-role"),
    ({"difficulty": "nope"}, {"difficulty": "any", "build": 500}, "unknown-difficulty"),
    ({"difficulty": "any", "build": 500}, {"difficulty": "any", "build": 0}, "bad-share"),
]


@pytest.mark.parametrize(("early", "late", "reason"), NEIGHBOURS)
def test_the_earlier_rule_wins_in_either_key_order(early, late, reason):
    rest = {h: b for h, b in SPLIT.items() if h != "claude"}
    pairs = [("claude", early), ("grok", late)]
    values = [{**dict(order), **rest} for order in (pairs, pairs[::-1])]
    assert [list(value)[:2] for value in values] == [["claude", "grok"], ["grok", "claude"]]
    for value in values:
        with pytest.raises(OverrideRefused) as refused:
            parse_override(value, SHIPPED)
        assert refused.value.reason == reason


def test_the_earlier_total_wins_in_either_key_order():
    for value in (
        {"claude": {"difficulty": "any", "build": 10}, "codex": {"difficulty": "any", "review": 10}},
        {"codex": {"difficulty": "any", "review": 10}, "claude": {"difficulty": "any", "build": 10}},
    ):
        with pytest.raises(OverrideRefused) as refused:
            parse_override(value, SHIPPED)
        assert refused.value.reason == "build-total"


def test_an_inherited_review_accepts_a_build_only_value():
    roles = MappingProxyType({**SHIPPED.roles, "review": "inherit"})
    value = {h: {"difficulty": b["difficulty"], "build": b["build"]} for h, b in SPLIT.items()}
    table = parse_override(value, replace(SHIPPED, roles=roles)).table(version=2)
    assert table.review == () and table.review_off == frozenset()
    assert table.build_off == frozenset({"cursor"})


class _ExplodingItems(dict):
    def items(self):
        raise RuntimeError("boom")

    def __iter__(self):
        raise RuntimeError("boom")

    def keys(self):
        raise RuntimeError("boom")


HOSTILE = [
    None,
    ["claude"],
    "claude",
    42,
    {("claude",): {"difficulty": "any"}},
    {"claude": {"difficulty": ["hard"]}},
    {"claude": {"difficulty": {"hard": 1}}},
    _with("claude", difficulty="hard", build=float("nan"), review=50),
    _with("claude", difficulty="hard", build=10**400, review=50),
    _ExplodingItems(SPLIT),
    {"claude": _ExplodingItems(difficulty="any")},
]


@pytest.mark.parametrize("value", HOSTILE)
def test_hostile_values_raise_only_a_refusal(value):
    with pytest.raises(OverrideRefused) as refused:
        parse_override(value, SHIPPED)
    assert type(refused.value) is OverrideRefused
    assert refused.value.reason in REASONS


MARK = "MARKER-7f3a"


@pytest.mark.parametrize(
    "value",
    [
        {MARK: {"difficulty": "any", "build": 10}},
        _with("claude", difficulty="hard", build=50, review=50, **{MARK: 1}),
        _with("claude", difficulty=MARK, build=50, review=50),
        _with("claude", difficulty="hard", build=MARK, review=50),
        _ExplodingItems({MARK: MARK}),
    ],
)
def test_a_refusal_carries_no_part_of_the_value(value):
    with pytest.raises(OverrideRefused) as refused:
        parse_override(value, SHIPPED)
    error = refused.value
    assert MARK not in str(error) and MARK not in repr(error) and MARK not in repr(error.args)
    assert error.__cause__ is None and error.__context__ is None
