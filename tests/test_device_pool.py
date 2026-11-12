import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import MappingProxyType

import pytest

from lib import device_pool
from lib.device_pool import DevicePool, choose_device
from lib.machines_yml import Device, MachinesConfig
from lib.queue import JobRequest, QueueError, SQLiteJobQueue
from tests.test_dispatcher import GO, comment, setup, ticket


def device(
    name: str,
    slot_max: int | None,
    *harnesses: str,
    enabled: bool = True,
    placeholders: tuple[str, ...] = (),
) -> Device:
    return Device(
        name=name,
        slot_max=slot_max,
        harnesses=MappingProxyType({harness: f"{harness}-bot" for harness in harnesses}),
        tailscale_name=f"{name}.example.ts.net",
        enabled=enabled,
        workdir_root="/home/work",
        placeholders=placeholders,
    )


def make_pool(
    tmp_path: Path,
    *devices: Device,
    now: list[float] | None = None,
) -> tuple[DevicePool, SQLiteJobQueue]:
    current = now if now is not None else [100.0]
    queue = SQLiteJobQueue(tmp_path / "d.db", clock=lambda: current[0])
    return DevicePool(queue, MachinesConfig(devices=tuple(devices))), queue


def enqueue(queue: SQLiteJobQueue, recipient: str, key: str) -> None:
    queue.enqueue(
        JobRequest(
            repo="owner/repo",
            ticket_id=key,
            action="build",
            recipient=recipient,
            source_comment_id=f"go-{key}",
            timeout_seconds=5400,
        ),
        dedupe_key=key,
    )


def job_state(queue: SQLiteJobQueue, job_id: str) -> str:
    with queue.connect() as connection:
        return str(
            connection.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
        )


def test_harness_is_never_placed_on_a_device_where_it_is_not_eligible(
    tmp_path: Path,
) -> None:
    pool, queue = make_pool(
        tmp_path,
        device("codex-only", 4, "codex"),
        device("cursor-only", 4, "cursor"),
    )
    enqueue(queue, "claude", "1")

    assert pool.claim("claude") is None
    assert pool.used() == {}
    assert [job.state for job in queue.outstanding()] == ["queued"]

    enqueue(queue, "cursor", "2")
    claimed = pool.claim("cursor")
    assert claimed is not None
    assert claimed[1].device == "cursor-only"
    assert claimed[1].os_user == "cursor-bot"


def test_slot_max_is_never_exceeded_and_slots_are_generic(tmp_path: Path) -> None:
    pool, queue = make_pool(
        tmp_path,
        device("box", 2, "claude", "codex", "cursor"),
        device("pc", 1, "codex"),
    )
    for key, recipient in enumerate(("claude", "codex", "cursor", "codex", "claude")):
        enqueue(queue, recipient, str(key))

    first = pool.claim("claude")
    second = pool.claim("cursor")
    third = pool.claim("codex")
    assert first is not None and second is not None and third is not None
    assert [first[1].device, second[1].device, third[1].device] == ["box", "box", "pc"]
    assert pool.used() == {"box": 2, "pc": 1}

    assert pool.claim("claude") is None
    assert pool.claim("codex") is None
    assert pool.used() == {"box": 2, "pc": 1}
    assert sorted(job.state for job in queue.outstanding()) == [
        "queued",
        "queued",
        "running",
        "running",
        "running",
    ]

    assert pool.release(first[0].id, owner="claude") is True
    reused = pool.claim("codex")
    assert reused is not None
    assert reused[1].device == "box"
    assert reused[1].harness == "codex"


def test_placeholder_and_disabled_devices_refuse_to_schedule(tmp_path: Path) -> None:
    pool, queue = make_pool(
        tmp_path,
        device("pc", None, placeholders=("slot_max", "harnesses")),
        device("mac", 5, "claude", placeholders=("tailscale_name",)),
        device("spare", 5, "claude", enabled=False),
    )
    enqueue(queue, "claude", "1")

    assert pool.claim("claude") is None
    assert pool.used() == {}
    assert [job.state for job in queue.outstanding()] == ["queued"]


def test_choose_device_skips_full_ineligible_placeholder_and_disabled() -> None:
    config = MachinesConfig(
        devices=(
            device("placeholder", 9, "claude", placeholders=("workdir_root",)),
            device("off", 9, "claude", enabled=False),
            device("full", 1, "claude"),
            device("other", 9, "codex"),
            device("free", 1, "claude"),
        )
    )

    chosen = choose_device(config, "claude", {"full": 1})
    assert chosen is not None and chosen.name == "free"
    assert choose_device(config, "claude", {"full": 1, "free": 1}) is None


def test_concurrent_claims_never_take_the_same_last_slot(tmp_path: Path) -> None:
    devices = (device("box", 2, "claude", "codex"),)
    pool, queue = make_pool(tmp_path, *devices)
    for key in range(12):
        enqueue(queue, "claude" if key % 2 else "codex", str(key))
    pools = [
        DevicePool(SQLiteJobQueue(tmp_path / "d.db", clock=lambda: 100.0), pool.config)
        for _ in range(12)
    ]
    barrier = threading.Barrier(len(pools))

    def claim(index: int):
        barrier.wait()
        return pools[index].claim("claude" if index % 2 else "codex")

    with ThreadPoolExecutor(max_workers=len(pools)) as executor:
        results = list(executor.map(claim, range(len(pools))))

    placed = [result for result in results if result is not None]
    assert len(placed) == 2
    assert len({job.id for job, _ in placed}) == 2
    assert pool.used() == {"box": 2}
    assert sum(job.state == "running" for job in queue.outstanding()) == 2


def test_claim_holds_the_write_lock_from_counting_to_placing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = MachinesConfig(devices=(device("box", 1, "claude"),))
    pool, queue = make_pool(tmp_path, *config.devices)
    enqueue(queue, "claude", "1")
    enqueue(queue, "claude", "2")
    rival = DevicePool(SQLiteJobQueue(tmp_path / "d.db", clock=lambda: 100.0), config)
    rival_result: list[object] = []
    rival_thread = threading.Thread(
        target=lambda: rival_result.append(rival.claim("claude"))
    )
    original = device_pool._used

    def count_then_race(connection: sqlite3.Connection) -> dict[str, int]:
        counts = original(connection)
        if not rival_thread.is_alive() and not rival_result:
            rival_thread.start()
            rival_thread.join(timeout=0.5)
            assert rival_thread.is_alive(), "rival claim did not wait for the lock"
        return counts

    monkeypatch.setattr(device_pool, "_used", count_then_race)
    first = pool.claim("claude")
    rival_thread.join(timeout=10)

    assert first is not None
    assert rival_result == [None]
    assert pool.used() == {"box": 1}


def test_grok_jobs_never_hold_device_slots(tmp_path: Path) -> None:
    pool, _ = make_pool(tmp_path, device("box", 1, "claude"))

    with pytest.raises(QueueError, match="only harness jobs hold slots"):
        pool.claim("grok")


def test_release_checks_owner_and_is_idempotent(tmp_path: Path) -> None:
    pool, queue = make_pool(tmp_path, device("box", 1, "claude"))
    enqueue(queue, "claude", "1")
    claimed = pool.claim("claude")
    assert claimed is not None
    job, placement = claimed
    assert pool.placement(job.id) == placement

    with pytest.raises(QueueError, match="belongs to claude, not codex"):
        pool.release(job.id, owner="codex")
    assert pool.release(job.id, owner="claude") is True
    assert pool.release(job.id, owner="claude") is False
    assert pool.placement(job.id) is None


def test_finishing_a_job_does_not_free_its_slot(tmp_path: Path) -> None:
    pool, queue = make_pool(tmp_path, device("box", 1, "claude"))
    enqueue(queue, "claude", "1")
    enqueue(queue, "claude", "2")
    claimed = pool.claim("claude")
    assert claimed is not None

    queue.finish(claimed[0].id, owner="claude", handoff_comment_id="ready-1")

    assert pool.used() == {"box": 1}
    assert pool.claim("claude") is None


def test_device_power_loss_keeps_slot_held_and_goes_stuck_never_done(
    tmp_path: Path,
) -> None:
    now = [0.0]
    dispatcher, queue = setup(tmp_path, now)
    pool = DevicePool(
        queue,
        MachinesConfig(devices=(device("pc", 1, "claude"),)),
    )
    snapshot = ticket(
        comment("go-1", GO.format(state="building"), author="grok-login"),
        timeout=60,
    )
    dispatcher.process(snapshot)

    claimed = pool.claim("claude")
    assert claimed is not None
    job, placement = claimed
    assert job.request.timeout_seconds == 3600
    now[0] = 600
    queue.heartbeat(job.id, owner="claude")

    # The device loses power: no more heartbeats, no done, no release.
    now[0] = 600 + 3599
    dispatcher.reconcile_jobs()
    assert job_state(queue, job.id) == "running"
    assert pool.placement(job.id) == placement

    now[0] = 600 + 3600
    dispatcher.reconcile_jobs()
    assert job_state(queue, job.id) == "failed"
    assert pool.placement(job.id) == placement
    assert not dispatcher.is_stuck(snapshot)

    retry = next(item for item in queue.outstanding() if item.id != job.id)
    assert (retry.state, retry.request.attempt) == ("queued", 2)
    assert pool.claim("claude") is None

    now[0] = retry.created_at + 3600
    dispatcher.reconcile_jobs()

    assert dispatcher.is_stuck(snapshot)
    assert job_state(queue, job.id) == "failed"
    assert job_state(queue, retry.id) == "failed"
    with queue.connect() as connection:
        done = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE recipient = 'claude' AND state = 'done'"
        ).fetchone()[0]
    assert done == 0
    assert pool.placement(job.id) == placement
    assert pool.used() == {"pc": 1}
