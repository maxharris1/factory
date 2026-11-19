from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from lib.dispatch.verdict.convergence import (
    Continue,
    Convergence,
    Repeated,
    RoundFindings,
    Stalled,
    converge,
)

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "replay_stops.py"
_spec = importlib.util.spec_from_file_location("replay_stops", _SCRIPT)
assert _spec is not None and _spec.loader is not None
replay_stops = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = replay_stops
_spec.loader.exec_module(replay_stops)


def rounds(*sets: str, repeat: dict[int, str] | None = None) -> list[RoundFindings]:
    return [
        RoundFindings(number, frozenset(ids.split()), (repeat or {}).get(number))
        for number, ids in enumerate(sets, start=1)
    ]


def last(history: list[RoundFindings]) -> Convergence:
    return converge(history[:-1], history[-1])


@pytest.mark.parametrize(
    ("history", "expected"),
    [
        pytest.param(rounds("f-1 f-2 f-3"), Continue(reduced=True), id="first round"),
        pytest.param(rounds("", ""), Continue(reduced=False), id="empty after empty is flat"),
        pytest.param(
            rounds("f-1 f-2 f-3 f-4 f-5", "f-2 f-6 f-7"),
            Continue(reduced=True),
            id="5 to 3 with a carried id continues",
        ),
        pytest.param(
            rounds("f-1 f-2 f-3 f-4 f-5", "f-2 f-6 f-7", repeat={2: "f-2"}),
            Continue(reduced=True),
            id="5 to 3 naming repeat= continues",
        ),
        pytest.param(
            rounds("f-1 f-2 f-3", "f-4 f-5 f-6"),
            Continue(reduced=False),
            id="one flat round with fresh ids gets another try",
        ),
        pytest.param(
            rounds("f-1 f-2 f-3", "f-4 f-5 f-6", "f-7 f-8 f-9"),
            Stalled(rounds=(2, 3), ids=frozenset({"f-7", "f-8", "f-9"})),
            id="3, 3, 3 stalls at round 3",
        ),
        pytest.param(
            rounds("f-1 f-2 f-3", "f-4 f-5", "f-6 f-7 f-8", "f-9 f-10"),
            Stalled(rounds=(3, 4), ids=frozenset({"f-9", "f-10"})),
            id="3, 2, 3, 2 stalls at round 4",
        ),
        pytest.param(
            rounds("f-1 f-2 f-3", "f-2 f-4 f-5"),
            Repeated(rule_id="f-2", earlier_round=1),
            id="3, 3 with f-2 carried repeats",
        ),
        pytest.param(
            rounds("f-1 f-2 f-3", "f-1 f-2 f-4", repeat={2: "f-2"}),
            Repeated(rule_id="f-2", earlier_round=1),
            id="the named repeat= wins among carried ids",
        ),
        pytest.param(
            rounds("f-1 f-2 f-3", "f-4", ""),
            Continue(reduced=False),
            id="an empty set after a new low is one flat round",
        ),
        pytest.param(
            rounds("f-1 f-2", "", "f-3"),
            Stalled(rounds=(2, 3), ids=frozenset({"f-3"})),
            id="an empty set is flat, not a new low",
        ),
    ],
)
def test_converge_rule(history: list[RoundFindings], expected: Convergence) -> None:
    assert last(history) == expected


def test_converge_reads_earlier_rounds_in_any_order() -> None:
    history = rounds("f-1 f-2 f-3", "f-4 f-5", "f-6 f-7 f-8", "f-9 f-10")
    assert converge(list(reversed(history[:-1])), history[-1]) == last(history)


def revise(number: int, ids: str, repeat: str | None = None, unsat: bool = False) -> replay_stops.Entry:
    return RoundFindings(number, frozenset(ids.split()) | ({repeat} if repeat else set()), repeat), unsat


APPROVE = None

REPLAYED_WEEK: dict[str, tuple[list[replay_stops.Entry | None], list[str | None]]] = {
    "factory#1072": (
        [revise(1, "ac-1 ac-3 f-1 f-2 f-3 f-4 f-5 f-6"), revise(2, "ac-1 f-1", "f-1"), APPROVE],
        ["continue", "continue", None],
    ),
    "factory#726": (
        [
            revise(1, "ac-1 ac-2 ac-4 f-1 f-2"),
            revise(2, "ac-1 ac-4 f-2 f-3", "f-2"),
            revise(3, "ac-1 ac-4 f-2", "f-2"),
            revise(4, "f-4"),
            APPROVE,
        ],
        ["continue", "continue", "continue", "continue", None],
    ),
    "factory#131": (
        [
            revise(1, "ac-1 ac-2 ac-5 ac-6 ac-7 f-1"),
            revise(2, "ac-5", "ac-5"),
            revise(3, "ac-1 ac-2", "ac-2"),
            revise(4, "ac-1", "ac-1"),
            revise(5, "ac-1", "ac-1"),
            revise(6, "ac-1", "ac-1"),
            revise(7, "ac-1", "ac-1"),
            revise(8, "ac-5 ac-6", "ac-5"),
            revise(9, "ac-2 ac-6", "ac-6"),
            revise(10, "ac-1 ac-2 ac-4 ac-6", "ac-6"),
            APPROVE,
            APPROVE,
        ],
        ["continue", "continue", "continue", "repeat", "repeat", "repeat", "repeat", "stall", "repeat", "repeat",
         None, None],
    ),
    "aut0#1472": (
        [
            revise(1, "ac-2 ac-3 ac-4 ac-5 f-1 f-2 f-3 f-4"),
            revise(2, "ac-2 ac-3 ac-4 ac-5 f-1 f-2", "f-1"),
            revise(3, "ac-2 ac-3 ac-4 ac-5 f-1 f-2", "f-1"),
            revise(4, "ac-5 f-5"),
            revise(5, "ac-5 f-3", "f-3"),
            APPROVE,
        ],
        ["continue", "continue", "repeat", "continue", "repeat", None],
    ),
    "aut0#1471": (
        [
            revise(1, "ac-2 f-1 f-2"),
            revise(2, "ac-2 f-1 f-2", "f-1"),
            *(revise(number, "ac-2 f-1", "f-1") for number in range(3, 8)),
            revise(8, "ac-2 f-2"),
            revise(9, "ac-2 f-2", "f-2"),
            revise(10, "ac-2 f-2 f-3", "f-2"),
            revise(11, "ac-2 f-3", "f-3"),
            APPROVE,
        ],
        ["continue", "repeat", "continue", *["repeat"] * 8, None],
    ),
    "aut0#1458": (
        [
            revise(1, "ac-1 ac-2 f-1 f-2 f-3 f-4"),
            revise(2, "ac-1 f-2 f-3", "f-2"),
            revise(3, "ac-1 f-2", "f-2"),
            revise(4, "ac-1 f-3", "f-3"),
            revise(5, "ac-1 f-3", "f-3"),
        ],
        ["continue", "continue", "continue", "repeat", "repeat"],
    ),
    "factory#939": (
        [
            revise(1, "ac-1 ac-2 ac-3 f-1 f-2 f-3 f-4"),
            revise(2, "ac-1 ac-3 f-1 f-3", "f-1"),
            revise(3, "ac-2 ac-3 ac-4 f-1 f-5 f-6", "f-1"),
            revise(4, "ac-3"),
            revise(5, "ac-3"),
            revise(6, "ac-3"),
        ],
        ["continue", "continue", "repeat", "continue", "repeat", "repeat"],
    ),
    "Cleffy#125": (
        [revise(1, "f-1 f-2"), revise(2, "")],
        ["continue", "continue"],
    ),
    "aut0#1481": (
        [revise(1, "f-1", unsat=True), revise(2, "f-1", "f-1", unsat=True), revise(3, "", unsat=True)],
        ["unsat", "unsat", "unsat"],
    ),
}


@pytest.mark.parametrize("pr", list(REPLAYED_WEEK))
def test_replayed_week(pr: str) -> None:
    series, expected = REPLAYED_WEEK[pr]
    assert replay_stops.walk(series) == expected
