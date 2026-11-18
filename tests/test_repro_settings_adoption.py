"""Repros from the #852 review (ac-3): the Settings page shows a device change
`pending` until every service uses it, and `applied` after that."""

from __future__ import annotations

from pathlib import Path

from lib import dashboard, live_settings, settings_apply
from lib.device_monitor import DeviceMonitor
from lib.device_pool import DevicePool
from lib.queue import SQLiteJobQueue
from tests import test_feeder as intake
from tests import test_remote_start as remote
from tests.test_settings_apply import ENABLE_PC, SignalTransport, loaded


def enable(machines: Path, queue: SQLiteJobQueue, state: Path, device: str,
           pending: tuple[str, ...]) -> settings_apply.AuditEntry:
    return settings_apply.Applier(
        machines, state, state / "deploy", "maxharris1", {}, (), pending,
        settings_apply.claim_lock(queue.path), run=lambda *_: 0,
    ).apply(settings_apply.SettingsRequest(
        "enable-request-abcdefghij", "maxharris1", "2026-11-17T10:00:00Z",
        device, "enabled", False, True,
    ))


def page(queue: SQLiteJobQueue, audit: Path) -> str:
    with queue.connect() as connection:
        (row,) = dashboard._settings_results(audit, live_settings.peek(connection))
    return row["result"]


def disabled_box(tmp_path: Path, box: int):
    feeder, queue, github, _ = intake.make_feeder(tmp_path, box=box)
    machines = tmp_path / "machines.yml"
    machines.write_text(machines.read_text().replace(
        "    local: true", "    local: true\n    enabled: false"))
    feeder.machines = config = loaded(machines)
    monitor = DeviceMonitor(DevicePool(queue, config, machines_path=machines), config,
                            SignalTransport(), machines_path=machines)
    monitor.reload()
    github.issues["owner/app"] = [intake.issue(85)]
    assert not feeder.feed_once().fed, "the box is disabled"
    return feeder, queue, machines, config, monitor


def test_applied_after_all_consumers_use_enable(tmp_path: Path) -> None:
    feeder, queue, machines, config, monitor = disabled_box(tmp_path, box=3)
    waiter = DevicePool(queue, config, machines_path=machines)
    entry = enable(machines, queue, tmp_path, "box", ("waiter-claude-bot",))
    audit = tmp_path / "settings-audit.jsonl"
    audit.write_text(entry.line() + "\n")
    monitor.reload()
    fed = len(feeder.feed_once().fed)
    remote.enqueue(queue, "claude", "1", timeout=5400)
    before = page(queue, audit)

    claimed = waiter.claim("claude")

    with queue.connect() as connection:
        live = live_settings.peek(connection)
        (row,) = dashboard._settings_results(audit, live)
    assert monitor.config.device("box").enabled
    assert fed == 1
    assert before == "pending", "adopted, but the waiter has not claimed by the row yet"
    assert claimed is not None and claimed[1].device == "box"
    assert live.is_applied(entry.version)
    assert row["result"] == "applied", "every settings consumer now uses the enable"


def test_applied_with_old_waiter(tmp_path: Path) -> None:
    """A waiter that places by its startup config and never reports `seen`
    (what a pre-#852 waiter does) keeps the enable `pending`, while a waiter on
    current code would already place by it."""
    machines = tmp_path / "machines.yml"
    machines.write_text(ENABLE_PC)
    config = loaded(machines)
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: 100.0)
    old_waiter = DevicePool(queue, config)
    monitor = DeviceMonitor(DevicePool(queue, config, machines_path=machines), config,
                            SignalTransport(), machines_path=machines)
    monitor.reload()
    remote.enqueue(queue, "claude", "1", timeout=5400)
    assert old_waiter.claim("claude") is None
    entry = enable(machines, queue, tmp_path, "pc", ("waiter-claude-bot",))
    monitor.reload()
    audit = tmp_path / "settings-audit.jsonl"
    audit.write_text(entry.line() + "\n")

    result = page(queue, audit)
    stale_claim = old_waiter.claim("claude")
    fresh_claim = DevicePool(queue, loaded(machines), machines_path=machines).claim("claude")

    assert stale_claim is None and fresh_claim is not None
    assert result == "pending", "the old waiter has not adopted"


def test_applied_with_current_feeder(tmp_path: Path) -> None:
    """The feeder sizes intake from the shared row (lib/feeder.py `_room`), so it
    is never stale once the poller adopts, and a change pending only on the
    poller is applied on adoption. An earlier round expected `pending` here on
    the premise of a stale feeder, which no longer holds."""
    feeder, queue, machines, _, monitor = disabled_box(tmp_path, box=1)
    entry = enable(machines, queue, tmp_path, "box", ("poller",))
    monitor.reload()

    report = feeder.feed_once()

    audit = tmp_path / "settings-audit.jsonl"
    audit.write_text(entry.line() + "\n")
    assert len(report.fed) == 1
    assert page(queue, audit) == "applied"
