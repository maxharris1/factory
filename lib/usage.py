"""Best-fit routing and the per-harness usage reserve.

F1-07 (`orchestrate-project`) calls `route_job` with a loaded factory.md
config. Each owning bot calls `reserve_check` on its own harness before
it starts that job.

Fit for a role is that role's factory.md harness and model hint, then
the other harnesses in schema order. Usage does not reorder a harness
above the reserve. The reserve is the last 20% of a verified cycle
(`remaining <= 0.20`). A non-urgent job skips a harness in reserve.
An urgent job — a resumed paused step, past-ship work, or a job Max
marks urgent — may use it. A missing or unverified report is not a
reserve and does not block routing.

A route is both sides or neither. The reviewer is a different model
family from the builder this call selected. On a later round,
`built_by=(harness, model)` names what actually built: that is the
builder, and the reviewer is outside its family. Family resolution is
the check-config rule. A preferred harness check-config would refuse
(unknown, or no family) raises instead of routing around it. This
module does not read a usage account; lib/usage_readings.py's
`TelemetryUsage` reads the CLI's own usage readings from orch.db.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Never, Protocol

from lib.factory_md import FactoryConfig, resolved_family
from lib.schema import HARNESSES

RESERVE_REMAINING = 0.20

Role = Literal["builder", "reviewer"]


@dataclass(frozen=True)
class FitRow:
    rank: int
    harness: str
    model: str | None
    family: str | None


@dataclass(frozen=True)
class UsageReport:
    harness: str
    remaining: float
    verified: bool

    def __post_init__(self) -> None:
        if self.harness not in HARNESSES:
            raise ValueError(f"unknown harness {self.harness!r}")
        remaining = self.remaining
        if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
            raise ValueError("remaining must be a fraction from 0 to 1")
        if remaining != remaining or remaining < 0 or remaining > 1:
            raise ValueError("remaining must be a fraction from 0 to 1")
        if not isinstance(self.verified, bool):
            raise ValueError("verified must be a bool")


class UsageSource(Protocol):
    """A harness report, or None when this harness has no verified source."""

    def read(self, harness: str) -> UsageReport | None: ...


@dataclass(frozen=True)
class StaticUsage:
    """Reports held in memory. Used by tests and by a bot that already measured."""

    reports: Mapping[str, UsageReport]

    def read(self, harness: str) -> UsageReport | None:
        report = self.reports.get(harness)
        if report is None or report.harness != harness:
            return None
        return report


class NoUsage:
    """No harness has a verified usage source."""

    def read(self, harness: str) -> UsageReport | None:
        return None


@dataclass(frozen=True)
class Assignment:
    role: str
    harness: str
    model: str | None
    family: str
    using_reserve: bool


@dataclass(frozen=True)
class JobRoute:
    builder: Assignment | None
    reviewer: Assignment | None
    reason: str | None

    @property
    def ok(self) -> bool:
        return (
            self.builder is not None
            and self.reviewer is not None
            and self.reason is None
        )


@dataclass(frozen=True)
class ReserveCheck:
    harness: str
    start: bool
    reserve: bool
    reason: str


def job_is_urgent(
    *,
    resumed_paused_step: bool = False,
    past_ship: bool = False,
    marked_urgent: bool = False,
) -> bool:
    return resumed_paused_step or past_ship or marked_urgent


def fit_table(config: FactoryConfig, role: Role) -> tuple[FitRow, ...]:
    preferred, model = _preference(config, role)
    _known_family(preferred, model, role)
    ordered = (preferred, *[item for item in HARNESSES if item != preferred])
    rows: list[FitRow] = []
    for rank, harness in enumerate(ordered):
        hint = model if harness == preferred else None
        rows.append(FitRow(rank, harness, hint, _family(harness, hint)))
    return tuple(rows)


def route_job(
    config: FactoryConfig,
    *,
    usage: UsageSource | None = None,
    resumed_paused_step: bool = False,
    past_ship: bool = False,
    marked_urgent: bool = False,
    built_by: tuple[str, str | None] | None = None,
) -> JobRoute:
    """Pick a builder and a cross-family reviewer, or pick neither."""
    urgent = job_is_urgent(
        resumed_paused_step=resumed_paused_step,
        past_ship=past_ship,
        marked_urgent=marked_urgent,
    )
    source: UsageSource = usage if usage is not None else NoUsage()
    if built_by is None:
        builder_rows = fit_table(config, "builder")
        builder = _select(builder_rows, "builder", urgent, source, None)
        if builder is None:
            return JobRoute(None, None, _blocked(builder_rows, urgent, source, None))
    else:
        harness, model = built_by
        family = _known_family(harness, model, "built_by")
        builder = Assignment("builder", harness, model, family, False)
    reviewer_rows = fit_table(config, "reviewer")
    reviewer = _select(reviewer_rows, "reviewer", urgent, source, builder.family)
    if reviewer is None:
        return JobRoute(
            None,
            None,
            _blocked(reviewer_rows, urgent, source, builder.family),
        )
    return JobRoute(builder, reviewer, None)


def reserve_check(
    harness: str,
    usage: UsageSource | None = None,
    *,
    resumed_paused_step: bool = False,
    past_ship: bool = False,
    marked_urgent: bool = False,
) -> ReserveCheck:
    """Owning-bot check for this harness. Missing a source does not block."""
    if harness not in HARNESSES:
        raise ValueError(f"unknown harness {harness!r}")
    urgent = job_is_urgent(
        resumed_paused_step=resumed_paused_step,
        past_ship=past_ship,
        marked_urgent=marked_urgent,
    )
    source: UsageSource = usage if usage is not None else NoUsage()
    report = _report(source, harness)
    if report is None or not report.verified:
        return ReserveCheck(
            harness,
            start=True,
            reserve=False,
            reason="no-verified-source",
        )
    reserve = report.remaining <= RESERVE_REMAINING
    if not reserve:
        return ReserveCheck(harness, start=True, reserve=False, reason="above-reserve")
    if urgent:
        return ReserveCheck(harness, start=True, reserve=True, reason="urgent")
    return ReserveCheck(harness, start=False, reserve=True, reason="reroute")


def _preference(config: FactoryConfig, role: Role) -> tuple[str, str | None]:
    if role == "builder":
        return config.builder_harness, config.builder_model
    if role == "reviewer":
        return config.reviewer_harness, config.reviewer_model
    return _never(role)


def _select(
    rows: tuple[FitRow, ...],
    role: Role,
    urgent: bool,
    source: UsageSource,
    forbidden_family: str | None,
) -> Assignment | None:
    for row in rows:
        family = row.family
        if family is None:
            continue
        if forbidden_family is not None and family == forbidden_family:
            continue
        reserve = _in_reserve(_report(source, row.harness))
        if reserve and not urgent:
            continue
        return Assignment(role, row.harness, row.model, family, reserve)
    return None


def _blocked(
    rows: tuple[FitRow, ...],
    urgent: bool,
    source: UsageSource,
    forbidden_family: str | None,
) -> str:
    for row in rows:
        if row.family is None:
            continue
        if forbidden_family is not None and row.family == forbidden_family:
            continue
        if _in_reserve(_report(source, row.harness)) and not urgent:
            return "reserve"
    return "no-fit"


def _report(source: UsageSource, harness: str) -> UsageReport | None:
    report = source.read(harness)
    if report is None or report.harness != harness:
        return None
    return report


def _in_reserve(report: UsageReport | None) -> bool:
    if report is None or not report.verified:
        return False
    return report.remaining <= RESERVE_REMAINING


def _known_family(harness: str, model: str | None, label: str) -> str:
    if harness not in HARNESSES:
        raise ValueError(f"{label} harness {harness!r} is unknown")
    family = _family(harness, model)
    if family is None:
        raise ValueError(f"{label} family not established for {harness!r}")
    return family


def _family(harness: str, model: str | None) -> str | None:
    family, _hint = resolved_family(harness, model)
    return family


def _never(value: Never) -> Never:
    raise ValueError(f"unknown role {value!r}")
