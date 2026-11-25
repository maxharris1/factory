from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from lib.factory_md import FactoryConfig


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


@dataclass(frozen=True)
class Rounds:
    auto_extra: int


@dataclass(frozen=True)
class ConvergencePolicy:
    pass


StopPolicy = Rounds | ConvergencePolicy


def stop_policy(config: FactoryConfig) -> StopPolicy:
    return ConvergencePolicy() if config.stop_on == "convergence" else Rounds(config.auto_extra_rounds)


def policy_name(policy: StopPolicy) -> Literal["rounds", "convergence"]:
    return "convergence" if isinstance(policy, ConvergencePolicy) else "rounds"


def _carried(earlier: Sequence[RoundFindings], current: RoundFindings) -> Convergence:
    earlier = sorted(earlier, key=lambda findings: findings.round, reverse=True)
    previous = next((item for item in earlier if item.round == current.round - 1), None)
    if current.repeat is not None:
        holder = next((item for item in earlier if current.repeat in item.blocking), previous)
        return Repeated(current.repeat, 0 if holder is None else holder.round)
    if previous is None:
        return Continue(reduced=False)
    shared = current.blocking & previous.blocking
    return Repeated(min(shared), previous.round) if shared else Continue(reduced=False)


def review_step(policy: StopPolicy, earlier: Sequence[RoundFindings], current: RoundFindings) -> Convergence:
    match policy:
        case Rounds():
            return _carried(earlier, current)
        case ConvergencePolicy():
            return converge(earlier, current)


def cap_grant(policy: StopPolicy, step: Continue | Stalled, cap_extra: int, stuck: bool) -> int | None:
    if stuck:
        return None
    match policy:
        case Rounds(auto_extra):
            return cap_extra + 1 if cap_extra < auto_extra else None
        case ConvergencePolicy():
            return cap_extra + 1 if isinstance(step, Continue) else None


def grant_reason(policy: StopPolicy, granted: int, step: Continue | Stalled) -> str:
    match policy:
        case Rounds(auto_extra):
            return (
                f"Auto extra round {granted} of {auto_extra} "
                "under factory.md `review.auto_extra_rounds` (set by Max)."
            )
        case ConvergencePolicy() if isinstance(step, Continue) and step.reduced:
            return (
                f"Extra round {granted}: this round cut the open blocking findings to a new low, "
                "so review goes on under factory.md `review.stop_on: convergence`."
            )
        case ConvergencePolicy():
            return (
                f"Extra round {granted}: this round did not cut the open blocking findings. "
                "One flat round is allowed under factory.md `review.stop_on: convergence`."
            )
