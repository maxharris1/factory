"""Device pool scheduling (docs/SPEC.md F2-01).

A slot is one row in `device_slots`, in the dispatcher database next to
`jobs`. `claim` moves the oldest queued job for a harness to running and
holds a slot for it on the first device, in machines.yml order, that is
schedulable, lists that harness, and has a free slot. Both writes happen
in one BEGIN IMMEDIATE transaction, so two claims cannot take the same
last slot. Slots are generic: any free slot on an eligible device takes
any harness.

A queued job never holds a slot. Requeueing a claimed job
(SQLiteJobQueue.release) frees its slot in the same transaction, and
claim drops any slot row left on a queued job before counting, so a
requeued job can always be claimed again.

Finishing, failing or timing out a job does not free its slot. Apart
from a requeue, only `DevicePool.release` frees a slot, and F2-02 calls
it only once the worker is confirmed gone: `orch job run` after its
command exits (a remote worker is checked over SSH first), or the
dispatcher's `DeviceMonitor` for a finished job whose runner died.

F2-02 records on the slot row who runs the job: `runner_pid` and
`runner_start` (the box's `orch job run`), `worker_pid` and
`worker_start` (its local command), `remote_started` (a remote start
was attempted, so the device must confirm the worker gone) and
`worker_tag` (the random tag every process of that remote start carries,
lib/remote.py, so a check never trusts a stored process number). #80 adds
`autostart_pid` and `autostart_start`: the autostart waiter that claimed
the job, if one did. #96 adds `run_token_hash`: the hash of the secret
token the job's current `orch job run` holds (never the token itself). The
`device_health` table holds each remote device's last successful SSH
contact and when it began failing, for the 60-minute offline check.

This module starts nothing and contacts no device.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass

from lib.machines_yml import Device, MachinesConfig, shared_tailscale_names
from lib.queue import SLOTS_TABLE, Job, QueueError, SQLiteJobQueue, _job
from lib.remote import new_tag
from lib.schema import HARNESSES

HEALTH_TABLE = "device_health"
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

    @property
    def autostarted(self) -> bool:
        return self.autostart_pid is not None


@dataclass(frozen=True)
class HeldSlot:
    """A held slot and its job's state (None when the job row is gone)."""

    placement: Placement
    job_state: str | None


def choose_device(
    config: MachinesConfig,
    harness: str,
    used: Mapping[str, int],
) -> Device | None:
    """First schedulable device listing harness with a free slot, or None."""
    for device in config.devices:
        if not device.eligible(harness):
            continue
        assert device.slot_max is not None
        if used.get(device.name, 0) < device.slot_max:
            return device
    return None


class DevicePool:
    def __init__(self, queue: SQLiteJobQueue, config: MachinesConfig):
        shared = shared_tailscale_names(config.devices)
        if shared:
            raise QueueError(
                f"devices {', '.join(shared[0])} share one tailscale_name; "
                "one host has one slot_max"
            )
        self.queue = queue
        self.config = config
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

    def claim(
        self,
        harness: str,
        *,
        excluded_job_ids: Collection[str] = (),
        autostart: tuple[int, str | None] | None = None,
    ) -> tuple[Job, Placement] | None:
        """Claim the oldest queued job for harness onto a free slot.

        `autostart` is the claiming autostart waiter's (pid, start); it is
        written on the slot in the same transaction as the claim.
        """
        if harness not in HARNESSES:
            raise QueueError(f"{harness!r} is not a harness; only harness jobs hold slots")
        excluded = frozenset(excluded_job_ids)
        now = self.queue.now()
        with self.queue.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                f"""
                DELETE FROM {SLOTS_TABLE}
                WHERE job_id IN (SELECT id FROM jobs WHERE state = 'queued')
                """
            )
            device = choose_device(self.config, harness, _used(connection))
            if device is None:
                connection.commit()
                return None
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE state = 'queued' AND recipient = ?
                ORDER BY created_at, id
                """,
                (harness,),
            ).fetchall()
            row = next(
                (item for item in rows if str(item["id"]) not in excluded),
                None,
            )
            if row is None:
                connection.commit()
                return None
            changed = connection.execute(
                """
                UPDATE jobs
                SET state = 'running', last_activity_at = ?
                WHERE id = ? AND state = 'queued'
                """,
                (now, row["id"]),
            ).rowcount
            if changed != 1:
                raise QueueError(f"job {row['id']} changed during claim")
            os_user = device.harnesses[harness]
            claimer_pid, claimer_start = autostart if autostart is not None else (None, None)
            connection.execute(
                f"""
                INSERT INTO {SLOTS_TABLE} (
                    job_id, device, harness, os_user, placed_at,
                    autostart_pid, autostart_start
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (row["id"], device.name, harness, os_user, now, claimer_pid, claimer_start),
            )
            updated = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (row["id"],),
            ).fetchone()
            connection.commit()
        assert updated is not None
        return _job(updated), Placement(
            job_id=str(row["id"]),
            device=device.name,
            harness=harness,
            os_user=os_user,
            placed_at=now,
            autostart_pid=claimer_pid,
            autostart_start=claimer_start,
        )

    def release(self, job_id: str, *, owner: str) -> bool:
        """Free a job's slot once its worker is confirmed gone. False if already free."""
        if owner not in HARNESSES:
            raise QueueError(f"unknown owner {owner!r}")
        with self.queue.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
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
    ) -> None:
        """Record the box process (`orch job run`) that runs a placed job.

        Refused while another recorded runner is still alive, so two runners
        never share one slot (the later one could release it under the other).
        `token_hash` (#96) is written in the same update, replacing any
        earlier run's, so only this run's token can finish the job.
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

        self._update_slot(
            job_id,
            owner,
            "runner_pid = ?, runner_start = ?, run_token_hash = ?",
            (pid, start, token_hash),
            guard=guard,
        )

    def attach_worker(
        self,
        job_id: str,
        *,
        owner: str,
        pid: int | None = None,
        start: str | None = None,
        remote: bool = False,
        tag: str | None = None,
    ) -> str | None:
        """Record the worker: a local command's pid, or that a remote start began.

        A remote start is recorded, with its worker tag (a fresh one unless
        given), before ssh is spawned, so a runner that dies mid-spawn still
        leaves the device to confirm the worker gone. Returns the tag.
        """
        if remote:
            worker_tag = tag if tag is not None else new_tag()
            self._update_slot(
                job_id, owner, "remote_started = 1, worker_tag = ?", (worker_tag,)
            )
            return worker_tag
        else:
            self._update_slot(
                job_id,
                owner,
                "worker_pid = ?, worker_start = ?",
                (pid, start),
            )
            return None

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

    def _update_slot(
        self,
        job_id: str,
        owner: str,
        assignments: str,
        values: tuple[object, ...],
        *,
        guard: Callable[[sqlite3.Row], None] | None = None,
    ) -> None:
        if owner not in HARNESSES:
            raise QueueError(f"unknown owner {owner!r}")
        with self.queue.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
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

    def used(self) -> dict[str, int]:
        with self.queue.connect() as connection:
            return _used(connection)


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
    )


def _int(value: object) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _text(value: object) -> str | None:
    return str(value) if value is not None else None


def _float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _used(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        str(row[0]): int(row[1])
        for row in connection.execute(
            f"SELECT device, COUNT(*) FROM {SLOTS_TABLE} GROUP BY device"
        )
    }
