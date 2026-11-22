from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal, get_args, get_origin, get_type_hints

import pytest

from lib import handoff
from lib.handoff import parse_comment
from lib.rule_engine import (
    Escalate,
    Facts,
    HeadProbe,
    Land,
    MovedHeadFacts,
    Outcome,
    Ruling,
    StopFacts,
    Wait,
    decide,
    finding_id,
    git_path,
    go_header,
    sha,
)

STOP = StopFacts(
    site="cap",
    stop="capped",
    verdict_id=4242,
    finding_ids=(finding_id("f-1"), finding_id("ac-2")),
    repeat_stops=0,
    prior_grants=frozenset(),
    round=3,
    cap=3,
    stuck=False,
)
MOVED = MovedHeadFacts(
    ok_head=sha("a" * 40),
    head=sha("b" * 40),
    probe=HeadProbe(
        parents=(sha("c" * 40),),
        second_parent_on_base=False,
        remerge_empty=True,
        changed_paths=(git_path("lib/rule_engine.py"),),
        scan="clean",
    ),
    held_patterns=(git_path("**/*.lock"),),
    ci="success",
)


@dataclass(frozen=True)
class _Stub:
    name: str
    applies_to: bool
    outcome: Outcome

    def applies(self, facts: Facts) -> bool:
        return self.applies_to

    def rule(self, facts: Facts) -> Ruling:
        return Ruling(self.name, self.outcome, ())


@dataclass(frozen=True)
class _Loose:
    stop: Literal["capped"]
    note: str | None


@pytest.mark.parametrize("facts", [STOP, MOVED])
def test_an_empty_registry_escalates_no_rule(facts: Facts) -> None:
    assert decide(facts) == Ruling("no-rule", Escalate("no-rule"), ())


def test_the_first_applying_rule_rules_and_a_rule_that_does_not_apply_is_skipped() -> None:
    rules = [_Stub("skipped", False, Land()), _Stub("first", True, Wait()), _Stub("second", True, Land())]
    assert decide(STOP, rules) == Ruling("first", Wait(), ())


@pytest.mark.parametrize(
    "parse, text",
    [
        (finding_id, "merge now"),
        (finding_id, "f-0"),
        (sha, "HEAD"),
        (sha, "abc12"),
        (git_path, "../x"),
        (git_path, "a/../b"),
        (git_path, "/etc/passwd"),
        (git_path, "a\nb"),
        (git_path, "a\x00b"),
        (git_path, ""),
    ],
)
def test_a_constructor_refuses_text_that_is_not_its_kind(parse: Any, text: str) -> None:
    with pytest.raises(ValueError):
        parse(text)


@pytest.mark.parametrize(
    "parse, text",
    [
        (finding_id, "ac-1"),
        (finding_id, "f-12"),
        (sha, "abc1234"),
        (sha, "0" * 40),
        (git_path, "lib/rule_engine.py"),
        (git_path, "docs/..notes.md"),
        (git_path, "**/*.lock"),
    ],
)
def test_a_constructor_returns_text_of_its_kind(parse: Any, text: str) -> None:
    assert parse(text) == text


def _leaves(hint: Any) -> Iterator[Any]:
    if dataclasses.is_dataclass(hint):
        for field_hint in get_type_hints(hint).values():
            yield from _leaves(field_hint)
    elif get_origin(hint) is Literal:
        yield Literal
    elif get_args(hint):
        for arg in get_args(hint):
            if arg is not Ellipsis:
                yield from _leaves(arg)
    else:
        yield hint


@pytest.mark.parametrize("facts_type", [StopFacts, HeadProbe, MovedHeadFacts])
def test_no_fact_field_is_a_bare_str(facts_type: type) -> None:
    assert str not in set(_leaves(facts_type))


def test_the_field_walk_flags_a_bare_str() -> None:
    assert str in set(_leaves(_Loose))


@pytest.mark.parametrize("round, cap", [(4, 3), (1, 1)])
def test_go_header_is_a_go_to_fixing_at_round_over_cap(round: int, cap: int) -> None:
    line = go_header(round, cap)
    assert isinstance(line, str)
    assert parse_comment(line).as_dict() == {
        "kind": "go",
        "from": "orch",
        "state": "fixing",
        "round": f"{round}/{cap}",
    }


def test_go_header_text() -> None:
    assert go_header(4, 3) == "<!-- orch:v1 kind=go from=orch round=4/3 state=fixing -->"


@pytest.mark.parametrize("round, cap", [(0, 3), (1, 0), (-1, 3), (1, -3)])
def test_go_header_escalates_a_round_or_cap_below_one(round: int, cap: int) -> None:
    assert go_header(round, cap) == Escalate("header-invalid")


def test_go_header_escalates_a_header_that_parses_back_to_other_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        handoff, "format_header", lambda fields: "<!-- orch:v1 kind=go from=orch round=4/3 state=review -->"
    )
    assert go_header(4, 3) == Escalate("header-invalid")
