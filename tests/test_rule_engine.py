from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal, get_args, get_origin, get_type_hints

import pytest

from lib import handoff
from lib.handoff import parse_comment
from lib.rule_engine import (
    Check,
    Escalate,
    Facts,
    FirstCapGrant,
    Grant,
    HeadProbe,
    Land,
    MovedHeadFacts,
    Outcome,
    Ruling,
    StopFacts,
    Wait,
    comment_id,
    decide,
    finding_id,
    git_path,
    go_header,
    sha,
)

STOP = StopFacts(
    site="cap",
    stop="capped",
    verdict_id=comment_id("4242"),
    finding_ids=(finding_id("f-1"), finding_id("ac-2")),
    repeat_stops=0,
    prior_grants=frozenset(),
    round=3,
    cap=3,
    stuck=False,
    policy="rounds",
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
    assert decide(facts, ()) == Ruling("no-rule", Escalate("no-rule"), ())


def test_the_first_applying_rule_rules_and_a_rule_that_does_not_apply_is_skipped() -> None:
    rules = [_Stub("skipped", False, Land()), _Stub("first", True, Wait()), _Stub("second", True, Land())]
    assert decide(STOP, rules) == Ruling("first", Wait(), ())


@pytest.mark.parametrize(
    "parse, text",
    [
        (finding_id, "merge now"),
        (finding_id, "f-0"),
        (comment_id, ""),
        (comment_id, "12 34"),
        (comment_id, "merge now"),
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
        (comment_id, "7083338990"),
        (comment_id, "revise-3"),
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


def test_a_first_cap_on_fresh_findings_grants_one_round_with_the_go_header() -> None:
    assert decide(STOP) == Ruling(
        "first-cap-grant",
        Grant("<!-- orch:v1 kind=go from=orch round=4/4 state=fixing -->"),
        (
            Check("stop", True, "capped"),
            Check("finding-ids", True, 2),
            Check("stuck", True, 0),
            Check("earlier-round", True, None),
            Check("policy", True, "rounds"),
            Check("header", True, "<!-- orch:v1 kind=go from=orch round=4/4 state=fixing -->"),
        ),
    )


@pytest.mark.parametrize(
    "change, failed",
    [
        ({"stop": "stalled"}, Check("stop", False, "stalled")),
        ({"finding_ids": ()}, Check("finding-ids", False, 0)),
        ({"stuck": True}, Check("stuck", False, 1)),
        ({"prior_grants": frozenset({finding_id("ac-2"), finding_id("f-9")})}, Check("earlier-round", False, "ac-2")),
        ({"prior_grants": None}, Check("earlier-round", False, "unknown")),
    ],
)
def test_a_cap_that_fails_one_check_escalates_on_that_check(change: dict, failed: Check) -> None:
    ruling = decide(dataclasses.replace(STOP, **change))
    assert ruling.rule == "first-cap-grant"
    assert ruling.outcome == Escalate(failed.name)
    assert failed in ruling.checks
    assert [check.name for check in ruling.checks if check.passed is False] == [failed.name]


def test_a_prior_grant_on_another_finding_still_grants() -> None:
    ruling = decide(dataclasses.replace(STOP, prior_grants=frozenset({finding_id("f-2")})))
    assert isinstance(ruling.outcome, Grant)


def test_a_cap_under_convergence_escalates_on_policy() -> None:
    ruling = decide(dataclasses.replace(STOP, policy="convergence"))
    assert ruling.outcome == Escalate("policy")
    assert [check for check in ruling.checks if check.passed is False] == [Check("policy", False, "convergence")]


def test_a_cap_under_rounds_grants() -> None:
    assert isinstance(decide(dataclasses.replace(STOP, policy="rounds")).outcome, Grant)


def test_a_cap_whose_header_does_not_round_trip_escalates_header_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        handoff, "format_header", lambda fields: "<!-- orch:v1 kind=go from=orch round=4/4 state=review -->"
    )
    ruling = decide(STOP)
    assert ruling.outcome == Escalate("header-invalid")
    assert ruling.checks[-1] == Check("header", False, None)


def test_first_cap_grant_does_not_rule_on_a_moved_head() -> None:
    assert not FirstCapGrant().applies(MOVED)
    assert decide(MOVED).rule == "moved-head-lands"


PURE_PROBE = dataclasses.replace(MOVED.probe, parents=(MOVED.ok_head, sha("c" * 40)), second_parent_on_base=True)


@pytest.mark.parametrize(
    "facts, outcome",
    [
        (dataclasses.replace(MOVED, probe=PURE_PROBE), Land()),
        (dataclasses.replace(MOVED, probe=PURE_PROBE, ci="pending"), Wait()),
        (MOVED, Escalate("pure-merge")),
        (
            dataclasses.replace(MOVED, probe=PURE_PROBE, held_patterns=(git_path("lib/**"),)),
            Escalate("held-paths"),
        ),
        (dataclasses.replace(MOVED, probe=None, ci="pending"), Escalate("probe-unknown")),
    ],
)
def test_moved_head_lands_runs_all_five_checks_and_rules_on_the_first_failure(
    facts: MovedHeadFacts, outcome: Outcome
) -> None:
    ruling = decide(facts)
    assert ruling.outcome == outcome
    names = ["pure-merge", "remerge-diff", "held-paths", "secrets-scan", "ci"]
    assert [check.name for check in ruling.checks] == names


REPEAT = dataclasses.replace(STOP, site="repeat", stop="repeat", finding_ids=(finding_id("f-1"),), repeat_stops=1, round=2)


@pytest.mark.parametrize("round, header", [(2, "round=3/3"), (3, "round=4/4")])
def test_a_first_repeat_stop_grants_the_round_and_the_cap_round_only_at_the_cap(round: int, header: str) -> None:
    ruling = decide(dataclasses.replace(REPEAT, round=round))
    line = f"<!-- orch:v1 kind=go from=orch {header} state=fixing -->"
    assert ruling == Ruling(
        "first-cap-grant",
        Grant(line),
        (
            Check("stop", True, "repeat"),
            Check("finding-ids", True, 1),
            Check("repeat-stops", True, 1),
            Check("stuck", True, 0),
            Check("earlier-round", True, None),
            Check("policy", True, "rounds"),
            Check("header", True, line),
        ),
    )


def test_a_first_repeat_stop_under_convergence_grants() -> None:
    assert isinstance(decide(dataclasses.replace(REPEAT, policy="convergence")).outcome, Grant)


@pytest.mark.parametrize(
    "change, failed",
    [
        ({"repeat_stops": 2}, Check("repeat-stops", False, 2)),
        ({"finding_ids": ()}, Check("finding-ids", False, 0)),
        ({"stuck": True}, Check("stuck", False, 1)),
        ({"prior_grants": frozenset({finding_id("f-1")})}, Check("earlier-round", False, "f-1")),
        ({"prior_grants": None}, Check("earlier-round", False, "unknown")),
    ],
)
def test_a_repeat_stop_that_fails_one_check_escalates_on_that_check(change: dict, failed: Check) -> None:
    ruling = decide(dataclasses.replace(REPEAT, **change))
    assert ruling.outcome == Escalate(failed.name)
    assert [check for check in ruling.checks if check.passed is False] == [failed]
