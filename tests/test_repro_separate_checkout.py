from __future__ import annotations

from pathlib import Path

from lib import live_settings
from lib.device_monitor import DeviceMonitor
from lib.device_pool import DevicePool
from lib.queue import SQLiteJobQueue
from tests import test_remote_start as remote
from tests.test_repro_settings_adoption import enable, page
from tests.test_settings_apply import ENABLE_PC, SignalTransport, loaded


def separate_checkouts(tmp_path: Path):
    shared_machines = tmp_path / "poller" / "machines.yml"
    shared_machines.parent.mkdir()
    shared_machines.write_text(ENABLE_PC)
    waiter_machines = tmp_path / "waiter" / "machines.yml"
    waiter_machines.parent.mkdir()
    waiter_machines.write_text(ENABLE_PC)
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: 100.0)
    config = loaded(shared_machines)
    monitor = DeviceMonitor(DevicePool(queue, config, machines_path=shared_machines), config,
                            SignalTransport(), machines_path=shared_machines)
    monitor.reload()
    waiter = DevicePool(queue, loaded(waiter_machines), machines_path=waiter_machines)
    entry = enable(shared_machines, queue, tmp_path, "pc", ("waiter-claude-bot",))
    audit = tmp_path / "settings-audit.jsonl"
    audit.write_text(entry.line() + "\n")
    monitor.reload()
    remote.enqueue(queue, "claude", "1", timeout=5400)
    return queue, audit, waiter, waiter_machines, shared_machines, monitor


def test_separate_checkout_false_ack(tmp_path: Path) -> None:
    queue, audit, waiter, _, _, _ = separate_checkouts(tmp_path)
    before = page(queue, audit)
    claim = waiter.claim("claude")
    after = page(queue, audit)
    with queue.connect() as connection:
        live = live_settings.peek(connection)
    assert live is not None
    assert before == "pending"
    assert claim is None
    assert not live.switches["pc"].enabled
    assert "claude" not in live.seen
    assert after == "pending", "waiter does not use enable; UI must remain pending"


def test_applied_once_the_waiters_own_file_holds_the_enable(tmp_path: Path) -> None:
    queue, audit, waiter, waiter_machines, shared_machines, _ = separate_checkouts(tmp_path)
    assert waiter.claim("claude") is None
    waiter_machines.write_bytes(shared_machines.read_bytes())

    claim = waiter.claim("claude")

    assert claim is not None and claim[1].device == "pc"
    assert page(queue, audit) == "applied"
