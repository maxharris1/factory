"""Claim a job the way `orch wait` does since F2-02: through the device pool."""

from __future__ import annotations

from pathlib import Path

from lib.device_pool import DevicePool
from lib.machines_yml import load_machines_yml
from lib.queue import Job, SQLiteJobQueue

ROOT = Path(__file__).resolve().parents[1]


def pool_claim(queue: SQLiteJobQueue, recipient: str, machines: Path | None = None) -> Job:
    """Claim the oldest queued job for recipient with a slot from machines.yml."""
    config, _ = load_machines_yml(machines or ROOT / "machines.yml", allow_placeholders=True)
    assert config is not None
    claimed = DevicePool(queue, config).claim(recipient)
    assert claimed is not None
    return claimed[0]
