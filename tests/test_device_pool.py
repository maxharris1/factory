import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import MappingProxyType

import pytest

from lib import device_pool
from lib.device_pool import DevicePool, choose_device
from lib.machines_yml import Device, MachinesConfig, load_machines_yml
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


def test_shipped_inventory_never_places_on_the_disabled_pc_or_mac(
    tmp_path: Path,
) -> None:
    """The PC and Mac stay enabled: false until F2-02. With the shipped
    machines.yml, every claim lands on the box and, once the box is full,
    work stays queued instead of spilling onto a remote device."""
    root = Path(__file__).resolve().parents[1]
    config, _ = load_machines_yml(root / "machines.yml", allow_placeholders=True)
    assert config is not None
    queue = SQLiteJobQueue(tmp_path / "d.db", clock=lambda: 100.0)
    pool = DevicePool(queue, config)
    for index in range(4):
        for harness in ("claude", "codex", "cursor"):
            enqueue(queue, harness, f"{harness}-{index}")

    placements = []
    for harness in ("claude", "codex", "cursor", "claude", "codex", "cursor"):
        claimed = pool.claim(harness)
        if claimed is not None:
            placements.append(claimed[1])

    box = config.device("box")
    assert box is not None and box.slot_max is not None
    assert len(placements) == box.slot_max
    assert {placement.device for placement in placements} == {"box"}
    assert pool.used() == {"box": box.slot_max}


def slot_rows(queue: SQLiteJobQueue) -> list[tuple[str, str]]:
    with queue.connect() as connection:
        return [
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                f"SELECT job_id, device FROM {device_pool.SLOTS_TABLE} ORDER BY job_id"
            )
        ]


def test_requeueing_a_pool_claimed_job_frees_its_slot_and_claim_still_works(
    tmp_path: Path,
) -> None:
    """R1: queue.release (running -> queued) on a pool-claimed job used to
    keep its device_slots row, so the next claim of that same job hit the
    job_id PRIMARY KEY and every later claim for the harness raised."""
    pool, queue = make_pool(tmp_path, device("box", 1, "claude"))
    enqueue(queue, "claude", "1")
    claimed = pool.claim("claude")
    assert claimed is not None
    job, _ = claimed

    requeued = queue.release(job.id, owner="claude")

    assert requeued.state == "queued"
    assert pool.placement(job.id) is None
    assert pool.used() == {}
    again = pool.claim("claude")
    assert again is not None
    assert again[0].id == job.id
    assert again[1].device == "box"
    assert pool.used() == {"box": 1}
    assert slot_rows(queue) == [(job.id, "box")]


def test_requeue_without_a_device_pool_table_still_works(tmp_path: Path) -> None:
    queue = SQLiteJobQueue(tmp_path / "d.db", clock=lambda: 100.0)
    enqueue(queue, "grok", "1")
    job = queue.claim("grok")
    assert job is not None

    assert queue.release(job.id, owner="grok").state == "queued"


def test_claim_never_raises_on_a_stale_slot_row_for_a_queued_job(
    tmp_path: Path,
) -> None:
    """A queued job never holds a slot. If a slot row survives an undone
    claim (a database written before requeue freed slots), claim drops it
    instead of hitting the job_id PRIMARY KEY, and the stale row does not
    count against slot_max."""
    pool, queue = make_pool(tmp_path, device("box", 1, "claude"))
    enqueue(queue, "claude", "1")
    (job,) = queue.outstanding()
    with queue.connect() as connection:
        connection.execute(
            f"INSERT INTO {device_pool.SLOTS_TABLE} "
            "(job_id, device, harness, os_user, placed_at) "
            "VALUES (?, 'box', 'claude', 'claude-bot', 1)",
            (job.id,),
        )

    claimed = pool.claim("claude")

    assert claimed is not None
    assert claimed[0].id == job.id
    assert slot_rows(queue) == [(job.id, "box")]
    assert pool.placement(job.id).placed_at == 100.0


def test_claim_rolls_back_the_job_when_the_slot_insert_fails(tmp_path: Path) -> None:
    pool, queue = make_pool(tmp_path, device("box", 2, "claude"))
    enqueue(queue, "claude", "1")
    with queue.connect() as connection:
        connection.execute(
            f"""
            CREATE TRIGGER fail_slot_insert BEFORE INSERT ON {device_pool.SLOTS_TABLE}
            BEGIN SELECT RAISE(ABORT, 'injected slot insert failure'); END
            """
        )

    with pytest.raises(sqlite3.DatabaseError, match="injected slot insert failure"):
        pool.claim("claude")

    assert [job.state for job in queue.outstanding()] == ["queued"]
    assert slot_rows(queue) == []
    assert pool.used() == {}
    with queue.connect() as connection:
        connection.execute("DROP TRIGGER fail_slot_insert")
    assert pool.claim("claude") is not None
    assert pool.used() == {"box": 1}


def test_reopening_the_database_keeps_held_slots(tmp_path: Path) -> None:
    pool, queue = make_pool(tmp_path, device("box", 2, "claude"))
    for key in ("1", "2", "3"):
        enqueue(queue, "claude", key)
    first = pool.claim("claude")
    second = pool.claim("claude")
    assert first is not None and second is not None
    queue.finish(first[0].id, owner="claude", handoff_comment_id="c-1")

    reopened = DevicePool(
        SQLiteJobQueue(tmp_path / "d.db", clock=lambda: 200.0), pool.config
    )

    assert reopened.used() == {"box": 2}
    assert reopened.placement(first[0].id) == first[1]
    assert reopened.placement(second[0].id) == second[1]
    assert reopened.claim("claude") is None


def test_a_released_slot_takes_the_next_job(tmp_path: Path) -> None:
    now = [100.0]
    pool, queue = make_pool(tmp_path, device("box", 1, "claude"), now=now)
    enqueue(queue, "claude", "1")
    enqueue(queue, "claude", "2")
    claimed = pool.claim("claude")
    assert claimed is not None
    timed_out, _ = claimed
    now[0] = 100.0 + 5400
    (running,) = [job for job in queue.outstanding() if job.state == "running"]
    assert queue.mark_timed_out(running)
    assert pool.claim("claude") is None

    assert pool.release(timed_out.id, owner="claude") is True
    reused = pool.claim("claude")

    assert reused is not None
    assert reused[0].id != timed_out.id
    assert reused[1].device == "box"
    assert pool.used() == {"box": 1}


def test_pool_refuses_two_devices_on_one_tailscale_host(tmp_path: Path) -> None:
    """A1: slots are counted per device name, so two entries for one host
    would double its slot_max. The pool refuses such a config even when it
    is built without load_machines_yml."""
    queue = SQLiteJobQueue(tmp_path / "d.db", clock=lambda: 100.0)
    a = device("pc-a", 1, "claude")
    b = Device(
        name="pc-b",
        slot_max=1,
        harnesses=MappingProxyType({"claude": "claude-bot"}),
        tailscale_name="PC-A.example.ts.net",
        workdir_root="/home/work",
    )

    with pytest.raises(QueueError, match="pc-a, pc-b"):
        DevicePool(queue, MachinesConfig(devices=(a, b)))
