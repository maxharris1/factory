from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class RoundFindings:
    round: int
    blocking: frozenset[str]
    repeat: str | None


@dataclass(frozen=True)
class Continue:
    reduced: bool


@dataclass(frozen=True)
class Repeated:
    rule_id: str
    earlier_round: int


@dataclass(frozen=True)
class Stalled:
    rounds: tuple[int, int]
    ids: frozenset[str]


Convergence = Continue | Repeated | Stalled


def _new_low(earlier: Sequence[RoundFindings], current: RoundFindings) -> bool:
    return bool(current.blocking) and all(len(current.blocking) < len(e.blocking) for e in earlier)


def converge(earlier: Sequence[RoundFindings], current: RoundFindings) -> Convergence:
    earlier = sorted(earlier, key=lambda findings: findings.round)
    if not earlier or _new_low(earlier, current):
        return Continue(reduced=True)
    previous = earlier[-1]
    carried = current.blocking & previous.blocking
    if carried:
        named = current.repeat if current.repeat is not None and current.repeat in carried else None
        return Repeated(rule_id=named or min(carried), earlier_round=previous.round)
    if len(earlier) > 1 and not _new_low(earlier[:-1], previous):
        return Stalled(rounds=(previous.round, current.round), ids=current.blocking)
    return Continue(reduced=False)
