"""Component heartbeats: which parts of the factory are running (issue #92).

Every long-running part writes one heartbeat row every BEAT_SECONDS (15):

* `poller`: `orch poll` (not `--once`);
* `waiter`: each bot's `orch wait`, one row per device it can claim on
  (`--autostart` waiters: box devices only); a plain `orch wait` removes
  its rows when it exits, an autostart waiter marks them stopped;
* `linear-listener`: `orch linear-webhook serve`;
* `dashboard`: the dashboard writer, once per `orch dashboard push`
  snapshot (its interval is the push interval);
* `dashboard-push` (#106): whether the poller's dashboard pushes reach the
  dashboard host, written by `orch poll` from each push child's exit
  status (`push_result`). A failed push leaves it stale ("push failing"),
  and failures for DOWN_BEATS push intervals make it down. It is listed
  once a push has finished, and is not in the expected registry: with no
  push at all, `dashboard` is already down;
* `device-probe`: one row per enabled remote device in machines.yml, from
  an SSH round trip the poller makes every PROBE_SECONDS (60); only a
  device that answered counts as seen;
* `tunnel`: the Linear listener's Funnel, from a GET the listener sends to
  `linear_webhook.public_url` every BEAT_SECONDS; only the listener's own
  405 for that path counts as seen.

A row is (component, device, bot, pid, started_at, last_seen, version,
interval_seconds, stopped_at, checked_at), one per process: the key holds
the pid, so one waiter exiting never removes another's row. Every value is
an allowlisted name, a device listed in machines.yml, a number or a
12-digit git SHA (`HealthStore.beat` refuses anything else), so no argv,
environment value, path, URL or token can reach a row. A beat also removes
rows other processes left for the same component and bot once they are
down.

The rows live in their own SQLite file beside orch.db (`health_path`,
orch.health.db), not in orch.db: a heartbeat every 15 seconds from each
component would otherwise wake the dashboard's live push (which watches
orch.db for changes) and add writers to the dispatcher's database. The file
is created with orch.db's permission bits and group, so a waiter running as
another bot user can write it whenever it can write orch.db. Every open
waits at most BUSY_SECONDS for a lock: a beat that cannot be written is
logged and dropped, and a report that cannot be read says so (`error`) and
needs attention.

State (`classify`): up while the last beat is at most STALE_BEATS (3)
intervals old, stale up to DOWN_BEATS (8) intervals, then down; a clean
stop is down at once, and so is a component with no beat at all. A device
whose latest probe failed is at least stale. With the default 15 seconds,
stale after 45 seconds and down after 2 minutes; a probe (60 seconds) is
stale after 3 minutes and down after 8.

`build_report` shows one entry per (component, device, bot), the best of
its processes' rows (for a device probe or tunnel, its newest check), and
adds every expected component (`expected`: the poller; each bot's waiter on
some eligible device and each probed device when machines.yml is known;
the Linear listener and its tunnel when orch.yml has `linear_webhook`; the
dashboard writer when it pushes) that has no row as down. A row that the
registry does not expect (a retired device or bot) is listed but is not
down for attention. It flags each queued harness job that has no live (up
or stale) waiter for its bot on any device eligible for its harness as "no
waiter for <harness>". The dashboard's Health tab, its needs-attention
line, `orch status` and `orch health` all read the same report.

Devices (#297): the report also lists each factory checkout's sync state,
the box's own (HEAD against origin/main, `box_sync`) and every remote
target's from orch.db's `device_sync` table (lib/device_sync.py). A remote
target needs attention when missing or refused at once, or after
SYNC_GRACE_SECONDS lagging or deploying; the box needs attention whenever its
HEAD is not origin/main, since it has no `since` to wait on. Devices out of
sync never change `orch health`'s exit status.
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
import urllib.error
import urllib.request
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, TextIO

from lib import device_sync
from lib.dashboard_config import EVERY_MINUTES_MAX
from lib.device_monitor import remote_target
from lib.machines_yml import MachinesConfig, load_machines_yml
from lib.remote import RemoteTarget, Transport, probe_device
from lib.wait import OWNING_BOTS

if TYPE_CHECKING:
    from lib.orch_yml import OrchConfig

ROOT = Path(__file__).resolve().parent.parent

TABLE = "component_heartbeats"
BEAT_SECONDS = 15.0
PROBE_SECONDS = 60.0
STALE_BEATS = 3
DOWN_BEATS = 8
BUSY_SECONDS = 0.5
DB_BUSY_SECONDS = 5.0
TUNNEL_TIMEOUT = 10.0
TUNNEL_SEEN = 405
MIN_INTERVAL = 5.0
MAX_INTERVAL = EVERY_MINUTES_MAX * 60.0
DEFAULT_DEVICE = "box"
PROBE_JOB_ID = "health-probe"

POLLER = "poller"
WAITER = "waiter"
LISTENER = "linear-listener"
TUNNEL = "tunnel"
DASHBOARD = "dashboard"
DASHBOARD_PUSH = "dashboard-push"
DEVICE_PROBE = "device-probe"
COMPONENTS = (POLLER, WAITER, LISTENER, TUNNEL, DASHBOARD, DASHBOARD_PUSH, DEVICE_PROBE)
# Checks of a target, not liveness of a process: the newest check decides.
CHECKS = (DEVICE_PROBE, TUNNEL, DASHBOARD_PUSH)

UP = "up"
STALE = "stale"
DOWN = "down"

TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
VERSION = re.compile(r"[0-9a-f]{12}|unknown")
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
    PRIMARY KEY (component, device, bot, pid)
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


def checkout_sha(root: Path = ROOT, ref: str = "HEAD") -> str | None:
    """The full sha a ref of the checkout names, read from .git without running git."""
    try:
        git = root / ".git"
        if git.is_file():
            text = git.read_text(encoding="utf-8").strip()
            if not text.startswith("gitdir:"):
                return None
            git = (root / text.split(":", 1)[1].strip()).resolve()
        common = git
        if (git / "commondir").is_file():
            common = (git / (git / "commondir").read_text(encoding="utf-8").strip()).resolve()
        sha = ref
        if ref == "HEAD":
            sha = (git / "HEAD").read_text(encoding="utf-8").strip()
            if sha.startswith("ref: "):
                ref = sha[5:].strip()
        if not _SHA.fullmatch(sha):
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
            return sha
    except (OSError, UnicodeDecodeError):
        pass
    return None


def factory_version(root: Path = ROOT) -> str:
    """The checkout's commit as 12 hex digits, or "unknown"."""
    sha = checkout_sha(root)
    return sha[:12] if sha is not None else "unknown"


LOADED_VERSION = factory_version()
CODE_COMPONENTS = frozenset({"poller", "waiter", "linear-listener"})


# --- writing ---------------------------------------------------------------------------


def _check(component: str, device: str, bot: str, pid: int, version: str,
           interval: float, devices: Collection[str]) -> None:
    if component not in COMPONENTS:
        raise HealthError(f"unknown component {component!r}")
    if not isinstance(device, str) or not TOKEN.fullmatch(device) or device not in devices:
        raise HealthError("device must be a device machines.yml lists")
    if bot != "" and bot not in OWNING_BOTS:
        raise HealthError("bot must be claude-bot, codex-bot or cursor-bot")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
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
    """Writes heartbeat rows to the health file beside `db`, for `devices` only."""

    def __init__(self, db: Path, *, devices: Collection[str],
                 clock: Callable[[], float] = time.time):
        self.db = Path(db)
        self.path = health_path(self.db)
        self.devices = frozenset(devices)
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
        pid: int,
        started_at: float | None = None,
        version: str = "unknown",
        interval: float = BEAT_SECONDS,
    ) -> None:
        _check(component, device, bot, pid, version, interval, self.devices)
        now = float(self.clock())
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    f"INSERT INTO {TABLE} ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?) "
                    "ON CONFLICT (component, device, bot, pid) DO UPDATE SET "
                    "started_at = excluded.started_at, last_seen = excluded.last_seen, "
                    "version = excluded.version, interval_seconds = excluded.interval_seconds, "
                    "stopped_at = NULL, checked_at = excluded.checked_at",
                    (component, device, bot, pid, _real(started_at, now), now, version,
                     float(interval), now),
                )
                connection.execute(
                    f"DELETE FROM {TABLE} WHERE component = ? AND bot = ? AND pid IS NOT ? "
                    "AND (stopped_at IS NOT NULL "
                    "OR COALESCE(last_seen, checked_at, 0) < ? - ? * interval_seconds)",
                    (component, bot, pid, now, DOWN_BEATS),
                )
        finally:
            connection.close()

    def checked(
        self,
        component: str,
        device: str,
        *,
        bot: str = "",
        pid: int,
        started_at: float | None = None,
        version: str = "unknown",
        interval: float = BEAT_SECONDS,
    ) -> None:
        """A failed check (an unreachable device or tunnel): recorded, never seen."""
        _check(component, device, bot, pid, version, interval, self.devices)
        now = float(self.clock())
        self._write(
            f"INSERT INTO {TABLE} ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, NULL, ?) "
            "ON CONFLICT (component, device, bot, pid) DO UPDATE SET "
            "started_at = excluded.started_at, version = excluded.version, "
            "interval_seconds = excluded.interval_seconds, stopped_at = NULL, "
            "checked_at = excluded.checked_at",
            (component, device, bot, pid, _real(started_at, now), version,
             float(interval), now),
        )

    def stop(self, component: str, device: str, *, bot: str = "", pid: int) -> None:
        """Mark this process's row stopped (a clean exit)."""
        self._write(
            f"UPDATE {TABLE} SET stopped_at = ? WHERE component = ? AND device = ? "
            "AND bot = ? AND pid = ?",
            (float(self.clock()), component, device, bot, pid),
        )

    def delete(self, component: str, device: str, *, bot: str = "", pid: int) -> None:
        """Remove this process's row (a plain `orch wait` that claimed and exited)."""
        self._write(
            f"DELETE FROM {TABLE} WHERE component = ? AND device = ? AND bot = ? AND pid = ?",
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
    """A daemon thread that calls `step` now and then every `interval` seconds,
    counted from each step's start, so a slow step does not stretch the cadence."""

    def __init__(self, name: str, interval: float, step: Callable[[], None], *,
                 block_signals: bool = True):
        self._interval = interval
        self._step = step
        self._block = block_signals
        self._halt = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, *, wait: bool = True) -> None:
        self._halt.set()
        if wait and self._thread.is_alive():
            self._thread.join(timeout=BUSY_SECONDS + 1)

    def _run(self) -> None:
        if self._block:
            signal.pthread_sigmask(signal.SIG_BLOCK, BLOCKED_SIGNALS)
        while not self._halt.is_set():
            started = time.monotonic()
            self._step()
            self._halt.wait(max(0.0, started + self._interval - time.monotonic()))


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
        self.devices = tuple(dict.fromkeys(devices)) or (DEFAULT_DEVICE,)
        self.store = store if store is not None else HealthStore(db, devices=self.devices)
        self.component = component
        self.bot = bot
        self.interval = interval
        self.on_exit = on_exit
        self.pid = pid if pid is not None else os.getpid()
        self.version = version if version is not None else LOADED_VERSION
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
    machines: Path | None = None,
    log: Log = _stderr,
    ok: bool = True,
) -> bool:
    """One heartbeat from a short-lived writer (the dashboard push), on the
    local device; with ok=False, a failed check instead. Never raises: a
    failure is logged and returns False."""
    try:
        config = load_machines(machines)
        store = HealthStore(db, devices=known_devices(config))
        record = store.beat if ok else store.checked
        record(
            component,
            local_device(config),
            pid=os.getpid(),
            version=LOADED_VERSION,
            interval=min(max(float(interval), MIN_INTERVAL), MAX_INTERVAL),
        )
        return True
    except Exception as exc:
        log(f"health: {component} heartbeat failed: {type(exc).__name__}")
        return False


def push_result(db: Path, ok: bool, *, interval: float, log: Log = _stderr) -> bool:
    """`orch poll`'s record of one finished dashboard push (#106)."""
    return beat_once(db, DASHBOARD_PUSH, interval=interval, log=log, ok=ok)


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
    """The poller's per-device SSH reachability probe, every PROBE_SECONDS.

    Each device has its own thread, so a device whose SSH hangs until the
    probe timeout never delays another device's probe."""

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
        self.version = version if version is not None else LOADED_VERSION
        self.log = log
        self.started_at = float(store.clock())
        self._failing: set[str] = set()
        # Not blocked: its ssh children would inherit the mask. It runs only
        # in `orch poll`, which has no claim guard.
        self._loops = [
            _Loop(f"health-device-probe-{item.device}", interval,
                  lambda item=item: self.probe(item), block_signals=False)
            for item in self.targets
        ]

    def forget_others(self) -> None:
        try:
            self.store.keep_only(DEVICE_PROBE, {t.device for t in self.targets})
        except Exception as exc:
            self.log(f"health: device-probe cleanup failed: {type(exc).__name__}")

    def probe(self, item: ProbeTarget) -> None:
        try:
            reached = probe_device(self.transport, item.target)
            record = self.store.beat if reached else self.store.checked
            record(DEVICE_PROBE, item.device, pid=self.pid, started_at=self.started_at,
                   version=self.version, interval=self.interval)
            self._failing.discard(item.device)
        except Exception as exc:  # a probe never stops the poller
            if item.device not in self._failing:
                self.log(f"health: device-probe {item.device} failed: {type(exc).__name__}")
            self._failing.add(item.device)

    def probe_once(self) -> None:
        for item in self.targets:
            self.probe(item)

    def __enter__(self) -> DeviceProber:
        self.forget_others()
        for loop in self._loops:
            loop.start()
        return self

    def __exit__(self, *exc: object) -> None:
        for loop in self._loops:
            loop.stop(wait=False)
        for loop in self._loops:
            loop.stop()


# --- the tunnel probe -------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def http_status(url: str, timeout: float) -> int | None:
    """The HTTP status a GET of `url` answers (no proxy, no redirect), or None."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(urllib.request.Request(url, method="GET"), timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (OSError, ValueError):
        return None


class TunnelProber:
    """The Linear listener's tunnel check: a GET of `public_url` every
    BEAT_SECONDS. Only the listener's own 405 for its path counts as seen;
    a gateway error, another status or no answer is a failed check."""

    def __init__(
        self,
        store: HealthStore,
        device: str,
        url: str,
        *,
        fetch: Callable[[str, float], int | None] = http_status,
        interval: float = BEAT_SECONDS,
        pid: int | None = None,
        version: str | None = None,
        log: Log = _stderr,
    ):
        self.store = store
        self.device = device
        self.url = url
        self.fetch = fetch
        self.interval = interval
        self.pid = pid if pid is not None else os.getpid()
        self.version = version if version is not None else LOADED_VERSION
        self.log = log
        self.started_at = float(store.clock())
        self._failing = False
        self._loop = _Loop("health-tunnel", interval, self.probe_once)

    def probe_once(self) -> None:
        try:
            seen = self.fetch(self.url, TUNNEL_TIMEOUT) == TUNNEL_SEEN
            record = self.store.beat if seen else self.store.checked
            record(TUNNEL, self.device, pid=self.pid, started_at=self.started_at,
                   version=self.version, interval=self.interval)
            self._failing = False
        except Exception as exc:  # a probe never stops the listener
            if not self._failing:
                self.log(f"health: tunnel probe failed: {type(exc).__name__}")
            self._failing = True

    def __enter__(self) -> TunnelProber:
        self._loop.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._loop.stop()
        try:
            self.store.stop(TUNNEL, self.device, pid=self.pid)
        except Exception:  # the row ages out to down instead
            pass


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


def known_devices(machines: MachinesConfig | None) -> frozenset[str]:
    """The device names a heartbeat may carry: machines.yml's, plus the local one."""
    names = {d.name for d in (machines.devices if machines is not None else ())
             if TOKEN.fullmatch(d.name)}
    return frozenset(names | {local_device(machines)})


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
    store = HealthStore(db, devices=known_devices(machines))
    if transport is None:
        from lib.remote import SshTransport

        transport = SshTransport()
    beater = Beater(db, POLLER, devices=[local_device(machines)], store=store,
                    interval=interval, log=log)
    return PollerBeats(beater, DeviceProber(store, probe_targets(machines), transport, log=log))


class ListenerBeats:
    """`orch linear-webhook serve`'s heartbeat, plus its tunnel probe when
    `linear_webhook.public_url` is set."""

    def __init__(self, beater: Beater, tunnel: TunnelProber | None):
        self.beater = beater
        self.tunnel = tunnel

    def __enter__(self) -> ListenerBeats:
        self.beater.start()
        if self.tunnel is not None:
            self.tunnel.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        if self.tunnel is not None:
            self.tunnel.__exit__(None, None, None)
        self.beater.close()


def listener_beats(
    db: Path,
    public_url: str | None,
    *,
    machines_path: Path | None = None,
    fetch: Callable[[str, float], int | None] = http_status,
    interval: float = BEAT_SECONDS,
    log: Log = _stderr,
) -> ListenerBeats:
    machines = load_machines(machines_path)
    device = local_device(machines)
    store = HealthStore(db, devices=known_devices(machines))
    beater = Beater(db, LISTENER, devices=[device], store=store, interval=interval, log=log)
    tunnel = (
        TunnelProber(store, device, public_url, fetch=fetch, interval=interval, log=log)
        if public_url
        else None
    )
    return ListenerBeats(beater, tunnel)


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
        return DOWN, _never(row.component)
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
    if failed and row.component == DASHBOARD_PUSH:
        return state, f"push failing, last pushed {age_text(age)} ago"
    if failed:
        return state, f"unreachable, last reached {age_text(age)} ago"
    return state, f"no heartbeat for {age_text(age)}"


def _never(component: str) -> str:
    if component == DASHBOARD_PUSH:
        return "never pushed"
    return "never reached" if component in (DEVICE_PROBE, TUNNEL) else "never seen"


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
    interval: float = BEAT_SECONDS
    configured: bool = True
    stale_code: bool = False

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
class DeviceSync:
    """One factory checkout's sync state: the box's, or a remote bot's."""

    device: str
    bot: str
    checkout: str
    sha: str | None
    target: str | None
    state: str
    detail: str
    since: float | None
    needs_attention: bool

    @classmethod
    def of(cls, row: device_sync.Row, now: float) -> DeviceSync:
        return cls(row.device, row.bot, row.checkout, row.sha, row.target, row.state,
                   row.detail, row.since, row.needs_attention(now))


def box_sync(device: str, root: Path = ROOT) -> DeviceSync | None:
    """The box checkout's HEAD against its origin/main; None when either is unknown."""
    head = checkout_sha(root)
    target = checkout_sha(root, "refs/remotes/origin/main")
    if head is None or target is None:
        return None
    synced = head == target
    return DeviceSync(device, "", str(root), head, target,
                      device_sync.SYNCED if synced else device_sync.LAGGING,
                      "", None, not synced)


@dataclass(frozen=True)
class HealthReport:
    known: bool
    components: tuple[Component, ...] = ()
    no_waiter: tuple[NoWaiter, ...] = ()
    error: str | None = None
    code_version: str = "unknown"
    devices: tuple[DeviceSync, ...] = ()

    @property
    def down(self) -> tuple[Component, ...]:
        """Configured components that are down: what needs attention."""
        return tuple(c for c in self.components if c.state == DOWN and c.configured)

    @property
    def stale_code(self) -> tuple[Component, ...]:
        """Configured components running code older than the checkout."""
        return tuple(c for c in self.components if c.stale_code and c.configured)

    @property
    def devices_out_of_sync(self) -> tuple[DeviceSync, ...]:
        return tuple(d for d in self.devices if d.needs_attention)

    def no_waiter_for(self, harness: str) -> NoWaiter | None:
        return next((n for n in self.no_waiter if n.harness == harness), None)


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", text)


@dataclass(frozen=True)
class Slot:
    """One expected component: a row for `component` and `bot` on any of `devices`."""

    component: str
    devices: tuple[str, ...]
    bot: str = ""


@dataclass(frozen=True)
class Expected:
    """The components this deployment runs. `covers` names the component
    kinds the registry decides; a row of a covered kind that no slot
    expects is shown but is not down for attention."""

    slots: tuple[Slot, ...]
    covers: frozenset[str]

    def wants(self, component: str, device: str, bot: str) -> bool:
        if component not in self.covers:
            return True
        return any(s.component == component and s.bot == bot and device in s.devices
                   for s in self.slots)


def expected_components(machines: MachinesConfig | None, orch: OrchConfig | None) -> Expected:
    """The poller always; each bot's waiter on one of its eligible devices
    (unless orch.yml lists its harness in `refused_harnesses`) and each probed
    device when machines.yml is known; the Linear listener when orch.yml has
    `linear_webhook`, and its tunnel when that sets `public_url`; the
    dashboard writer when orch.yml pushes the dashboard."""
    local = local_device(machines)
    slots = [Slot(POLLER, (local,))]
    covers = {POLLER}
    refused = frozenset(orch.refused_harnesses) if orch is not None else frozenset()
    if machines is not None:
        covers |= {WAITER, DEVICE_PROBE}
        for bot, harness in OWNING_BOTS.items():
            if harness in refused:
                continue
            devices = tuple(d.name for d in machines.devices
                            if d.eligible(harness) and TOKEN.fullmatch(d.name))
            if devices:
                slots.append(Slot(WAITER, devices, bot))
        slots.extend(Slot(DEVICE_PROBE, (t.device,)) for t in probe_targets(machines))
    if orch is not None:
        covers |= {LISTENER, TUNNEL, DASHBOARD}
        if orch.linear_webhook is not None:
            slots.append(Slot(LISTENER, (local,)))
            if orch.linear_webhook.public_url:
                slots.append(Slot(TUNNEL, (local,)))
        if orch.dashboard.push.transport != "none":
            slots.append(Slot(DASHBOARD, (local,)))
    return Expected(tuple(slots), frozenset(covers))


_RANK = {DOWN: 0, STALE: 1, UP: 2}


def _better(a: Component, b: Component) -> bool:
    return (_RANK[a.state], a.last_seen or 0.0) > (_RANK[b.state], b.last_seen or 0.0)


def _latest_checks(rows: Sequence[Heartbeat]) -> list[Heartbeat]:
    """A device probe, tunnel check or dashboard push is a fact about its
    target, not about the process that made it: across processes (a poller
    or listener that restarted), the newest check decides, with the newest
    success as its last seen. Every other component keeps one row per
    process."""
    merged: dict[tuple[str, str, str], Heartbeat] = {}
    others = []
    for row in rows:
        if row.component not in CHECKS:
            others.append(row)
            continue
        key = (row.component, row.device, row.bot)
        held = merged.get(key)
        if held is None:
            merged[key] = row
            continue
        newer = row if _checked(row) > _checked(held) else held
        merged[key] = newer._replace(
            last_seen=max((r.last_seen for r in (row, held) if r.last_seen is not None), default=None),
            checked_at=max(_checked(row), _checked(held)),
        )
    return others + list(merged.values())


def _checked(row: Heartbeat) -> float:
    return max(row.checked_at or 0.0, row.last_seen or 0.0)


def build_report(
    rows: Sequence[Heartbeat] | None,
    now: float,
    *,
    queued: Iterable[tuple[str, str]] = (),
    eligible: Mapping[str, Collection[str]] | None = None,
    expected: Expected | None = None,
    checkout_version: str = "unknown",
    devices: Sequence[DeviceSync] = (),
) -> HealthReport:
    """Classify every row and flag queued jobs no live waiter can take.

    `queued` is (job id, recipient) per queued job; `eligible` maps a harness
    to the devices that may run it (None: any device counts); `expected` is
    the registry of components that should be running (default: the poller
    on the box); `checkout_version` is the checkout's commit: a poller,
    waiter or listener whose row names another known commit runs stale
    code; `devices` are the factory checkouts' sync states."""
    checkout = checkout_version
    checkouts = tuple(devices)
    if rows is None:
        return HealthReport(known=False, code_version=checkout, devices=checkouts)
    registry = expected if expected is not None else Expected(
        (Slot(POLLER, (DEFAULT_DEVICE,)),), frozenset({POLLER}))
    best: dict[tuple[str, str, str], Component] = {}
    for row in _latest_checks(rows):
        state, reason = classify(row, now)
        item = Component(
            component=row.component,
            device=row.device,
            bot=row.bot,
            pid=row.pid,
            state=state,
            reason=reason,
            last_seen=row.last_seen,
            started_at=row.started_at,
            version=row.version,
            interval=row.interval_seconds,
            configured=registry.wants(row.component, row.device, row.bot),
            stale_code=_stale(row.component, row.version, checkout),
        )
        key = (row.component, row.device, row.bot)
        if key not in best or _better(item, best[key]):
            best[key] = item
    components = list(best.values())
    for slot in registry.slots:
        if not any(c.component == slot.component and c.bot == slot.bot
                   and c.device in slot.devices for c in components):
            components.append(Component(slot.component, slot.devices[0], slot.bot, None, DOWN,
                                        _never(slot.component), None, None, "unknown"))
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
    return HealthReport(known=True, components=tuple(components), no_waiter=tuple(flags),
                        code_version=checkout, devices=checkouts)


def _stale(component: str, version: str, checkout: str) -> bool:
    return (component in CODE_COMPONENTS and "unknown" not in (version, checkout)
            and version != checkout)


def load_report(
    db: Path,
    now: float,
    *,
    queued: Iterable[tuple[str, str]] = (),
    machines: MachinesConfig | None = None,
    orch: OrchConfig | None = None,
    root: Path | None = None,
) -> HealthReport:
    """`build_report` from the health file and orch.db's `device_sync` rows;
    an unreadable health file is reported (`error`, which needs attention),
    not raised."""
    box = box_sync(local_device(machines), ROOT if root is None else root)
    devices = ((box,) if box is not None else ()) + tuple(
        DeviceSync.of(row, now) for row in read_device_sync(db))
    try:
        rows = read_heartbeats(db)
    except (sqlite3.Error, OSError) as exc:
        return HealthReport(known=False, error=f"cannot read {health_path(db).name}: {type(exc).__name__}",
                            code_version=factory_version(), devices=devices)
    return build_report(rows, now, queued=queued, eligible=eligible_devices(machines),
                        expected=expected_components(machines, orch),
                        checkout_version=factory_version(), devices=devices)


def read_device_sync(db: Path) -> list[device_sync.Row]:
    """orch.db's `device_sync` rows, read-only; none when the database or table is unreadable."""
    if not db.is_file():
        return []
    # As `orch status` reads it: immutable while no writer holds a -wal, so a
    # read never creates the -shm beside a WAL database.
    query = "?mode=ro" if Path(f"{db}-wal").exists() else "?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(db.resolve().as_uri() + query, uri=True,
                                     timeout=DB_BUSY_SECONDS)
        try:
            connection.execute("PRAGMA query_only = ON")
            return device_sync.read(connection)
        finally:
            connection.close()
    except (sqlite3.Error, OSError, ValueError):
        return []


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
    devices = [_device_line(d) for d in report.devices if d.state != device_sync.SYNCED]
    if report.error is not None:
        return ["Health", f"  unknown: {report.error}", *devices]
    if not report.known:
        return ["Health", "  no heartbeats recorded yet", *devices]
    header = f"{len(report.components)} components, {len(report.down)} down"
    if report.stale_code:
        header += f", {len(report.stale_code)} stale code"
    if report.devices_out_of_sync:
        header += f", {len(report.devices_out_of_sync)} devices out of sync"
    lines = [f"Health ({header})"]
    for c in report.components:
        reason = f" ({c.reason})" if c.reason else ""
        if not c.configured:
            reason += " (not configured)"
        if c.stale_code:
            reason += f", stale code (running {c.version}, checkout {report.code_version})"
        if c.last_seen is None:
            seen = "never seen"
        else:
            seen = f"last seen {et(c.last_seen)} ({age_text(now - c.last_seen)} ago)"
        pid = f", pid {c.pid}" if c.pid is not None else ""
        lines.append(f"  {c.label} {c.state}{reason}, {seen}{pid}")
    for flag in report.no_waiter:
        count = len(flag.jobs)
        lines.append(f"  no waiter for {flag.harness}: {count} queued job{'' if count == 1 else 's'}")
    return lines + devices


def _device_line(d: DeviceSync) -> str:
    who = f"{d.device} {d.bot}" if d.bot else d.device
    line = f"  device {who} {d.state}"
    if d.sha:
        line += f" at {d.sha[:12]}"
    if d.target:
        line += f", target {d.target[:12]}"
    return line + (f" ({d.detail})" if d.detail else "")


def report_json(report: HealthReport, now: float) -> dict:
    return {
        "known": report.known,
        "error": report.error,
        "code_version": report.code_version,
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
                "interval_seconds": c.interval,
                "configured": c.configured,
                "started_at_et": None if c.started_at is None else et(c.started_at),
                "started_at": c.started_at,
                "version": c.version,
                "stale_code": c.stale_code,
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
        "devices": [
            {"device": d.device, "bot": d.bot, "checkout": d.checkout, "sha": d.sha,
             "target": d.target, "state": d.state, "since": d.since, "detail": d.detail,
             "needs_attention": d.needs_attention}
            for d in report.devices
        ],
        "devices_out_of_sync": len(report.devices_out_of_sync),
        "down": len(report.down),
        "stale_code": len(report.stale_code),
        "stale": sum(1 for c in report.components if c.state == STALE and c.configured),
        "thresholds": {
            "beat_seconds": BEAT_SECONDS,
            "probe_seconds": PROBE_SECONDS,
            "stale_beats": STALE_BEATS,
            "down_beats": DOWN_BEATS,
        },
    }


# --- orch health ---------------------------------------------------------------------------

USAGE = """\
orch health [--db PATH] [--machines PATH] [--orch PATH] [--json]
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
    connection = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True,
                                 timeout=DB_BUSY_SECONDS)
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
        if argv[:1] == ["forget"]:
            return _forget(argv[1:])
        return _report(argv, out, clock)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2


def _report(argv: list[str], out: TextIO, clock: Callable[[], float]) -> int:
    parser = _Parser(prog="orch health", usage=USAGE)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--machines", type=Path)
    parser.add_argument("--orch", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    db = _db(args.db)
    machines = load_machines(args.machines)
    now = clock()
    try:
        orch = _orch(args.orch)
        report = load_report(db, now, queued=_queued(db), machines=machines, orch=orch)
    except (HealthError, sqlite3.Error) as exc:
        print(f"orch health: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report_json(report, now), sort_keys=True), file=out)
    else:
        print("\n".join(render_lines(report, now)), file=out)
    if report.error is not None:
        return 1
    return 3 if report.down or report.no_waiter else 0


def _orch(path: Path | None) -> OrchConfig | None:
    """orch.yml (default: the checkout's, when present); an explicit one must load."""
    from lib.orch_yml import load_orch_yml

    orch_path = path if path is not None else ROOT / "orch.yml"
    if path is None and not orch_path.is_file():
        return None
    config, findings = load_orch_yml(orch_path, allow_placeholders=True)
    if config is None or any(item.severity == "error" for item in findings):
        raise HealthError(f"{orch_path}: invalid orch.yml")
    return config


def _identity(parser: _Parser) -> None:
    parser.add_argument("component", choices=COMPONENTS)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--device")
    parser.add_argument("--bot", choices=tuple(OWNING_BOTS))


def _forget(argv: list[str]) -> int:
    parser = _Parser(prog="orch health forget", usage=USAGE)
    _identity(parser)
    args = parser.parse_args(argv)
    db = _db(args.db)
    if not health_path(db).is_file():
        return 0
    try:
        removed = HealthStore(db, devices=()).forget(args.component, device=args.device, bot=args.bot)
    except (OSError, sqlite3.Error) as exc:
        print(f"orch health forget: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"removed {removed} row{'' if removed == 1 else 's'}", file=sys.stdout)
    return 0
