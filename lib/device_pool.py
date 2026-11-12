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

Finishing, failing or timing out a job does not free its slot. A device
that loses power mid-job keeps the slot held while T6 times the job out
(never done) and, on the second failure, marks the ticket stuck. Apart
from a requeue, only `DevicePool.release` frees a slot; F2-02 calls it
once the worker is confirmed gone.

This module starts nothing and contacts no device.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Mapping
from dataclasses import dataclass

from lib.machines_yml import Device, MachinesConfig, shared_tailscale_names
from lib.queue import SLOTS_TABLE, Job, QueueError, SQLiteJobQueue, _job
from lib.schema import HARNESSES


@dataclass(frozen=True)
class Placement:
    job_id: str
    device: str
    harness: str
    os_user: str
    placed_at: float


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
        with self.queue.connect() as connection:
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

    def claim(
        self,
        harness: str,
        *,
        excluded_job_ids: Collection[str] = (),
    ) -> tuple[Job, Placement] | None:
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
            connection.execute(
                f"""
                INSERT INTO {SLOTS_TABLE} (job_id, device, harness, os_user, placed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (row["id"], device.name, harness, os_user, now),
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
        return Placement(
            job_id=str(row["job_id"]),
            device=str(row["device"]),
            harness=str(row["harness"]),
            os_user=str(row["os_user"]),
            placed_at=float(row["placed_at"]),
        )

    def used(self) -> dict[str, int]:
        with self.queue.connect() as connection:
            return _used(connection)


def _used(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        str(row[0]): int(row[1])
        for row in connection.execute(
            f"SELECT device, COUNT(*) FROM {SLOTS_TABLE} GROUP BY device"
        )
    }
