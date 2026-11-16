"""Remote factory checkout sync state (issue #297).

One `device_sync` row per remote deploy target, keyed (device, bot): each
bot on a remote device has its own factory checkout (machines.yml
factory_root, e.g. /Users/{bot}/factory), so the unit is a bot's checkout.
The table lives in orch.db beside `device_slots`, so the deploy pass
(lib/device_deploy.py) marks a target deploying in one BEGIN IMMEDIATE
transaction, and DevicePool.claim reads the same rows in its own: no job
lands on a device while its checkout is being updated.

States:

* synced: HEAD is the target, on main, and every pinned skill file matches.
* lagging: not synced, until the next deploy pass moves it. The lagging
  bot's harness is not placed on the device.
* deploying: a deploy runs now. No harness is placed on the device.
* missing: the checkout does not exist; the next pass clones it.
* refused: the deploy would discard local work (staged or modified files,
  an untracked or ignored file in the checkout's way, a merge or rebase in
  progress, local commits) or failed. Escalated once.
* unreachable: ssh failed. Not a placement block: the device probe and the
  start's own preflight already cover an offline device.

A device with no row is placeable, so a new device is not dead before its
first pass; the start's preflight still checks its HEAD.

The box has rows too, written by lib/self_deploy.py (device bot "",
deploying while its checkout moves) and by each bot's waiter: lagging when
a box start was refused, and synced or refused for the skills installed in
its own home (#345). Self-deploy drops its own row and the lagging rows
once the box HEAD is origin/main, and `orch heal devices` never forgets
box rows.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

TABLE = "device_sync"

SYNCED = "synced"
LAGGING = "lagging"
DEPLOYING = "deploying"
MISSING = "missing"
REFUSED = "refused"
UNREACHABLE = "unreachable"
STATES = (SYNCED, LAGGING, DEPLOYING, MISSING, REFUSED, UNREACHABLE)

BLOCKS_HARNESS = frozenset({LAGGING, DEPLOYING, MISSING, REFUSED})
ALARM_AT_ONCE = frozenset({MISSING, REFUSED})
ALARM_AFTER_GRACE = frozenset({LAGGING, DEPLOYING})
SYNC_GRACE_SECONDS = 15 * 60

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    device TEXT NOT NULL,
    bot TEXT NOT NULL,
    harness TEXT NOT NULL,
    checkout TEXT NOT NULL,
    target TEXT,
    sha TEXT,
    state TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    since REAL NOT NULL,
    checked_at REAL NOT NULL,
    PRIMARY KEY (device, bot)
)
"""


@dataclass(frozen=True)
class Row:
    device: str
    bot: str
    harness: str
    checkout: str
    target: str | None
    sha: str | None
    state: str
    detail: str
    since: float
    checked_at: float

    def needs_attention(self, now: float) -> bool:
        if self.state in ALARM_AT_ONCE:
            return True
        return self.state in ALARM_AFTER_GRACE and now - self.since >= SYNC_GRACE_SECONDS


@dataclass(frozen=True)
class Blocked:
    """Where placement must not go: (device, harness) pairs, and whole devices."""

    pairs: frozenset[tuple[str, str]] = frozenset()
    devices: frozenset[str] = frozenset()

    def blocks(self, device: str, harness: str) -> bool:
        return device in self.devices or (device, harness) in self.pairs


NOTHING_BLOCKED = Blocked()


def ensure(connection: sqlite3.Connection) -> None:
    connection.execute(_SCHEMA)


def _exists(connection: sqlite3.Connection) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)
        ).fetchone()
        is not None
    )


def read(connection: sqlite3.Connection) -> list[Row]:
    if not _exists(connection):
        return []
    return [
        Row(
            device=str(row[0]),
            bot=str(row[1]),
            harness=str(row[2]),
            checkout=str(row[3]),
            target=None if row[4] is None else str(row[4]),
            sha=None if row[5] is None else str(row[5]),
            state=str(row[6]),
            detail=str(row[7]),
            since=float(row[8]),
            checked_at=float(row[9]),
        )
        for row in connection.execute(
            f"SELECT device, bot, harness, checkout, target, sha, state, detail, since, checked_at "
            f"FROM {TABLE} ORDER BY device, bot"
        )
    ]


def blocked_by(rows: Iterable[Row]) -> Blocked:
    pairs: set[tuple[str, str]] = set()
    devices: set[str] = set()
    for row in rows:
        if row.state in BLOCKS_HARNESS:
            pairs.add((row.device, row.harness))
        if row.state == DEPLOYING:
            devices.add(row.device)
    return Blocked(frozenset(pairs), frozenset(devices))


def blocked(connection: sqlite3.Connection) -> Blocked:
    return blocked_by(read(connection))


def upsert(
    connection: sqlite3.Connection,
    *,
    device: str,
    bot: str,
    harness: str,
    checkout: str,
    target: str | None,
    sha: str | None,
    state: str,
    detail: str,
    now: float,
    replaces_deploying: bool = False,
) -> None:
    """Write one row in the caller's transaction; `since` restarts only when the state changes."""
    if state not in STATES:
        raise ValueError(f"unknown sync state {state!r}")
    ensure(connection)
    connection.execute(
        f"""
        INSERT INTO {TABLE} (
            device, bot, harness, checkout, target, sha, state, detail, since, checked_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(device, bot) DO UPDATE SET
            since = CASE WHEN {TABLE}.state = excluded.state THEN {TABLE}.since
                         ELSE excluded.since END,
            harness = excluded.harness,
            checkout = excluded.checkout,
            target = excluded.target,
            sha = excluded.sha,
            state = excluded.state,
            detail = excluded.detail,
            checked_at = excluded.checked_at
        WHERE {TABLE}.state != '{DEPLOYING}' OR ?
        """,
        (device, bot, harness, checkout, target, sha, state, detail, now, now, replaces_deploying),
    )


def forget_except(
    connection: sqlite3.Connection,
    keep: Iterable[tuple[str, str]],
    keep_devices: Iterable[str] = (),
) -> None:
    """Drop rows for targets no longer in machines.yml, so a removed device never blocks."""
    if not _exists(connection):
        return
    kept = set(keep)
    devices = set(keep_devices)
    for row in read(connection):
        if (row.device, row.bot) not in kept and row.device not in devices:
            connection.execute(
                f"DELETE FROM {TABLE} WHERE device = ? AND bot = ?", (row.device, row.bot)
            )


def clear(
    connection: sqlite3.Connection,
    device: str,
    states: Iterable[str] = STATES,
    *,
    bots: Iterable[str] | None = None,
) -> None:
    if not _exists(connection):
        return
    wanted = tuple(states)
    sql = f"DELETE FROM {TABLE} WHERE device = ? AND state IN ({', '.join('?' for _ in wanted)})"
    params: tuple[str, ...] = (device, *wanted)
    if bots is not None:
        names = tuple(bots)
        sql += f" AND bot IN ({', '.join('?' for _ in names)})"
        params += names
    connection.execute(sql, params)


@dataclass(frozen=True)
class Observed:
    """One target as the pass saw it. sha None with reachable True: no checkout."""

    reachable: bool
    sha: str | None = None
    branch: str | None = None
    skills_ok: bool = False

    @property
    def missing(self) -> bool:
        return self.reachable and self.sha is None


OBSERVE_UNREACHABLE = "unreachable"
KEEP_SYNCED = "synced"
DEPLOY = "deploy"


def step(observed: Observed, target: str) -> str:
    if not observed.reachable:
        return OBSERVE_UNREACHABLE
    if observed.sha == target and observed.branch == "main" and observed.skills_ok:
        return KEEP_SYNCED
    return DEPLOY


def waiting_state(observed: Observed) -> str:
    return MISSING if observed.missing else LAGGING
