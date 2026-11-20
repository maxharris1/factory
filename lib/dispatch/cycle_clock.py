"""Per-cycle phase timing for poller.log and `orch status` (#440)."""

from __future__ import annotations

import math
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Literal

from lib.health import et

PHASES = (
    "linear-states",
    "feed",
    "github-candidates",
    "linear-candidates",
    "tracked-read",
    "process",
    "linear-moves",
    "merge-queue",
    "verify-handoffs",
    "reconcile-jobs",
    "ci-board",
    "factory-reads",
    "ci-pool",
    "after-poll",
)
AFTER_POLL_STEPS = ("backup", "dashboard-push", "self-deploy")
SLOW_STEP_SECONDS = 10.0
SLOW_CYCLE_SECONDS = 300.0
LOCKED_CYCLES = 3
CALM_CYCLES = 3
POLLER_REPO = "orch"
POLLER_TICKET = "poller"
SLOW_CYCLE = "slow-cycle"
LOCK_ERRORS = "lock-errors"
TOP_ROUTES = 8

CallKind = Literal["billed", "304", "cached", "graphql"]


@dataclass
class CallLedger:
    billed: Counter[str] = field(default_factory=Counter)
    free: Counter[str] = field(default_factory=Counter)
    graphql: int = 0
    core: tuple[int, int, float] | None = None

    def add(self, route: str, kind: CallKind, count: int = 1) -> None:
        if kind == "graphql":
            self.graphql += count
        elif kind == "billed":
            self.billed[route] += count
        else:
            self.free[kind] += count

    def summary(self, now: float) -> str:
        if not (self.billed or self.free or self.graphql):
            return ""
        line = f"; github core {self.billed.total()}"
        if self.billed:
            ranked = sorted(self.billed.items(), key=lambda row: (-row[1], row[0]))
            routes = [f"{route} {count}" for route, count in ranked[:TOP_ROUTES]]
            if len(ranked) > TOP_ROUTES:
                routes.append(f"other {sum(count for _, count in ranked[TOP_ROUTES:])}")
            line += " [" + ", ".join(routes) + "]"
        line += (
            f", free {self.free.total()} (304 {self.free['304']}, cached {self.free['cached']}),"
            f" graphql {self.graphql}"
        )
        if self.core is not None:
            remaining, limit, reset = self.core
            line += f"; core {remaining}/{limit} resets {max(0, math.ceil((reset - now) / 60))}m"
        return line


@dataclass
class CycleClock:
    wall: Callable[[], float] = time.time
    monotonic: Callable[[], float] = time.monotonic
    started_at: float | None = None
    last_cycle_seconds: float | None = None
    _rows: list[tuple[str, float, bool]] = field(default_factory=list)
    _parts: dict[str, dict[str, float]] = field(default_factory=dict)
    _open: str | None = None
    _ended: tuple[float, float] | None = None
    _calls: CallLedger = field(default_factory=CallLedger)
    _deferrals: Counter[str] = field(default_factory=Counter)

    @property
    def open_phase(self) -> str | None:
        return self._open

    @property
    def billed_calls(self) -> int:
        return self._calls.billed.total()

    def start(self) -> str:
        self._rows = []
        self._calls = CallLedger()
        self._deferrals = Counter()
        self._parts = {}
        self._open = None
        self._ended = None
        self.started_at = self.wall()
        return f"orch poll: cycle start {et(self.started_at)}"

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        if name not in PHASES:
            raise ValueError(f"unknown poll phase {name!r}")
        began = self.wall()
        failed = True
        self._open = name
        try:
            yield
            failed = False
        finally:
            self._open = None
            self._rows.append((name, self.wall() - began, failed))

    @contextmanager
    def step(self, name: str) -> Iterator[None]:
        if name not in AFTER_POLL_STEPS:
            raise ValueError(f"unknown after-poll step {name!r}")
        began = self.wall()
        try:
            yield
        finally:
            self._add("after-poll", name, self.wall() - began)

    def add_source(self, name: str, seconds: float) -> None:
        if self._open is not None:
            self._add(self._open, name, seconds)

    @contextmanager
    def source(self, name: str) -> Iterator[None]:
        began = self.wall()
        try:
            yield
        finally:
            self.add_source(name, self.wall() - began)

    def add_call(self, route: str, kind: CallKind, count: int = 1) -> None:
        self._calls.add(route, kind, count)

    def add_deferral(self, reason: str) -> None:
        self._deferrals[reason] += 1

    def note_core(self, remaining: int, limit: int, reset_epoch: float) -> None:
        self._calls.core = (remaining, limit, reset_epoch)

    def _add(self, phase: str, name: str, seconds: float) -> None:
        parts = self._parts.setdefault(phase, {})
        parts[name] = parts.get(name, 0.0) + seconds

    def end(self) -> str:
        ended = self.wall()
        self._ended = (ended, self.monotonic())
        started = ended if self.started_at is None else self.started_at
        self.last_cycle_seconds = ended - started
        self.started_at = None
        parts = []
        for name, seconds, failed in self._rows:
            part = f"{name}{'!' if failed else ''} {seconds:.1f}s"
            if self._parts.get(name):
                part += " [" + ", ".join(
                    f"{source} {spent:.1f}s" for source, spent in self._parts[name].items()
                ) + "]"
            parts.append(part)
        return (
            f"orch poll: cycle end {et(ended)} ({self.last_cycle_seconds:.1f}s): "
            + ", ".join(parts)
            + self._calls.summary(ended)
            + (
                "; deferred " + ", ".join(
                    f"{reason} {count}" for reason, count in sorted(self._deferrals.items())
                )
                if self._deferrals
                else ""
            )
        )

    def between(self, asked: float) -> str | None:
        if self._ended is None:
            return None
        wall = self.wall() - self._ended[0]
        monotonic = self.monotonic() - self._ended[1]
        elapsed = max(wall, monotonic)
        if elapsed - asked <= SLOW_STEP_SECONDS:
            return None
        detail = f"asked {asked:g}s, monotonic {monotonic:.1f}s"
        if wall - monotonic > SLOW_STEP_SECONDS:
            detail += f", clock jumped {wall - monotonic:.1f}s"
        elif monotonic - wall > SLOW_STEP_SECONDS:
            detail += f", clock stepped back {monotonic - wall:.1f}s"
        return f"orch poll: between cycles {elapsed:.1f}s: sleep {elapsed:.1f}s ({detail})"

    def slowest(self) -> tuple[str, float] | None:
        return max(((name, seconds) for name, seconds, _ in self._rows),
                   key=lambda row: row[1], default=None)

    def state(self) -> tuple[float | None, float | None]:
        return self.started_at, self.last_cycle_seconds


@dataclass
class CycleAlarm:
    raise_after: int
    bad: int = 0
    good: int = 0
    may_be_up: bool = True

    def observe(self, bad: bool) -> Literal["raise", "clear"] | None:
        if bad:
            self.bad, self.good = self.bad + 1, 0
            if self.bad >= self.raise_after:
                self.may_be_up = True
                return "raise"
            return None
        self.bad, self.good = 0, self.good + 1
        if self.may_be_up and self.good >= CALM_CYCLES:
            self.may_be_up = False
            return "clear"
        return None


def cycle_alarms() -> dict[str, CycleAlarm]:
    return {SLOW_CYCLE: CycleAlarm(raise_after=1), LOCK_ERRORS: CycleAlarm(raise_after=LOCKED_CYCLES)}
