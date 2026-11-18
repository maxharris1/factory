"""Repro B from the #818 round-3 review (#852): an unrestarted waiter that
loaded `enabled: false, slot_max: 6` must never place past the slot_max on
disk after a live slot_max 6 -> 1 and then an enable."""

from __future__ import annotations

from pathlib import Path

from lib import settings_apply
from lib.device_monitor import DeviceMonitor
from lib.device_pool import DevicePool
from lib.queue import SQLiteJobQueue
from lib.settings_apply import Applier, SettingsRequest
from tests import test_remote_start as remote
from tests.test_settings_apply import SignalTransport, loaded

BOX = """\
devices:
  - name: box
    local: true
    enabled: false
    slot_max: 6
    harnesses: {claude: claude-bot}
"""


def lowered_then_enabled(tmp_path: Path) -> tuple[DevicePool, DeviceMonitor, Path]:
    machines = tmp_path / "machines.yml"
    machines.write_text(BOX, encoding="utf-8")
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: 100.0)
    stale = DevicePool(queue, loaded(machines), machines_path=machines)
    for n in range(7):
        remote.enqueue(queue, "claude", str(n), timeout=5400)
    applier = Applier(machines, tmp_path, tmp_path / "deploy", "maxharris1", {}, (),
                      ("waiter-claude-bot",), settings_apply.claim_lock(queue.path))
    for field, expect, value in [("slot_max", 6, 1), ("enabled", False, True)]:
        entry = applier.apply(SettingsRequest(
            f"request-{field}-123456", "maxharris1", "2026-11-17T10:00:00Z",
            "box", field, expect, value))
        assert (entry.result, entry.pending) == ("pending", ("waiter-claude-bot",)), entry
    box = loaded(machines).device("box")
    assert (box.enabled, box.slot_max) == (True, 1), "the file on disk"
    monitor = DeviceMonitor(DevicePool(queue, loaded(machines), machines_path=machines),
                            loaded(machines), SignalTransport(), machines_path=machines)
    return stale, monitor, machines


def test_a_stale_waiter_places_up_to_the_slot_max_on_disk_once_the_poller_adopts(
    tmp_path: Path,
) -> None:
    stale, monitor, machines = lowered_then_enabled(tmp_path)

    monitor.reload()
    landed = sum(stale.claim("claude") is not None for _ in range(7))

    assert stale.used().get("box", 0) <= loaded(machines).device("box").slot_max, (
        "never past slot_max 1 on disk"
    )
    assert landed == 1, "the enabled box takes exactly one job"


def test_a_stale_waiter_places_nothing_before_the_poller_adopts_the_enable(
    tmp_path: Path,
) -> None:
    stale, _, _ = lowered_then_enabled(tmp_path)

    landed = sum(stale.claim("claude") is not None for _ in range(7))

    assert landed == 0, "the poller still holds box disabled"
    assert stale.used() == {}
