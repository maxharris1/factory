"""Device pool scheduling (docs/SPEC.md F2-01).

A slot is one row in `device_slots`, in the dispatcher database next to
`jobs`. `claim` moves the first queued job for a harness, in the queue's
claim order (lib/queue.py claim_order), to running
and holds a slot for it on an (account, device) pair (#1315). Devices rank
by utilization (slots held over slot_max; a tie goes to a remote before the
local box, remotes in machines.yml order; issue #223) among those that are
schedulable, not draining, and list that harness (issue #85). Of the
accounts signed in for the harness on a free device that are neither
paused, usage-limited, at max_jobs across every device nor under FLOOR fresh
headroom, the one with the highest score wins (then fewer running jobs, its
best device, its id) and takes its best-ranked device. The score is fresh
headroom (UNREAD_HEADROOM with none) less PENDING_CHARGE per job placed
since the account's newest reading (#1316). A job of a project pinned
to devices (orch.yml projects[].devices, #1172) takes only a listed device;
with none free it stays queued and the next job in claim order is tried.
Both writes happen
in one BEGIN IMMEDIATE transaction, so two claims cannot take the same
last slot. A pinned project's QA job (recipient 'qa') holds a slot on a listed
device too, a row in `qa_device_slots` written by `place_qa` in the QA claim's
own transaction. Slots are generic: any free slot on an eligible device takes
any harness. Inside that transaction a pool given `machines_path` reads
the shared settings row (lib/live_settings.py, #852) with machines.yml as
it is now written into it, and places by the row's enabled, drain and
slot_max, and each account's paused, max_jobs and signed_in_on, not by the
config the process loaded. A settings change written under the same lock
(lib/settings_apply.py) that disables, drains or lowers slot_max, or pauses
an account, lowers its max_jobs or drops a device from its signed_in_on,
stops the next claim in every process
(#777). An enable, an undrain, a resume or a higher cap reaches placement
once the poller's DeviceMonitor adopts the file and every machines.yml the
row holds allows it, with no restart. Until the poller first adopts, every
device is closed.

A running harness job with no slot row (claimed without a device: an older
`orch wait`, issue #90) counts as one slot on the box, so the box never runs
more than its slot_max; `lib/wait.py claim_once` refuses such a claim now.

A queued job never holds a slot. Requeueing a claimed job
(SQLiteJobQueue.release) frees its slot in the same transaction, and
claim drops any slot row left on a queued job before counting, so a
requeued job can always be claimed again.

A QA slot counts only while its job is running: it needs no release,
since no device worker runs it.

Finishing, failing or timing out a harness job does not free its slot. Apart
from a requeue, only `DevicePool.release` frees a slot, and F2-02 calls
it only once the worker is confirmed gone: `orch job run` after its
command exits (a remote worker is checked over SSH first), or the
dispatcher's `DeviceMonitor` for a finished job whose runner died.

F2-02 records on the slot row who runs the job: `runner_pid` and
`runner_start` (the box's `orch job run`), `worker_pid` and
`worker_start` (its local command), `remote_started` (a remote start
was attempted, so the device must confirm the worker gone) and
`worker_tag` (the random tag every process of that remote start carries,
lib/remote.py, so a check never trusts a stored process number). #722
adds `run_tag`: the tag every process of a box job's run carries, and #817
`run_scope`: the systemd user scope the run was spawned into on Linux (NULL
for an uncontained run, lib/process_tree.py scope_prefix). #80 adds
`autostart_pid` and `autostart_start`: the autostart waiter that claimed
the job, if one did. #96 adds `run_token_hash`: the hash of the secret
token the job's current `orch job run` holds (never the token itself). The
`device_health` table holds each remote device's last successful SSH
contact and when it began failing, for the lost check (#662). #460
adds `device_backoff`: a device whose start preflight could not reach it
takes no new placement, for any harness, until its `until` time. #662
adds `device_orphans`: the slot of a job lost with its device, moved there
so the slot is free while its worker may still run. #679 reads it back:
the monitor stops that worker once its device answers and forgets the row
once the worker is gone. #722 keeps a lost box job's `run_tag` (and #817
its `run_scope`) there, and `members`, every process of its run seen so
far (pid and start time), so the monitor finds the run again after it
restarts. #1226 adds `device_memory_holds`: a device over its memory limits
takes no new placement until its `until` time, apart from any backoff.

This module starts nothing and contacts no device.
"""

from __future__ import annotations

import math
import sqlite3
from collections import Counter
from collections.abc import Callable, Collection, Mapping
import json
from dataclasses import dataclass, field, replace
from fractions import Fraction
from pathlib import Path

from lib import account_doctor, device_sync, lane_limit, live_settings, usage_readings
from lib.device_sync import NOTHING_BLOCKED, Blocked
from lib.machines_yml import (
    Device,
    MachinesConfig,
    default_account,
    orch_host,
    shared_tailscale_names,
)
from lib.queue import (
    SLOTS_TABLE,
    Job,
    Proof,
    QueueError,
    SQLiteJobQueue,
    _job,
    claim_order,
    require_latest_claim,
    require_proof,
    require_proof_while_slot_held,
    retire_requeued,
)
from lib.remote import new_tag
from lib.schema import HARNESSES

HEALTH_TABLE = "device_health"
BACKOFF_TABLE = "device_backoff"
MEMORY_HOLDS_TABLE = "device_memory_holds"
ORPHANS_TABLE = "device_orphans"
QA_SLOTS_TABLE = "qa_device_slots"
UNREACHABLE_BACKOFF_SECONDS = 5 * 60
# Columns F2-02 added to the F2-01 slot table, added in place on an old database.
_SLOT_COLUMNS = (
    ("runner_pid", "INTEGER"),
    ("runner_start", "TEXT"),
    ("worker_pid", "INTEGER"),
    ("worker_start", "TEXT"),
    ("remote_started", "INTEGER NOT NULL DEFAULT 0"),
    ("worker_tag", "TEXT"),
    # #80: the autostart waiter (`orch wait <bot> --autostart`) that claimed
    # the job, recorded in the claim's own transaction. Only that waiter's
    # child may `orch job run` the job, so a bot turn never runs it too.
    ("autostart_pid", "INTEGER"),
    ("autostart_start", "TEXT"),
    # #96: SHA-256 of the secret token that `orch job run` minted for an
    # autostart run (lib/job_token.py). Only that run, which holds the
    # token, may `orch job done` the job. The token itself is never stored.
    ("run_token_hash", "TEXT"),
    # #722: the RUN_TAG_ENV value every process of a box job's run carries
    # (lib/process_tree.py), so a lost run is found after its parents exit.
    ("run_tag", "TEXT"),
    ("run_scope", "TEXT"),
    ("account", "TEXT"),
)


@dataclass(frozen=True)
class Placement:
    job_id: str
    device: str
    harness: str
    os_user: str
    placed_at: float
    runner_pid: int | None = None
    runner_start: str | None = None
    worker_pid: int | None = None
    worker_start: str | None = None
    remote_started: bool = False
    worker_tag: str | None = None
    autostart_pid: int | None = None
    autostart_start: str | None = None
    run_tag: str | None = None
    run_scope: str | None = None
    account: str | None = None

    @property
    def autostarted(self) -> bool:
        return self.autostart_pid is not None


@dataclass(frozen=True)
class HeldSlot:
    """A held slot and its job's state (None when the job row is gone)."""

    placement: Placement
    job_state: str | None


@dataclass(frozen=True)
class Orphan:
    """A lost job's worker that may still run on its device (#662, #679)."""

    job_id: str
    device: str
    harness: str
    os_user: str
    worker_tag: str | None
    lost_at: float
    runner_pid: int | None = None
    runner_start: str | None = None
    worker_pid: int | None = None
    worker_start: str | None = None
    run_tag: str | None = None
    members: Mapping[int, str] = field(default_factory=dict)
    run_scope: str | None = None


def placed_account(config: MachinesConfig, placement: Placement) -> str | None:
    if placement.account is not None:
        return placement.account
    device = config.device(placement.device)
    return None if device is None else device.account_for(placement.harness)


def run_account(config: MachinesConfig, placement: Placement) -> tuple[str, str | None]:
    """The account the placed run uses and its folder, None for the device's own login.
    Raises ValueError when the account is not signed in on the device."""
    account = placed_account(config, placement) or default_account(placement.harness)
    return account, config.account_home(placement.device, placement.harness, account)


FLOOR = 0.05
PENDING_CHARGE = 0.02
UNREAD_HEADROOM = 0.5


@dataclass(frozen=True)
class AccountLoads:
    running: Mapping[str, int]
    held: frozenset[str]
    closed: frozenset[str]
    score: Mapping[str, float]
    signed_in: frozenset[tuple[str, str]] | None
    signed_out: frozenset[tuple[str, str]]
    running_on: Mapping[str, tuple[str, ...]]

    def signed_in_on(self, config: MachinesConfig, device: str, harness: str) -> list[str]:
        own = config.device(device)
        login = None if own is None else own.account_for(harness)
        return [
            account
            for account in config.accounts_on(device, harness)
            if (account, device) not in self.signed_out
            and (account == login or self.signed_in is None or (account, device) in self.signed_in)
        ]

    def taking_work(self, config: MachinesConfig, device: str, harness: str) -> list[str]:
        return [
            account
            for account in self.signed_in_on(config, device, harness)
            if account not in self.closed
        ]

    def holds(self, account: str, device: str) -> bool:
        return account in self.held or (account, device) in self.signed_out

    def signed_in_devices(self, config: MachinesConfig) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        for device in config.devices:
            if not device.enabled or device.placeholder:
                continue
            for harness in device.harnesses:
                for account in self.signed_in_on(config, device.name, harness):
                    devices = found.setdefault(account, [])
                    if device.name not in devices:
                        devices.append(device.name)
        return found

    def score_of(self, account: str) -> float:
        return self.score.get(account, UNREAD_HEADROOM)


@dataclass(frozen=True)
class RunningPair:
    job_id: str
    account: str
    device: str
    placed_at: float


def running_pairs(connection: sqlite3.Connection, config: MachinesConfig | None) -> list[RunningPair]:
    if not _has_table(connection, SLOTS_TABLE):
        return []
    present = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({SLOTS_TABLE})")}
    select = ", ".join(
        name if name in present else "NULL" for name in ("job_id", "device", "harness", "account", "placed_at")
    )
    pairs = []
    for job_id, device_name, harness, account, placed_at in connection.execute(
        f"SELECT {select} FROM {SLOTS_TABLE}"
    ):
        if placed_at is None:
            continue
        if account is None:
            device = None if config is None else config.device(str(device_name))
            account = None if device is None else device.account_for(str(harness))
        if account is not None:
            pairs.append(RunningPair(str(job_id), str(account), str(device_name), float(placed_at)))
    return pairs


def account_loads(
    connection: sqlite3.Connection, config: MachinesConfig | None, now: float
) -> AccountLoads:
    pairs = running_pairs(connection, config)
    placed = [(pair.account, pair.placed_at) for pair in pairs]
    devices_of: dict[str, set[str]] = {}
    for pair in pairs:
        devices_of.setdefault(pair.account, set()).add(pair.device)
    order = {device.name: index for index, device in enumerate(config.devices if config is not None else ())}
    running_on = {
        account: tuple(sorted(names, key=lambda name: (order.get(name, len(order)), name)))
        for account, names in devices_of.items()
    }
    running = Counter(account for account, _ in placed)
    newest = usage_readings.newest_by_account(usage_readings.stored(connection) or [])
    read_at = {account: max(r.observed_at for r in windows.values()) for account, windows in newest.items()}
    pending = Counter(account for account, at in placed if at > read_at.get(account, -math.inf))
    fresh = {account: usage_readings.headroom(list(windows.values()), now) for account, windows in newest.items()}
    score = {
        account: round(
            (UNREAD_HEADROOM if fresh.get(account) is None else fresh[account])
            - PENDING_CHARGE * pending[account],
            6,
        )
        for account in running.keys() | fresh.keys()
    }
    held = set(lane_limit.account_limits(connection, now))
    held.update(account for account, left in fresh.items() if left is not None and left < FLOOR)
    closed = set(held)
    for item in config.accounts if config is not None else ():
        if item.paused or (item.max_jobs is not None and running.get(item.id, 0) >= item.max_jobs):
            closed.add(item.id)
    signed_in = None
    if _has_table(connection, account_doctor.TABLE):
        signed_in = frozenset(
            (str(account), str(device))
            for account, device in connection.execute(
                f"SELECT account, device FROM {account_doctor.TABLE} WHERE status = 'ok'"
            )
        )
    signed_out = account_doctor.held(connection) if _has_table(connection, account_doctor.HOLDS) else frozenset()
    if _has_table(connection, account_doctor.SIGNOUTS):
        signed_out |= account_doctor.signing_out(connection, now)
    return AccountLoads(running, frozenset(held), frozenset(closed), score, signed_in, signed_out, running_on)


def _ranked_free(
    config: MachinesConfig,
    harness: str,
    used: Mapping[str, int],
    blocked: Blocked,
    avoid: Collection[str],
    allowed: Collection[str] | None,
) -> list[tuple[tuple[bool, Fraction, bool, int], Device]]:
    candidates = []
    for index, device in enumerate(config.devices):
        if not _places(device, harness, blocked, allowed):
            continue
        assert device.slot_max is not None
        held = used.get(device.name, 0)
        if held < device.slot_max:
            rank = (device.name in avoid, Fraction(held, device.slot_max), device.local, index)
            candidates.append((rank, device))
    return sorted(candidates, key=lambda candidate: candidate[0])


def choose_pair(
    config: MachinesConfig,
    harness: str,
    used: Mapping[str, int],
    blocked: Blocked,
    loads: AccountLoads,
    avoid: Collection[str] = frozenset(),
    allowed: Collection[str] | None = None,
) -> tuple[str, Device] | None:
    """The account to run the next job and its best free device, or None (#1315).

    The avoid flag leads, so a device in `avoid` is used only when no other
    pair exists (#218).
    """
    best: dict[str, tuple[tuple[bool, Fraction, bool, int], Device]] = {}
    for rank, device in _ranked_free(config, harness, used, blocked, avoid, allowed):
        for account in loads.taking_work(config, device.name, harness):
            best.setdefault(account, (rank, device))
    if not best:
        return None

    def order(account: str) -> tuple:
        rank, _ = best[account]
        avoided = rank[0]
        return avoided, -loads.score_of(account), loads.running.get(account, 0), rank, account

    account = min(best, key=order)
    return account, best[account][1]


def choose_device(
    config: MachinesConfig,
    harness: str,
    used: Mapping[str, int],
    blocked: Blocked = NOTHING_BLOCKED,
    avoid: Collection[str] = frozenset(),
    allowed: Collection[str] | None = None,
) -> Device | None:
    """The schedulable, non-draining device listing harness with the lowest utilization, or None.

    Utilization is slots held over slot_max, compared exactly. A tie goes to
    a remote device before the local box, remotes in machines.yml order
    (issue #223). A full device is never chosen, nor one whose factory
    checkout for harness is out of sync or being updated (#297). A device
    in `avoid` is chosen only when no other has a free slot (#218). With
    `allowed`, no device outside it is chosen.
    """
    ranked = _ranked_free(config, harness, used, blocked, avoid, allowed)
    return ranked[0][1] if ranked else None


def free_slots(
    config: MachinesConfig,
    harness: str,
    used: Mapping[str, int],
    blocked: Blocked = NOTHING_BLOCKED,
    allowed: Collection[str] | None = None,
) -> int:
    """Free slots, summed over every device listing harness that choose_device may pick."""
    total = 0
    for device in config.devices:
        if _places(device, harness, blocked, allowed):
            assert device.slot_max is not None
            total += max(0, device.slot_max - used.get(device.name, 0))
    return total


def _places(
    device: Device, harness: str, blocked: Blocked, allowed: Collection[str] | None
) -> bool:
    return (
        device.takes_new(harness)
        and not blocked.blocks(device.name, harness)
        and (allowed is None or device.name in allowed)
    )


class DevicePool:
    def __init__(
        self,
        queue: SQLiteJobQueue,
        config: MachinesConfig,
        *,
        machines_path: Path | None = None,
        head: Callable[[], str | None] = lambda: None,
        pins: Callable[[], Mapping[str, frozenset[str]] | None] = dict,
    ):
        shared = shared_tailscale_names(config.devices)
        if shared:
            raise QueueError(
                f"devices {', '.join(shared[0])} share one tailscale_name; "
                "one host has one slot_max"
            )
        self.queue = queue
        self.config = config
        self.machines_path = machines_path
        self.head = head
        self.pins = pins
        if not self._needs_schema():
            return
        with self.queue.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {SLOTS_TABLE} (
                    job_id TEXT PRIMARY KEY,
                    device TEXT NOT NULL,
                    harness TEXT NOT NULL,
                    os_user TEXT NOT NULL,
                    placed_at REAL NOT NULL
                )
                """
            )
            present = {
                str(row["name"])
                for row in connection.execute(f"PRAGMA table_info({SLOTS_TABLE})")
            }
            for name, kind in _SLOT_COLUMNS:
                if name not in present:
                    connection.execute(
                        f"ALTER TABLE {SLOTS_TABLE} ADD COLUMN {name} {kind}"
                    )
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {HEALTH_TABLE} (
                    device TEXT PRIMARY KEY,
                    last_ok_at REAL,
                    failing_since REAL
                )
                """
            )
            connection.commit()

    def _needs_schema(self) -> bool:
        """True unless both tables and every F2-02 column already exist.

        Checked without a write lock, so a waiter starting while the poller
        writes does not queue behind it just to find nothing to do.
        """
        with self.queue.connect() as connection:
            columns = {
                str(row["name"])
                for row in connection.execute(f"PRAGMA table_info({SLOTS_TABLE})")
            }
            health = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (HEALTH_TABLE,),
            ).fetchone()
        return health is None or not {name for name, _ in _SLOT_COLUMNS} <= columns

    def _placement(
        self, connection: sqlite3.Connection
    ) -> tuple[MachinesConfig, live_settings.Live | None]:
        if self.machines_path is None:
            return self.config, None
        live = live_settings.recorded(connection, self.machines_path)
        return live.placement_config(self.config), live

    def placement_config(self) -> MachinesConfig:
        with self.queue.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            config, _ = self._placement(connection)
            connection.commit()
        return config

    def serving(self, harness: str) -> frozenset[str]:
        """The accounts of harness on devices placement may use."""
        return self.placement_config().serving(harness)

    def place_qa(self, connection: sqlite3.Connection, job_id: str, repo: str) -> bool:
        """Hold a slot for a pinned project's QA job on a listed device.

        Runs inside the caller's open BEGIN IMMEDIATE transaction, the one
        that moves the job to running. True for an unpinned project, which
        holds no slot; False when no listed device has a free slot, and for
        every project while the pins are unknown (None). QA runs under no
        harness account, so a limited or capped account never closes its slot.
        """
        pins = self.pins()
        if pins is None:
            return False
        allowed = pins.get(repo.lower())
        if allowed is None:
            return True
        now = self.queue.now()
        config, _ = self._placement(connection)
        blocked = device_blocked(connection, now, config, self.head())
        used = _used(connection, config)
        for harness in sorted(HARNESSES):
            device = choose_device(config, harness, used, blocked, allowed=allowed)
            if device is not None:
                break
        else:
            return False
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {QA_SLOTS_TABLE} (
                job_id TEXT PRIMARY KEY,
                device TEXT NOT NULL,
                placed_at REAL NOT NULL
            )
            """
        )
        connection.execute(
            f"""
            DELETE FROM {QA_SLOTS_TABLE}
            WHERE job_id NOT IN (SELECT id FROM jobs WHERE state = 'running')
            """
        )
        connection.execute(
            f"INSERT OR REPLACE INTO {QA_SLOTS_TABLE} (job_id, device, placed_at) VALUES (?, ?, ?)",
            (job_id, device.name, now),
        )
        return True

    def claim(
        self,
        harness: str,
        *,
        excluded_job_ids: Collection[str] = (),
        autostart: tuple[int, str | None] | None = None,
        token_hash: str | None = None,
    ) -> tuple[Job, Placement] | None:
        """Claim the oldest queued job for harness onto a free slot.

        `autostart` is the claiming autostart waiter's (pid, start); it is
        written on the slot in the same transaction as the claim.
        """
        if harness not in HARNESSES:
            raise QueueError(f"{harness!r} is not a harness; only harness jobs hold slots")
        excluded = frozenset(excluded_job_ids)
        pins = self.pins()
        if pins is None:
            return None
        refused = getattr(self.pins, "refused_harnesses", None)
        if callable(refused) and harness in refused():
            return None
        now = self.queue.now()
        with self.queue.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            config, live = self._placement(connection)
            if live is not None and live.places_by_adopted():
                live_settings.record_seen(connection, harness, live.adopted)
            connection.execute(
                f"""
                DELETE FROM {SLOTS_TABLE}
                WHERE job_id IN (SELECT id FROM jobs WHERE state = 'queued')
                """
            )
            blocked = placement_blocked(connection, now, config, self.head())
            used = _used(connection, config)
            loads = account_loads(connection, config, now)
            if choose_pair(config, harness, used, blocked, loads) is None:
                connection.commit()
                return None
            for row in claim_order(
                connection, harness, now=now, serving=config.serving(harness)
            ):
                if str(row["id"]) in excluded:
                    continue
                allowed = pins.get(str(row["repo"]).lower())
                pair = choose_pair(config, harness, used, blocked, loads, allowed=allowed)
                if pair is not None:
                    break
            else:
                connection.commit()
                return None
            dropped = _lost_on(connection, str(row["id"]))
            if dropped == pair[1].name:
                pair = choose_pair(
                    config, harness, used, blocked, loads, avoid={dropped}, allowed=allowed
                )
                assert pair is not None
            account, device = pair
            changed = connection.execute(
                """
                UPDATE jobs
                SET state = 'running', last_activity_at = ?, claim_token_hash = ?
                WHERE id = ? AND state = 'queued'
                """,
                (now, token_hash, row["id"]),
            ).rowcount
            if changed != 1:
                raise QueueError(f"job {row['id']} changed during claim")
            os_user = device.harnesses[harness]
            claimer_pid, claimer_start = autostart if autostart is not None else (None, None)
            connection.execute(
                f"""
                INSERT INTO {SLOTS_TABLE} (
                    job_id, device, harness, os_user, placed_at,
                    autostart_pid, autostart_start, account
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (row["id"], device.name, harness, os_user, now, claimer_pid, claimer_start, account),
            )
            updated = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (row["id"],),
            ).fetchone()
            assert updated is not None
            job = _job(updated, connection)
            connection.commit()
        return job, Placement(
            job_id=str(row["id"]),
            device=device.name,
            harness=harness,
            os_user=os_user,
            placed_at=now,
            autostart_pid=claimer_pid,
            autostart_start=claimer_start,
            account=account,
        )

    def release(self, job_id: str, *, owner: str, proof: Proof | None = None) -> bool:
        """Free a job's slot once its worker is confirmed gone. False if already free."""
        if owner not in HARNESSES:
            raise QueueError(f"unknown owner {owner!r}")
        with self.queue.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            require_proof_while_slot_held(connection, job_id, proof)
            row = connection.execute(
                f"SELECT harness FROM {SLOTS_TABLE} WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                connection.commit()
                return False
            if row["harness"] != owner:
                raise QueueError(
                    f"slot for job {job_id} belongs to {row['harness']}, not {owner}"
                )
            connection.execute(
                f"DELETE FROM {SLOTS_TABLE} WHERE job_id = ?",
                (job_id,),
            )
            connection.commit()
        return True

    def requeue_unstarted(
        self, job_id: str, *, owner: str, runner: int, reason: str, proof: Proof | None = None
    ) -> bool:
        """Requeue a placed job whose start failed before any worker (#94).

        One transaction, guarded: the job is still running for owner, and
        its slot is still run by `runner` (this `orch job run`) with no
        worker recorded, local or remote. The job goes back to queued with
        its queue-wait clock restarted (created_at is kept, so it keeps its
        place in line), its slot is freed, and nothing else changes: no
        round, no attempt, no failure. Records a `requeued-unstarted` job
        event when the dispatcher's job_events table exists. On a finished
        ticket, or for a restored claim, the job is cancelled instead
        (retire_requeued, #108). False (nothing changed) when the guard does
        not hold.
        """
        if owner not in HARNESSES:
            raise QueueError(f"unknown owner {owner!r}")
        now = self.queue.now()
        with self.queue.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            require_proof(connection, job_id, proof)
            changed = connection.execute(
                f"""
                UPDATE jobs SET state = 'queued', last_activity_at = ?
                WHERE id = ? AND state = 'running' AND recipient = ?
                  AND EXISTS (
                    SELECT 1 FROM {SLOTS_TABLE}
                    WHERE job_id = ? AND harness = ? AND runner_pid = ?
                      AND worker_pid IS NULL AND COALESCE(remote_started, 0) = 0
                  )
                """,
                (now, job_id, owner, job_id, owner, runner),
            ).rowcount
            if changed != 1:
                connection.commit()
                return False
            connection.execute(f"DELETE FROM {SLOTS_TABLE} WHERE job_id = ?", (job_id,))
            if retire_requeued(connection, job_id, reason, now=now) is None and _has_table(
                connection, "job_events"
            ):
                job = connection.execute(
                    "SELECT repo, ticket_id, action, recipient FROM jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO job_events (
                        job_id, repo, ticket_id, action, recipient, event, detail, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, 'requeued-unstarted', ?, ?)
                    """,
                    (
                        job_id,
                        str(job["repo"]).lower(),
                        job["ticket_id"],
                        job["action"],
                        job["recipient"],
                        f"{reason}; requeued, no round charged",
                        now,
                    ),
                )
            connection.commit()
        return True

    def placement(self, job_id: str) -> Placement | None:
        with self.queue.connect() as connection:
            row = connection.execute(
                f"SELECT * FROM {SLOTS_TABLE} WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return _placement(row)

    def attach_runner(
        self,
        job_id: str,
        *,
        owner: str,
        pid: int,
        start: str | None,
        alive: Callable[[int | None, str | None], bool],
        token_hash: str | None = None,
        proof: Proof | None = None,
    ) -> Placement:
        """Record the box process (`orch job run`) that runs a placed job.

        Refused while another recorded runner is still alive, so two runners
        never share one slot (the later one could release it under the other).
        `token_hash` (#96) is written in the same update, replacing any
        earlier run's, so only this run's token can finish the job.

        Returns the slot as read inside that transaction, with this runner
        on it (#70 F2.1): a worker an earlier runner recorded after the
        caller's own read is on it, so takeover is decided on this row.
        """

        def guard(row: sqlite3.Row) -> None:
            other = row["runner_pid"]
            if (
                other is not None
                and int(other) != pid
                and alive(int(other), _text(row["runner_start"]))
            ):
                raise QueueError(
                    f"job {job_id} is already run by pid {other} (a live orch job run); "
                    "a second run of a running job is refused and nothing was started"
                )

        row = self._update_slot(
            job_id,
            owner,
            "runner_pid = ?, runner_start = ?, run_token_hash = ?",
            (pid, start, token_hash),
            guard=guard,
            proof=proof,
        )
        return replace(_placement(row), runner_pid=pid, runner_start=start)

    def attach_worker(
        self,
        job_id: str,
        *,
        owner: str,
        pid: int | None = None,
        start: str | None = None,
        remote: bool = False,
        tag: str | None = None,
        run_tag: str | None = None,
        run_scope: str | None = None,
        runner: int | None = None,
        proof: Proof | None = None,
    ) -> str | None:
        """Record the worker: a local command's pid, or that a remote start began.

        A remote start is recorded, with its worker tag (a fresh one unless
        given), before ssh is spawned, so a runner that dies mid-spawn still
        leaves the device to confirm the worker gone. Returns the tag.

        With `runner` (#70 F2.1), refused unless that runner still runs the
        slot and the slot names no worker yet: a worker's identity is never
        overwritten.
        """

        def guard(row: sqlite3.Row) -> None:
            slot = _placement(row)
            if slot.runner_pid != runner:
                raise QueueError(
                    f"job {job_id} is now run by pid {slot.runner_pid}, not {runner}; "
                    "its worker was not recorded"
                )
            if slot.worker_pid is not None or slot.remote_started:
                named = slot.worker_pid if slot.worker_pid is not None else f"tag {slot.worker_tag}"
                raise QueueError(
                    f"job {job_id}'s slot already names worker {named}; "
                    "it is never overwritten"
                )

        checked = guard if runner is not None else None
        if remote:
            worker_tag = tag if tag is not None else new_tag()
            self._update_slot(
                job_id,
                owner,
                "remote_started = 1, worker_tag = ?",
                (worker_tag,),
                guard=checked,
                proof=proof,
            )
            return worker_tag
        self._update_slot(
            job_id,
            owner,
            "worker_pid = ?, worker_start = ?, run_tag = ?, run_scope = ?",
            (pid, start, run_tag, run_scope),
            guard=checked,
            proof=proof,
        )
        return None

    def forget_worker(
        self,
        job_id: str,
        *,
        owner: str,
        runner: int,
        earlier: Placement,
        proof: Proof | None = None,
    ) -> None:
        """Drop the record of an earlier runner's worker, now confirmed gone.

        Guarded: the slot is still run by `runner` and still names exactly
        the worker that was confirmed gone, so a newer record is never
        dropped. The slot then reads as never started, and a start that
        fails before any worker can be requeued (#94).
        """

        def worker(slot: Placement) -> tuple[object, ...]:
            return (slot.worker_pid, slot.worker_start, slot.remote_started, slot.worker_tag)

        def guard(row: sqlite3.Row) -> None:
            slot = _placement(row)
            if slot.runner_pid != runner or worker(slot) != worker(earlier):
                raise QueueError(
                    f"job {job_id}'s slot changed while its earlier worker was checked; "
                    "nothing was started"
                )

        self._update_slot(
            job_id,
            owner,
            "worker_pid = NULL, worker_start = NULL, remote_started = 0, worker_tag = NULL, "
            "run_tag = NULL, run_scope = NULL",
            (),
            guard=guard,
            proof=proof,
        )

    def held(self) -> tuple[HeldSlot, ...]:
        """Every held slot with its job's state, oldest placement first."""
        with self.queue.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT slots.*, jobs.state AS job_state
                FROM {SLOTS_TABLE} AS slots
                LEFT JOIN jobs ON jobs.id = slots.job_id
                ORDER BY slots.placed_at, slots.job_id
                """
            ).fetchall()
        return tuple(
            HeldSlot(
                placement=_placement(row),
                job_state=(
                    str(row["job_state"]) if row["job_state"] is not None else None
                ),
            )
            for row in rows
        )

    def health(self, device: str) -> tuple[float | None, float | None]:
        """(last_ok_at, failing_since) for a device, or (None, None)."""
        with self.queue.connect() as connection:
            row = connection.execute(
                f"SELECT last_ok_at, failing_since FROM {HEALTH_TABLE} WHERE device = ?",
                (device,),
            ).fetchone()
        if row is None:
            return None, None
        return _float(row["last_ok_at"]), _float(row["failing_since"])

    def record_contact(self, device: str, *, ok: bool, at: float) -> float | None:
        """Record one SSH probe. Returns failing_since (None while reachable)."""
        with self.queue.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if ok:
                connection.execute(
                    f"""
                    INSERT INTO {HEALTH_TABLE} (device, last_ok_at, failing_since)
                    VALUES (?, ?, NULL)
                    ON CONFLICT(device) DO UPDATE SET
                        last_ok_at = excluded.last_ok_at,
                        failing_since = NULL
                    """,
                    (device, at),
                )
                connection.commit()
                return None
            connection.execute(
                f"""
                INSERT INTO {HEALTH_TABLE} (device, last_ok_at, failing_since)
                VALUES (?, NULL, ?)
                ON CONFLICT(device) DO UPDATE SET
                    failing_since = COALESCE(failing_since, excluded.failing_since)
                """,
                (device, at),
            )
            row = connection.execute(
                f"SELECT failing_since FROM {HEALTH_TABLE} WHERE device = ?",
                (device,),
            ).fetchone()
            connection.commit()
        return _float(row["failing_since"])

    def forget_health(self, *, keep: Collection[str]) -> None:
        """Drop the offline clock of every device that holds no slot now.

        A later failure then starts a fresh clock instead of resuming one
        left from an earlier outage.
        """
        kept = sorted(set(keep))
        marks = ", ".join("?" for _ in kept)
        where = f"WHERE device NOT IN ({marks})" if kept else ""
        with self.queue.connect() as connection:
            connection.execute(f"DELETE FROM {HEALTH_TABLE} {where}", kept)

    def orphans(self) -> list[Orphan]:
        with self.queue.connect() as connection:
            if not _has_table(connection, ORPHANS_TABLE):
                return []
            rows = connection.execute(
                f"SELECT * FROM {ORPHANS_TABLE} ORDER BY lost_at, job_id"
            ).fetchall()
        return [_orphan(row) for row in rows]

    def remember_run(self, job_id: str, tag: str | None, members: Mapping[int, str]) -> None:
        """Keep a lost box job's run tag and every process of it seen so far (pid -> start)."""
        with self.queue.connect() as connection:
            if _has_table(connection, ORPHANS_TABLE):
                _orphan_columns(connection)
                connection.execute(
                    f"UPDATE {ORPHANS_TABLE} SET run_tag = ?, members = ? WHERE job_id = ?",
                    (tag, json.dumps({str(pid): start for pid, start in members.items()}), job_id),
                )

    def forget_orphan(self, job_id: str) -> None:
        with self.queue.connect() as connection:
            if _has_table(connection, ORPHANS_TABLE):
                connection.execute(f"DELETE FROM {ORPHANS_TABLE} WHERE job_id = ?", (job_id,))

    def _update_slot(
        self,
        job_id: str,
        owner: str,
        assignments: str,
        values: tuple[object, ...],
        *,
        guard: Callable[[sqlite3.Row], None] | None = None,
        proof: Proof | None = None,
    ) -> sqlite3.Row:
        """Update one slot in its own transaction; returns the row read before the update."""
        if owner not in HARNESSES:
            raise QueueError(f"unknown owner {owner!r}")
        with self.queue.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            require_latest_claim(connection, job_id, proof)
            row = connection.execute(
                f"SELECT * FROM {SLOTS_TABLE} WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                connection.commit()
                raise QueueError(f"job {job_id} holds no device slot")
            if row["harness"] != owner:
                raise QueueError(
                    f"slot for job {job_id} belongs to {row['harness']}, not {owner}"
                )
            if guard is not None:
                guard(row)
            connection.execute(
                f"UPDATE {SLOTS_TABLE} SET {assignments} WHERE job_id = ?",
                (*values, job_id),
            )
            connection.commit()
        return row

    def used(self) -> dict[str, int]:
        with self.queue.connect() as connection:
            return _used(connection, self.config)


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def read_autostart(
    connection: sqlite3.Connection, job_id: str
) -> tuple[int, str | None] | None:
    """The autostart waiter (pid, start) that claimed job_id, read without writing.

    None when the job holds no slot, was not claimed by an autostart waiter,
    or the slot table predates the autostart columns (no autostart claim can
    exist there).
    """
    present = {
        str(row[1]) for row in connection.execute(f"PRAGMA table_info({SLOTS_TABLE})")
    }
    if not {"job_id", "autostart_pid", "autostart_start"} <= present:
        return None
    row = connection.execute(
        f"SELECT autostart_pid, autostart_start FROM {SLOTS_TABLE} WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    pid = _int(tuple(row)[0])
    return None if pid is None else (pid, _text(tuple(row)[1]))


def _orphan(row: sqlite3.Row) -> Orphan:
    keys = set(row.keys())
    return Orphan(
        job_id=str(row["job_id"]),
        device=str(row["device"]),
        harness=str(row["harness"]),
        os_user=str(row["os_user"]),
        worker_tag=_text(row["worker_tag"]),
        lost_at=float(row["lost_at"]),
        runner_pid=_int(row["runner_pid"]) if "runner_pid" in keys else None,
        runner_start=_text(row["runner_start"]) if "runner_start" in keys else None,
        worker_pid=_int(row["worker_pid"]) if "worker_pid" in keys else None,
        worker_start=_text(row["worker_start"]) if "worker_start" in keys else None,
        run_tag=_text(row["run_tag"]) if "run_tag" in keys else None,
        members={
            int(pid): start for pid, start in json.loads(row["members"] or "{}").items()
        }
        if "members" in keys
        else {},
        run_scope=_text(row["run_scope"]) if "run_scope" in keys else None,
    )


def _placement(row: sqlite3.Row) -> Placement:
    return Placement(
        job_id=str(row["job_id"]),
        device=str(row["device"]),
        harness=str(row["harness"]),
        os_user=str(row["os_user"]),
        placed_at=float(row["placed_at"]),
        runner_pid=_int(row["runner_pid"]),
        runner_start=_text(row["runner_start"]),
        worker_pid=_int(row["worker_pid"]),
        worker_start=_text(row["worker_start"]),
        remote_started=bool(row["remote_started"]),
        worker_tag=_text(row["worker_tag"]),
        autostart_pid=_int(row["autostart_pid"]),
        autostart_start=_text(row["autostart_start"]),
        run_tag=_text(row["run_tag"]),
        run_scope=_text(row["run_scope"]),
        account=_text(row["account"]),
    )


def _int(value: object) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _text(value: object) -> str | None:
    return str(value) if value is not None else None


def _float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _backoff_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {BACKOFF_TABLE} (
            device TEXT PRIMARY KEY,
            until REAL NOT NULL,
            reason TEXT NOT NULL
        )
        """
    )


def back_off(connection: sqlite3.Connection, device: str, *, until: float, reason: str) -> None:
    """Keep new placements off device until `until`, in the caller's transaction (#460)."""
    _backoff_table(connection)
    connection.execute(
        f"""
        INSERT INTO {BACKOFF_TABLE} (device, until, reason) VALUES (?, ?, ?)
        ON CONFLICT(device) DO UPDATE SET
            until = MAX(until, excluded.until),
            reason = excluded.reason
        """,
        (device, until, reason),
    )


def _memory_holds_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MEMORY_HOLDS_TABLE} (
            device TEXT PRIMARY KEY,
            until REAL NOT NULL,
            reason TEXT NOT NULL
        )
        """
    )


def hold_memory(connection: sqlite3.Connection, device: str, *, until: float, reason: str) -> None:
    _memory_holds_table(connection)
    connection.execute(
        f"INSERT OR REPLACE INTO {MEMORY_HOLDS_TABLE} (device, until, reason) VALUES (?, ?, ?)",
        (device, until, reason),
    )


def memory_held(connection: sqlite3.Connection, device: str, now: float) -> bool:
    _memory_holds_table(connection)
    row = connection.execute(
        f"SELECT 1 FROM {MEMORY_HOLDS_TABLE} WHERE device = ? AND until > ?", (device, now)
    ).fetchone()
    return row is not None


def clear_memory_hold(connection: sqlite3.Connection, device: str, now: float) -> None:
    """End device's hold at now; the row stays so the queue-wait clock sees when it ended."""
    _memory_holds_table(connection)
    connection.execute(
        f"UPDATE {MEMORY_HOLDS_TABLE} SET until = MIN(until, ?) WHERE device = ?", (now, device)
    )


def memory_holds(queue: SQLiteJobQueue) -> dict[str, str]:
    with queue.connect() as connection:
        if not _has_table(connection, MEMORY_HOLDS_TABLE):
            return {}
        rows = connection.execute(
            f"SELECT device, reason FROM {MEMORY_HOLDS_TABLE} WHERE until > ?", (queue.now(),)
        ).fetchall()
    return {str(row[0]): str(row[1]) for row in rows}


_ORPHAN_PIDS = (
    ("runner_pid", "INTEGER"),
    ("runner_start", "TEXT"),
    ("worker_pid", "INTEGER"),
    ("worker_start", "TEXT"),
    ("run_tag", "TEXT"),
    ("run_scope", "TEXT"),
)


def _orphan_columns(connection: sqlite3.Connection) -> None:
    present = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({ORPHANS_TABLE})")}
    for name, kind in (*_ORPHAN_PIDS, ("members", "TEXT")):
        if name not in present:
            connection.execute(f"ALTER TABLE {ORPHANS_TABLE} ADD COLUMN {name} {kind}")


def orphan_slot(connection: sqlite3.Connection, job_id: str, now: float) -> None:
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {ORPHANS_TABLE} (
            job_id TEXT PRIMARY KEY,
            device TEXT NOT NULL,
            harness TEXT NOT NULL,
            os_user TEXT NOT NULL,
            worker_tag TEXT,
            lost_at REAL NOT NULL
        )
        """
    )
    _orphan_columns(connection)
    pids = ", ".join(name for name, _ in _ORPHAN_PIDS)
    connection.execute(
        f"""
        INSERT OR REPLACE INTO {ORPHANS_TABLE}
            (job_id, device, harness, os_user, worker_tag, lost_at, {pids})
        SELECT job_id, device, harness, os_user, worker_tag, ?, {pids} FROM {SLOTS_TABLE}
        WHERE job_id = ?
        """,
        (now, job_id),
    )
    connection.execute(f"DELETE FROM {SLOTS_TABLE} WHERE job_id = ?", (job_id,))


def device_blocked(
    connection: sqlite3.Connection,
    now: float,
    config: MachinesConfig | None,
    head: str | None,
    *,
    memory: bool = True,
) -> Blocked:
    blocked = device_sync.checkout_blocks(device_sync.read(connection), head, orch_host(config))
    tables = (BACKOFF_TABLE, MEMORY_HOLDS_TABLE) if memory else (BACKOFF_TABLE,)
    backed_off = {
        str(row[0])
        for table in tables
        if _has_table(connection, table)
        for row in connection.execute(f"SELECT device FROM {table} WHERE until > ?", (now,))
    }
    return replace(blocked, devices=blocked.devices | backed_off)


def placement_blocked(
    connection: sqlite3.Connection,
    now: float,
    config: MachinesConfig | None,
    head: str | None,
    *,
    memory: bool = True,
) -> Blocked:
    blocked = device_blocked(connection, now, config, head, memory=memory)
    loads = account_loads(connection, config, now)
    pairs = {
        (device.name, harness)
        for device in (config.devices if config is not None else ())
        for harness in device.harnesses
        if not loads.taking_work(config, device.name, harness)
    }
    return replace(blocked, pairs=blocked.pairs | pairs)


def placement_blocks(
    queue: SQLiteJobQueue, config: MachinesConfig | None, head: str | None, *, memory: bool = True
) -> Blocked:
    with queue.connect() as connection:
        return placement_blocked(connection, queue.now(), config, head, memory=memory)


def backed_off_through(queue: SQLiteJobQueue, config: MachinesConfig) -> dict[str, float]:
    now = queue.now()
    until: dict[str, float] = {}
    with queue.connect() as connection:
        for table in (BACKOFF_TABLE, MEMORY_HOLDS_TABLE):
            if _has_table(connection, table):
                for device, at in connection.execute(f"SELECT device, until FROM {table}"):
                    until[str(device)] = max(until.get(str(device), 0.0), min(float(at), now))
    through: dict[str, float] = {}
    for device in config.devices:
        if device.name not in until:
            continue
        for harness in HARNESSES:
            if device.eligible(harness):
                through[harness] = max(through.get(harness, 0.0), until[device.name])
    return through


def used_slots(queue: SQLiteJobQueue, config: MachinesConfig | None) -> dict[str, int]:
    """Slots in use per device, as claims see them (issue #90)."""
    with queue.connect() as connection:
        return _used(connection, config)


def slots_in_use(
    connection: sqlite3.Connection,
    config: MachinesConfig | None,
) -> dict[str, int]:
    """used_slots on an open connection (the feeder's capacity read)."""
    return _used(connection, config)


LINK_DROP_TAG = "; worker tag "


def link_drop_detail(device: str, minutes: str, tag: str) -> str:
    return f"{device}: no certain answer for {minutes}{LINK_DROP_TAG}{tag}"


def device_lost_detail(device: str, reason: str, tag: str | None) -> str:
    return f"{device}: {reason}" + ("" if tag is None else f"{LINK_DROP_TAG}{tag}")


def _lost_on(connection: sqlite3.Connection, job_id: str) -> str | None:
    """#218: the device job_id's latest run lost its link to, or None."""
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'job_events'"
    ).fetchone()
    if table is None:
        return None
    row = connection.execute(
        """
        SELECT event, detail FROM job_events
        WHERE job_id = ? AND event IN ('run-died', 'run-exited', 'link-drop', 'device-lost')
        ORDER BY id DESC LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    if row is None or row[0] not in ("link-drop", "device-lost"):
        return None
    return str(row[1]).split(": ", 1)[0]


def _used(
    connection: sqlite3.Connection,
    config: MachinesConfig | None = None,
) -> dict[str, int]:
    """Held slots per device, running QA jobs' slots, plus every running
    harness job that has no placement (a claim made without a device, issue #90) on the orch host.

    Such a job runs on the orch host all the same, so it takes a slot there:
    the host never runs more than its slot_max, however the job was claimed.
    """
    has_slots = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (SLOTS_TABLE,),
    ).fetchone() is not None
    used: dict[str, int] = {}
    if has_slots:
        used = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                f"SELECT device, COUNT(*) FROM {SLOTS_TABLE} GROUP BY device"
            )
        }
    if _has_table(connection, QA_SLOTS_TABLE):
        for device, count in connection.execute(
            f"""
            SELECT slots.device, COUNT(*) FROM {QA_SLOTS_TABLE} AS slots
            JOIN jobs ON jobs.id = slots.job_id
            WHERE jobs.state = 'running'
            GROUP BY slots.device
            """
        ):
            used[str(device)] = used.get(str(device), 0) + int(count)
    harnesses = sorted(HARNESSES)
    marks = ", ".join("?" for _ in harnesses)
    unplaced_filter = (
        f" AND id NOT IN (SELECT job_id FROM {SLOTS_TABLE})" if has_slots else ""
    )
    unplaced = int(
        connection.execute(
            f"SELECT COUNT(*) FROM jobs WHERE state = 'running' "
            f"AND recipient IN ({marks}){unplaced_filter}",
            harnesses,
        ).fetchone()[0]
    )
    if unplaced:
        host = orch_host(config)
        used[host] = used.get(host, 0) + unplaced
    return used
