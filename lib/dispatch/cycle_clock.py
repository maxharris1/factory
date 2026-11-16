"""Per-cycle phase timing for poller.log and `orch status` (#440)."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from lib.health import et

PHASES = (
    "feed",
    "github-candidates",
    "linear-candidates",
    "tracked-read",
    "process",
    "merge-queue",
    "verify-handoffs",
    "reconcile-jobs",
    "factory-reads",
    "after-poll",
)
AFTER_POLL_STEPS = ("backup", "dashboard-push", "self-deploy")
SLOW_STEP_SECONDS = 10.0


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

    def start(self) -> str:
        self._rows = []
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

    def state(self) -> tuple[float | None, float | None]:
        return self.started_at, self.last_cycle_seconds
