"""Component heartbeats: which parts of the factory are running (issue #92).

Every long-running part writes one heartbeat row every BEAT_SECONDS (15):

* `poller`: `orch poll` (not `--once`);
* `waiter`: each bot's `orch wait`, one row per device it can claim on
  (`--autostart` waiters: box devices only); a plain `orch wait` removes
  its rows when it exits, an autostart waiter marks them stopped;
* `linear-listener`: `orch linear-webhook serve`;
* `dashboard`: the dashboard writer, once per `orch dashboard push`
  snapshot (its interval is the push interval);
* `device-probe`: one row per enabled remote device in machines.yml, from
  an SSH round trip the poller makes every PROBE_SECONDS (60); only a
  device that answered counts as seen;
* `tunnel`: the Linear listener's reverse tunnel is not a factory process,
  so whatever runs it reports with `orch health beat tunnel --pid <pid>`.

A row is (component, device, bot, pid, started_at, last_seen, version,
interval_seconds, stopped_at, checked_at). Every value is an allowlisted
name, a number or a short git SHA (`HealthStore.beat` refuses anything
else), so no argv, environment value, path, URL or token can reach a row.

The rows live in their own SQLite file beside orch.db (`health_path`,
orch.health.db), not in orch.db: a heartbeat every 15 seconds from each
component would otherwise wake the dashboard's live push (which watches
orch.db for changes) and add writers to the dispatcher's database. The file
is created with orch.db's permission bits and group, so a waiter running as
another bot user can write it whenever it can write orch.db.

State (`classify`): up while the last beat is at most STALE_BEATS (3)
intervals old, stale up to DOWN_BEATS (8) intervals, then down; a clean
stop is down at once, and so is a component with no beat at all. A device
whose latest probe failed is at least stale. With the default 15 seconds,
stale after 45 seconds and down after 2 minutes; a probe (60 seconds) is
stale after 3 minutes and down after 8.

`build_report` adds the poller as down when the health file exists but has
no poller row, and flags each queued harness job that has no live (up or
stale) waiter for its bot on any device eligible for its harness as
"no waiter for <harness>". The dashboard's Health tab, its needs-attention
line, `orch status` and `orch health` all read the same report.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sqlite3
import stat
import sys
import threading
import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple, TextIO

from lib.device_monitor import remote_target
from lib.machines_yml import MachinesConfig, load_machines_yml
from lib.remote import RemoteTarget, Transport, probe_device
from lib.wait import OWNING_BOTS

ROOT = Path(__file__).resolve().parent.parent

TABLE = "component_heartbeats"
BEAT_SECONDS = 15.0
PROBE_SECONDS = 60.0
STALE_BEATS = 3
DOWN_BEATS = 8
BUSY_SECONDS = 5.0
MIN_INTERVAL = 5.0
MAX_INTERVAL = 3600.0
DEFAULT_DEVICE = "box"
PROBE_JOB_ID = "health-probe"

POLLER = "poller"
WAITER = "waiter"
LISTENER = "linear-listener"
TUNNEL = "tunnel"
DASHBOARD = "dashboard"
DEVICE_PROBE = "device-probe"
COMPONENTS = (POLLER, WAITER, LISTENER, TUNNEL, DASHBOARD, DEVICE_PROBE)

UP = "up"
STALE = "stale"
DOWN = "down"

TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
VERSION = re.compile(r"[0-9a-f]{7,40}|unknown")
_SHA = re.compile(r"[0-9a-f]{40}")
_BOT_HARNESS = {bot: harness for bot, harness in OWNING_BOTS.items()}
_HARNESS_BOT = {harness: bot for bot, harness in OWNING_BOTS.items()}

Log = Callable[[str], None]

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    component TEXT NOT NULL,
    device TEXT NOT NULL,
    bot TEXT NOT NULL DEFAULT '',
    pid INTEGER,
    started_at REAL,
    last_seen REAL,
    version TEXT NOT NULL DEFAULT 'unknown',
    interval_seconds REAL NOT NULL,
    stopped_at REAL,
    checked_at REAL,
    PRIMARY KEY (component, device, bot)
)
"""
_COLUMNS = (
    "component, device, bot, pid, started_at, last_seen, version, "
    "interval_seconds, stopped_at, checked_at"
)


class HealthError(ValueError):
    """A heartbeat value outside the allowlist, or an unreadable health file."""


def _stderr(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def health_path(db: Path) -> Path:
    """orch.db -> orch.health.db, in the same directory."""
    db = Path(db)
    stem = db.name[:-3] if db.name.endswith(".db") else db.name
    return db.with_name(f"{stem}.health.db")


def factory_version(root: Path = ROOT) -> str:
    """The checkout's commit as 12 hex digits, read from .git without running git."""
    try:
        git = root / ".git"
        if git.is_file():
            text = git.read_text(encoding="utf-8").strip()
            if not text.startswith("gitdir:"):
                return "unknown"
            git = (root / text.split(":", 1)[1].strip()).resolve()
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        sha = head
        if head.startswith("ref: "):
            ref = head[5:].strip()
            common = git
            if (git / "commondir").is_file():
                common = (git / (git / "commondir").read_text(encoding="utf-8").strip()).resolve()
            sha = ""
            for base in (git, common):
                if (base / ref).is_file():
                    sha = (base / ref).read_text(encoding="utf-8").strip()
                    break
            else:
                packed = common / "packed-refs"
                if packed.is_file():
                    for line in packed.read_text(encoding="utf-8").splitlines():
                        parts = line.split()
                        if len(parts) == 2 and parts[1] == ref:
                            sha = parts[0]
                            break
        if _SHA.fullmatch(sha):
            return sha[:12]
    except (OSError, UnicodeDecodeError):
        pass
    return "unknown"


# --- writing ---------------------------------------------------------------------------


def _check(component: str, device: str, bot: str, pid: int | None, version: str,
           interval: float) -> None:
    if component not in COMPONENTS:
        raise HealthError(f"unknown component {component!r}")
    if not isinstance(device, str) or not TOKEN.fullmatch(device):
        raise HealthError("device must be a plain name")
    if bot != "" and bot not in OWNING_BOTS:
        raise HealthError("bot must be claude-bot, codex-bot or cursor-bot")
    if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int) or pid < 1):
        raise HealthError("pid must be a positive integer")
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise HealthError("version must be a short git SHA or unknown")
    if (
        isinstance(interval, bool)
        or not isinstance(interval, (int, float))
        or not MIN_INTERVAL <= interval <= MAX_INTERVAL
    ):
        raise HealthError(f"interval must be {MIN_INTERVAL:g} to {MAX_INTERVAL:g} seconds")


def _create(path: Path, like: Path) -> None:
    """Create the health file with orch.db's permission bits and group."""
    mode, gid = 0o600, None
    try:
        info = like.stat()
        mode, gid = stat.S_IMODE(info.st_mode) & 0o666, info.st_gid
    except OSError:
        pass
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    except FileExistsError:
        return
    try:
        os.fchmod(fd, mode)
        if gid is not None:
            try:
                os.fchown(fd, -1, gid)
            except OSError:
                pass
    finally:
        os.close(fd)


class HealthStore:
    """Writes heartbeat rows to the health file beside `db`."""

    def __init__(self, db: Path, *, clock: Callable[[], float] = time.time):
        self.db = Path(db)
        self.path = health_path(self.db)
        self.clock = clock

    def _connect(self) -> sqlite3.Connection:
        if not self.path.exists():
            _create(self.path, self.db)
        connection = sqlite3.connect(self.path, timeout=BUSY_SECONDS)
        connection.execute(_SCHEMA)
        return connection

    def _write(self, sql: str, params: tuple) -> int:
        connection = self._connect()
        try:
            with connection:
                return connection.execute(sql, params).rowcount
        finally:
            connection.close()

    def beat(
        self,
        component: str,
        device: str,
        *,
        bot: str = "",
        pid: int | None = None,
        started_at: float | None = None,
        version: str = "unknown",
        interval: float = BEAT_SECONDS,
    ) -> None:
        _check(component, device, bot, pid, version, interval)
        now = float(self.clock())
        self._write(
            f"INSERT INTO {TABLE} ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?) "
            "ON CONFLICT (component, device, bot) DO UPDATE SET pid = excluded.pid, "
            "started_at = excluded.started_at, last_seen = excluded.last_seen, "
            "version = excluded.version, interval_seconds = excluded.interval_seconds, "
            "stopped_at = NULL, checked_at = excluded.checked_at",
            (component, device, bot, pid, _real(started_at, now), now, version,
             float(interval), now),
        )

    def checked(
        self,
        component: str,
        device: str,
        *,
        bot: str = "",
        pid: int | None = None,
        started_at: float | None = None,
        version: str = "unknown",
        interval: float = BEAT_SECONDS,
    ) -> None:
        """A failed check (an unreachable device): recorded, never seen."""
        _check(component, device, bot, pid, version, interval)
        now = float(self.clock())
        self._write(
            f"INSERT INTO {TABLE} ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, NULL, ?) "
            "ON CONFLICT (component, device, bot) DO UPDATE SET pid = excluded.pid, "
            "started_at = excluded.started_at, version = excluded.version, "
            "interval_seconds = excluded.interval_seconds, stopped_at = NULL, "
            "checked_at = excluded.checked_at",
            (component, device, bot, pid, _real(started_at, now), version,
             float(interval), now),
        )

    def stop(self, component: str, device: str, *, bot: str = "", pid: int | None = None) -> None:
        """Mark this process's row stopped (a clean exit)."""
        self._write(
            f"UPDATE {TABLE} SET stopped_at = ? WHERE component = ? AND device = ? "
            "AND bot = ? AND pid IS ?",
            (float(self.clock()), component, device, bot, pid),
        )

    def delete(self, component: str, device: str, *, bot: str = "", pid: int | None = None) -> None:
        """Remove this process's row (a plain `orch wait` that claimed and exited)."""
        self._write(
            f"DELETE FROM {TABLE} WHERE component = ? AND device = ? AND bot = ? AND pid IS ?",
            (component, device, bot, pid),
        )

    def forget(self, component: str, *, device: str | None = None, bot: str | None = None) -> int:
        """Remove a retired component's rows (`orch health forget`)."""
        if component not in COMPONENTS:
            raise HealthError(f"unknown component {component!r}")
        sql = f"DELETE FROM {TABLE} WHERE component = ?"
        params: list = [component]
        if device is not None:
            sql += " AND device = ?"
            params.append(device)
        if bot is not None:
            sql += " AND bot = ?"
            params.append(bot)
        return self._write(sql, tuple(params))

    def keep_only(self, component: str, devices: Collection[str]) -> None:
        """Remove `component` rows for devices no longer listed (retired probes)."""
        connection = self._connect()
        try:
            with connection:
                for (device,) in connection.execute(
                    f"SELECT device FROM {TABLE} WHERE component = ?", (component,)
                ).fetchall():
                    if device not in devices:
                        connection.execute(
                            f"DELETE FROM {TABLE} WHERE component = ? AND device = ?",
                            (component, device),
                        )
        finally:
            connection.close()


def _real(value: float | None, default: float) -> float:
    return float(default if value is None else value)


# Signals a heartbeat thread never takes, so the kernel delivers them to the
# main thread: `orch wait` blocks SIGINT and SIGTERM there while it claims a
# job (lib/wait.py), and a signal taken by an unblocked heartbeat thread
# would still run its handler in the main thread mid-claim.
BLOCKED_SIGNALS = frozenset({signal.SIGINT, signal.SIGTERM, signal.SIGHUP})


class _Loop:
    """A daemon thread that calls `step` now and then every `interval` seconds."""

    def __init__(self, name: str, interval: float, step: Callable[[], None], *,
                 block_signals: bool = True):
        self._interval = interval
        self._step = step
        self._block = block_signals
        self._halt = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._halt.set()
        if self._thread.is_alive():
            self._thread.join(timeout=BUSY_SECONDS + 1)

    def _run(self) -> None:
        if self._block:
            signal.pthread_sigmask(signal.SIG_BLOCK, BLOCKED_SIGNALS)
        while not self._halt.is_set():
            self._step()
            self._halt.wait(self._interval)


class Beater:
    """Heartbeats for one process: one row per device, every `interval`.

    Use as a context manager: the thread starts on entry; on exit it stops
    and the rows are marked stopped (`on_exit="stop"`) or removed
    (`"delete"`). A failed write logs one line naming the exception class,
    once until a write succeeds again, and never raises.
    """

    def __init__(
        self,
        db: Path,
        component: str,
        *,
        devices: Sequence[str],
        bot: str = "",
        interval: float = BEAT_SECONDS,
        on_exit: str = "stop",
        store: HealthStore | None = None,
        pid: int | None = None,
        version: str | None = None,
        log: Log = _stderr,
    ):
        if on_exit not in ("stop", "delete"):
            raise ValueError("on_exit must be stop or delete")
        self.store = store if store is not None else HealthStore(db)
        self.component = component
        self.devices = tuple(dict.fromkeys(devices)) or (DEFAULT_DEVICE,)
        self.bot = bot
        self.interval = interval
        self.on_exit = on_exit
        self.pid = pid if pid is not None else os.getpid()
        self.version = version if version is not None else factory_version()
        self.log = log
        self.started_at = float(self.store.clock())
        self._lock = threading.Lock()
        self._closed = False
        self._failing = False
        self._loop = _Loop(f"health-{component}", interval, self.beat_now)

    def beat_now(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                for device in self.devices:
                    self.store.beat(
                        self.component, device, bot=self.bot, pid=self.pid,
                        started_at=self.started_at, version=self.version,
                        interval=self.interval,
                    )
                self._failing = False
            except Exception as exc:  # a heartbeat never stops its component
                if not self._failing:
                    self.log(f"health: {self.component} heartbeat failed: {type(exc).__name__}")
                self._failing = True

    def start(self) -> Beater:
        self._loop.start()
        return self

    def close(self) -> None:
        self._loop.stop()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            end = self.store.stop if self.on_exit == "stop" else self.store.delete
            for device in self.devices:
                try:
                    end(self.component, device, bot=self.bot, pid=self.pid)
                except Exception:  # the row ages out to down instead
                    pass

    def __enter__(self) -> Beater:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()


def beat_once(
    db: Path,
    component: str,
    *,
    interval: float = BEAT_SECONDS,
    device: str | None = None,
    machines: Path | None = None,
) -> bool:
    """One heartbeat from a short-lived writer (the dashboard push). Never raises."""
    try:
        HealthStore(db).beat(
            component,
            device or local_device(load_machines(machines)),
            pid=os.getpid(),
            version=factory_version(),
            interval=min(max(float(interval), MIN_INTERVAL), MAX_INTERVAL),
        )
        return True
    except Exception:
        return False


# --- the device probe -------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeTarget:
    device: str
    target: RemoteTarget


def probe_targets(machines: MachinesConfig | None) -> list[ProbeTarget]:
    """Each enabled, non-placeholder remote device, over its first bot's alias."""
    targets: list[ProbeTarget] = []
    for device in machines.devices if machines is not None else ():
        if device.local or not device.schedulable or not TOKEN.fullmatch(device.name):
            continue
        harness = next(iter(device.harnesses), None)
        if harness is None:
            continue
        target = remote_target(device, harness, PROBE_JOB_ID)
        if target is not None:
            targets.append(ProbeTarget(device.name, target))
    return targets


class DeviceProber:
    """The poller's per-device SSH reachability probe, every PROBE_SECONDS."""

    def __init__(
        self,
        store: HealthStore,
        targets: Sequence[ProbeTarget],
        transport: Transport,
        *,
        interval: float = PROBE_SECONDS,
        pid: int | None = None,
        version: str | None = None,
        log: Log = _stderr,
    ):
        self.store = store
        self.targets = tuple(targets)
        self.transport = transport
        self.interval = interval
        self.pid = pid if pid is not None else os.getpid()
        self.version = version if version is not None else factory_version()
        self.log = log
        self.started_at = float(store.clock())
        self._failing = False
        # Not blocked: its ssh children would inherit the mask. It runs only
        # in `orch poll`, which has no claim guard.
        self._loop = _Loop("health-device-probe", interval, self.probe_once,
                           block_signals=False)

    def forget_others(self) -> None:
        try:
            self.store.keep_only(DEVICE_PROBE, {t.device for t in self.targets})
        except Exception as exc:
            self.log(f"health: device-probe cleanup failed: {type(exc).__name__}")

    def probe_once(self) -> None:
        for item in self.targets:
            try:
                reached = probe_device(self.transport, item.target)
                record = self.store.beat if reached else self.store.checked
                record(DEVICE_PROBE, item.device, pid=self.pid, started_at=self.started_at,
                       version=self.version, interval=self.interval)
                self._failing = False
            except Exception as exc:  # a probe never stops the poller
                if not self._failing:
                    self.log(f"health: device-probe failed: {type(exc).__name__}")
                self._failing = True

    def __enter__(self) -> DeviceProber:
        self.forget_others()
        if self.targets:
            self._loop.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._loop.stop()


# --- what each component beats as ----------------------------------------------------------


def load_machines(path: Path | None) -> MachinesConfig | None:
    """machines.yml (default: the checkout's) or None when absent or invalid."""
    machines_path = path if path is not None else ROOT / "machines.yml"
    try:
        config, findings = load_machines_yml(machines_path, allow_placeholders=True)
    except (OSError, ValueError):
        return None
    if config is None or any(item.severity == "error" for item in findings):
        return None
    return config


def local_device(machines: MachinesConfig | None) -> str:
    for device in machines.devices if machines is not None else ():
        if device.local and TOKEN.fullmatch(device.name):
            return device.name
    return DEFAULT_DEVICE


def eligible_devices(machines: MachinesConfig | None) -> dict[str, frozenset[str]] | None:
    """harness -> the devices that may run it, or None when machines.yml is unknown."""
    if machines is None:
        return None
    return {
        harness: frozenset(d.name for d in machines.devices if d.eligible(harness))
        for harness in _HARNESS_BOT
    }


def waiter_devices(machines: MachinesConfig | None, bot: str) -> list[str]:
    """The devices a waiter for `bot` claims on, in machines.yml order."""
    harness = _BOT_HARNESS.get(bot)
    names = [
        d.name
        for d in (machines.devices if machines is not None else ())
        if harness is not None and d.eligible(harness) and TOKEN.fullmatch(d.name)
    ]
    return names or [local_device(machines)]


class PollerBeats:
    """`orch poll`'s heartbeat plus the per-device SSH probe: start(), stop()."""

    def __init__(self, beater: Beater, prober: DeviceProber):
        self.beater = beater
        self.prober = prober

    def start(self) -> PollerBeats:
        self.beater.start()
        self.prober.__enter__()
        return self

    def stop(self) -> None:
        self.prober.__exit__(None, None, None)
        self.beater.close()

    def __enter__(self) -> PollerBeats:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def poller_beats(
    db: Path,
    machines_path: Path | None,
    *,
    transport: Transport | None = None,
    interval: float = BEAT_SECONDS,
    log: Log = _stderr,
) -> PollerBeats:
    """The poller's beats for `db`, probing machines.yml's remote devices."""
    machines = load_machines(machines_path)
    store = HealthStore(db)
    if transport is None:
        from lib.remote import SshTransport

        transport = SshTransport()
    beater = Beater(db, POLLER, devices=[local_device(machines)], store=store,
                    interval=interval, log=log)
    return PollerBeats(beater, DeviceProber(store, probe_targets(machines), transport, log=log))


def beating(db: Path, component: str, *, log: Log = _stderr) -> Beater:
    """A box component's heartbeat (the Linear listener)."""
    return Beater(db, component, devices=[local_device(load_machines(None))], log=log)


# --- reading -------------------------------------------------------------------------------


class Heartbeat(NamedTuple):
    component: str
    device: str
    bot: str
    pid: int | None
    started_at: float | None
    last_seen: float | None
    version: str
    interval_seconds: float
    stopped_at: float | None
    checked_at: float | None


def read_heartbeats(db: Path) -> tuple[Heartbeat, ...] | None:
    """Every row, read-only; None when no component has ever written."""
    path = health_path(db)
    if not path.is_file():
        return None
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True,
                                 timeout=BUSY_SECONDS)
    try:
        connection.execute("PRAGMA query_only = ON")
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)
        ).fetchone()
        if not exists:
            return ()
        rows = connection.execute(f"SELECT {_COLUMNS} FROM {TABLE}").fetchall()
    finally:
        connection.close()
    result = []
    for values in rows:
        record = Heartbeat(*values)
        if record.component in COMPONENTS and isinstance(record.interval_seconds, (int, float)):
            result.append(record._replace(interval_seconds=float(record.interval_seconds)))
    return tuple(result)


def classify(row: Heartbeat, now: float) -> tuple[str, str]:
    """(state, reason): up, stale or down by the age of the last beat."""
    if row.last_seen is None:
        return DOWN, "never reached" if row.component == DEVICE_PROBE else "never seen"
    if row.stopped_at is not None and row.stopped_at >= row.last_seen:
        return DOWN, "stopped"
    interval = max(float(row.interval_seconds), 1.0)
    age = max(0.0, now - row.last_seen)
    failed = row.checked_at is not None and row.checked_at > row.last_seen
    if age > DOWN_BEATS * interval:
        state = DOWN
    elif age > STALE_BEATS * interval or failed:
        state = STALE
    else:
        return UP, ""
    if failed:
        return state, f"unreachable, last reached {age_text(age)} ago"
    return state, f"no heartbeat for {age_text(age)}"


@dataclass(frozen=True)
class Component:
    component: str
    device: str
    bot: str
    pid: int | None
    state: str
    reason: str
    last_seen: float | None
    started_at: float | None
    version: str

    @property
    def label(self) -> str:
        who = f"{self.component} {self.bot}" if self.bot else self.component
        return f"{who} @ {self.device}"

    @property
    def anchor(self) -> str:
        parts = [self.component, self.bot, self.device]
        return "health-" + "-".join(_slug(part) for part in parts if part)


@dataclass(frozen=True)
class NoWaiter:
    harness: str
    bot: str
    jobs: tuple[str, ...]

    @property
    def anchor(self) -> str:
        return f"health-waiter-{self.bot}"


@dataclass(frozen=True)
class HealthReport:
    known: bool
    components: tuple[Component, ...] = ()
    no_waiter: tuple[NoWaiter, ...] = ()
    error: str | None = None

    @property
    def down(self) -> tuple[Component, ...]:
        return tuple(c for c in self.components if c.state == DOWN)

    def no_waiter_for(self, harness: str) -> NoWaiter | None:
        return next((n for n in self.no_waiter if n.harness == harness), None)


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", text)


def build_report(
    rows: Sequence[Heartbeat] | None,
    now: float,
    *,
    queued: Iterable[tuple[str, str]] = (),
    eligible: Mapping[str, Collection[str]] | None = None,
    local: str = DEFAULT_DEVICE,
) -> HealthReport:
    """Classify every row and flag queued jobs no live waiter can take.

    `queued` is (job id, recipient) per queued job; `eligible` maps a harness
    to the devices that may run it (None: any device counts)."""
    if rows is None:
        return HealthReport(known=False)
    components = [
        Component(
            component=row.component,
            device=row.device,
            bot=row.bot,
            pid=row.pid,
            state=state,
            reason=reason,
            last_seen=row.last_seen,
            started_at=row.started_at,
            version=row.version,
        )
        for row in rows
        for state, reason in (classify(row, now),)
    ]
    if not any(c.component == POLLER for c in components):
        components.append(Component(POLLER, local, "", None, DOWN, "never seen", None, None, "unknown"))
    components.sort(key=lambda c: (COMPONENTS.index(c.component), c.bot, c.device))
    jobs: dict[str, list[str]] = {}
    for job_id, recipient in queued:
        if recipient in _HARNESS_BOT:
            jobs.setdefault(recipient, []).append(job_id)
    flags = []
    for harness in sorted(jobs):
        bot = _HARNESS_BOT[harness]
        devices = None if eligible is None else eligible.get(harness, frozenset())
        live = any(
            c.component == WAITER and c.bot == bot and c.state != DOWN
            and (devices is None or c.device in devices)
            for c in components
        )
        if not live:
            flags.append(NoWaiter(harness, bot, tuple(jobs[harness])))
    return HealthReport(known=True, components=tuple(components), no_waiter=tuple(flags))


def load_report(
    db: Path,
    now: float,
    *,
    queued: Iterable[tuple[str, str]] = (),
    machines: MachinesConfig | None = None,
) -> HealthReport:
    """`build_report` from the health file; an unreadable file is reported, not raised."""
    try:
        rows = read_heartbeats(db)
    except sqlite3.Error as exc:
        return HealthReport(known=False, error=f"cannot read {health_path(db).name}: {type(exc).__name__}")
    return build_report(rows, now, queued=queued, eligible=eligible_devices(machines),
                        local=local_device(machines))


# --- text ------------------------------------------------------------------------------------


def et(when: float) -> str:
    """A Unix time in US Eastern time, e.g. "Nov 12 9:19:05 PM ET"."""
    utc = datetime.fromtimestamp(when, tz=timezone.utc)
    local = utc + timedelta(hours=-4 if _eastern_dst(utc) else -5)
    hour = local.hour % 12 or 12
    half = "AM" if local.hour < 12 else "PM"
    return f"{local:%b} {local.day} {hour}:{local:%M:%S} {half} ET"


def _eastern_dst(utc: datetime) -> bool:
    """US daylight time: 2 AM on the second Sunday of March to 2 AM on the first Sunday of November."""
    start = _sunday(utc.year, 3, 2) + timedelta(hours=7)  # 2 AM EST
    end = _sunday(utc.year, 11, 1) + timedelta(hours=6)  # 2 AM EDT
    return start <= utc < end


def _sunday(year: int, month: int, nth: int) -> datetime:
    first = datetime(year, month, 1, tzinfo=timezone.utc)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (nth - 1))


def age_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def render_lines(report: HealthReport, now: float) -> list[str]:
    """The Health section of `orch status` and `orch health`."""
    if report.error is not None:
        return ["Health", f"  unknown: {report.error}"]
    if not report.known:
        return ["Health", "  no heartbeats recorded yet"]
    lines = [f"Health ({len(report.components)} components, {len(report.down)} down)"]
    for c in report.components:
        reason = f" ({c.reason})" if c.reason else ""
        if c.last_seen is None:
            seen = "never seen"
        else:
            seen = f"last seen {et(c.last_seen)} ({age_text(now - c.last_seen)} ago)"
        pid = f", pid {c.pid}" if c.pid is not None else ""
        lines.append(f"  {c.label} {c.state}{reason}, {seen}{pid}")
    for flag in report.no_waiter:
        count = len(flag.jobs)
        lines.append(f"  no waiter for {flag.harness}: {count} queued job{'' if count == 1 else 's'}")
    return lines


def report_json(report: HealthReport, now: float) -> dict:
    return {
        "known": report.known,
        "error": report.error,
        "components": [
            {
                "component": c.component,
                "device": c.device,
                "bot": c.bot,
                "pid": c.pid,
                "state": c.state,
                "reason": c.reason,
                "last_seen_et": None if c.last_seen is None else et(c.last_seen),
                "last_seen_age_seconds": None if c.last_seen is None else int(max(0.0, now - c.last_seen)),
                "started_at_et": None if c.started_at is None else et(c.started_at),
                "version": c.version,
                "label": c.label,
                "anchor": c.anchor,
            }
            for c in report.components
        ],
        "no_waiter": [
            {"harness": n.harness, "bot": n.bot, "jobs": list(n.jobs), "count": len(n.jobs),
             "anchor": n.anchor}
            for n in report.no_waiter
        ],
        "down": len(report.down),
        "stale": sum(1 for c in report.components if c.state == STALE),
        "thresholds": {
            "beat_seconds": BEAT_SECONDS,
            "probe_seconds": PROBE_SECONDS,
            "stale_beats": STALE_BEATS,
            "down_beats": DOWN_BEATS,
        },
    }


# --- orch health ---------------------------------------------------------------------------

USAGE = """\
orch health [--db PATH] [--machines PATH] [--json]
orch health beat <component> [--db PATH] [--device NAME] [--bot BOT] [--pid PID]
                             [--interval SECONDS]
orch health forget <component> [--db PATH] [--device NAME] [--bot BOT]
"""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        print(f"{self.prog}: {message}", file=sys.stderr)
        raise SystemExit(2)


def _db(value: Path | None) -> Path:
    if value is not None:
        return value
    env = os.environ.get("ORCH_DB")
    if not env:
        print("orch health: pass --db or set ORCH_DB to the dispatcher database", file=sys.stderr)
        raise SystemExit(2)
    return Path(env)


def _queued(db: Path) -> list[tuple[str, str]]:
    if not db.is_file():
        raise HealthError(f"database does not exist: {db}")
    connection = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=BUSY_SECONDS)
    try:
        connection.execute("PRAGMA query_only = ON")
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
        ).fetchone():
            return []
        return [
            (str(job_id), str(recipient))
            for job_id, recipient in connection.execute(
                "SELECT id, recipient FROM jobs WHERE state = 'queued' ORDER BY created_at, id"
            )
        ]
    finally:
        connection.close()


def main(argv: list[str], *, stdout: TextIO | None = None,
         clock: Callable[[], float] = time.time) -> int:
    out = stdout if stdout is not None else sys.stdout
    try:
        if argv[:1] == ["beat"]:
            return _beat(argv[1:])
        if argv[:1] == ["forget"]:
            return _forget(argv[1:])
        return _report(argv, out, clock)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2


def _report(argv: list[str], out: TextIO, clock: Callable[[], float]) -> int:
    parser = _Parser(prog="orch health", usage=USAGE)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--machines", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    db = _db(args.db)
    machines = load_machines(args.machines)
    now = clock()
    try:
        report = load_report(db, now, queued=_queued(db), machines=machines)
    except (HealthError, sqlite3.Error) as exc:
        print(f"orch health: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report_json(report, now), sort_keys=True), file=out)
    else:
        print("\n".join(render_lines(report, now)), file=out)
    return 3 if report.down or report.no_waiter else 0


def _identity(parser: _Parser) -> None:
    parser.add_argument("component", choices=COMPONENTS)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--device")
    parser.add_argument("--bot", choices=tuple(OWNING_BOTS))


def _beat(argv: list[str]) -> int:
    parser = _Parser(prog="orch health beat", usage=USAGE)
    _identity(parser)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--interval", type=float, default=BEAT_SECONDS)
    args = parser.parse_args(argv)
    db = _db(args.db)
    pid = args.pid if args.pid is not None else os.getppid()
    if args.pid is not None and not _pid_running(pid):
        print(f"orch health beat: pid {pid} is not running; nothing recorded", file=sys.stderr)
        return 1
    try:
        HealthStore(db).beat(
            args.component,
            args.device or local_device(load_machines(None)),
            bot=args.bot or "",
            pid=pid,
            version=factory_version(),
            interval=args.interval,
        )
    except HealthError as exc:
        print(f"orch health beat: {exc}", file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error) as exc:
        print(f"orch health beat: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


def _forget(argv: list[str]) -> int:
    parser = _Parser(prog="orch health forget", usage=USAGE)
    _identity(parser)
    args = parser.parse_args(argv)
    db = _db(args.db)
    if not health_path(db).is_file():
        return 0
    try:
        removed = HealthStore(db).forget(args.component, device=args.device, bot=args.bot)
    except (OSError, sqlite3.Error) as exc:
        print(f"orch health forget: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"removed {removed} row{'' if removed == 1 else 's'}", file=sys.stdout)
    return 0


def _pid_running(pid: int) -> bool:
    if pid < 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OverflowError:
        return False
    return True
