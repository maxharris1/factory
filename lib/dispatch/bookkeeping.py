"""Poller bookkeeping writes: retried briefly on a held orch.db lock, else
deferred to the next cycle (#839)."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

from lib.dispatch.cycle_clock import PHASES
from lib.wait import is_locked, lock_backoff

BOOKKEEPING_BUSY_SECONDS = 2.0
BOOKKEEPING_ATTEMPTS = 3
LOCK_MISS_BUDGET = 5

T = TypeVar("T")
_LOCKED = object()


@dataclass
class LockLedger:
    phase: str = PHASES[0]
    _sites: dict[str, list[str]] = field(default_factory=dict)

    def reset(self) -> None:
        self.phase = PHASES[0]
        self._sites = {}

    def missed(self, site: str) -> None:
        self._sites.setdefault(self.phase, []).append(site)

    @property
    def misses(self) -> int:
        return sum(len(sites) for sites in self._sites.values())

    def spent(self) -> bool:
        return self.misses >= LOCK_MISS_BUDGET

    def worst_phase(self) -> str | None:
        return max(self._sites, key=lambda phase: len(self._sites[phase]), default=None)

    def summary(self) -> str:
        return ", ".join(
            f"{phase} {len(sites)} ({', '.join(dict.fromkeys(sites))})"
            for phase, sites in self._sites.items()
        )


def bookkeeping_write(
    connect: Callable[[], sqlite3.Connection],
    work: Callable[[sqlite3.Connection], None],
    *,
    site: str,
    ledger: LockLedger | None,
    sleep: Callable[[float], None] | None = None,
) -> bool:
    """Run `work` in one BEGIN IMMEDIATE transaction; False when deferred."""
    if ledger is not None and ledger.spent():
        ledger.missed(site)
        return False
    return _attempts(connect, work, site=site, ledger=ledger, sleep=sleep) is not _LOCKED


def locked_write(
    connect: Callable[[], sqlite3.Connection],
    work: Callable[[sqlite3.Connection], T],
    *,
    site: str,
    ledger: LockLedger | None,
    sleep: Callable[[float], None] | None = None,
) -> T:
    """`work`'s result from one BEGIN IMMEDIATE transaction, even past the miss budget;
    raises database is locked when every attempt finds the lock held."""
    result = _attempts(connect, work, site=site, ledger=ledger, sleep=sleep)
    if result is _LOCKED:
        raise sqlite3.OperationalError("database is locked")
    return result


def _attempts(
    connect: Callable[[], sqlite3.Connection],
    work: Callable[[sqlite3.Connection], T],
    *,
    site: str,
    ledger: LockLedger | None,
    sleep: Callable[[float], None] | None,
) -> T | object:
    pause = sleep if sleep is not None else time.sleep
    for attempt in range(1, BOOKKEEPING_ATTEMPTS + 1):
        result = _commit(connect, work)
        if result is not _LOCKED:
            return result
        if attempt < BOOKKEEPING_ATTEMPTS:
            pause(lock_backoff(attempt))
    if ledger is not None:
        ledger.missed(site)
    return _LOCKED


def must_write(written: bool) -> None:
    if not written:
        raise sqlite3.OperationalError("database is locked")


def _commit(
    connect: Callable[[], sqlite3.Connection],
    work: Callable[[sqlite3.Connection], T],
) -> T | object:
    connection = connect()
    try:
        connection.execute(f"PRAGMA busy_timeout = {int(BOOKKEEPING_BUSY_SECONDS * 1000)}")
        connection.execute("BEGIN IMMEDIATE")
        result = work(connection)
        connection.commit()
        return result
    except sqlite3.OperationalError as exc:
        if not is_locked(exc):
            raise
        return _LOCKED
    finally:
        connection.close()
