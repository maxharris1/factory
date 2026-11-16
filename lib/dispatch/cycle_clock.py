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


@dataclass
class CycleClock:
    wall: Callable[[], float] = time.time
    started_at: float | None = None
    last_cycle_seconds: float | None = None
    _rows: list[tuple[str, float, bool]] = field(default_factory=list)
    _sources: dict[str, dict[str, float]] = field(default_factory=dict)
    _open: str | None = None

    def start(self) -> str:
        self._rows = []
        self._sources = {}
        self._open = None
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

    def add_source(self, name: str, seconds: float) -> None:
        if self._open is None:
            return
        spent = self._sources.setdefault(self._open, {})
        spent[name] = spent.get(name, 0.0) + seconds

    @contextmanager
    def source(self, name: str) -> Iterator[None]:
        began = self.wall()
        try:
            yield
        finally:
            self.add_source(name, self.wall() - began)

    def end(self) -> str:
        ended = self.wall()
        started = ended if self.started_at is None else self.started_at
        self.last_cycle_seconds = ended - started
        self.started_at = None
        parts = []
        for name, seconds, failed in self._rows:
            part = f"{name}{'!' if failed else ''} {seconds:.1f}s"
            sources = self._sources.get(name)
            if sources:
                part += " [" + ", ".join(
                    f"{source} {spent:.1f}s" for source, spent in sources.items()
                ) + "]"
            parts.append(part)
        return (
            f"orch poll: cycle end {et(ended)} ({self.last_cycle_seconds:.1f}s): "
            + ", ".join(parts)
        )

    def state(self) -> tuple[float | None, float | None]:
        return self.started_at, self.last_cycle_seconds
