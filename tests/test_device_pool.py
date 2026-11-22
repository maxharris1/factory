import io
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import MappingProxyType

import pytest

from lib import device_pool, device_sync, live_settings
from lib.device_pool import DevicePool, Orphan, choose_device, placed_account
from lib.job_state import read_placements
from lib.machines_yml import Device, MachinesConfig, load_machines_yml
from lib.queue import JobRequest, QueueError, SQLiteJobQueue
from lib.wait import wait_for_job
from tests.pool_helpers import poller_adopts, write_target_machines
from tests.test_dispatcher import GO, comment, setup, ticket


def device(
    name: str,
    slot_max: int | None,
    *harnesses: str,
    enabled: bool = True,
    drain: bool = False,
    placeholders: tuple[str, ...] = (),
) -> Device:
    return Device(
        name=name,
        slot_max=slot_max,
        harnesses=MappingProxyType({harness: f"{harness}-bot" for harness in harnesses}),
        tailscale_name=f"{name}.example.ts.net",
        enabled=enabled,
        drain=drain,
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

    # The slot is released once the job ended and its worker is gone. (A
    # running job with no slot still counts on the box: issue #90.)
    queue.finish(first[0].id, owner="claude", handoff_comment_id="1")
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
            device("free", 2, "claude"),
        )
    )

    chosen = choose_device(config, "claude", {"full": 1})
    assert chosen is not None and chosen.name == "free"
    chosen = choose_device(config, "claude", {"full": 1, "free": 1})
    assert chosen is not None and chosen.name == "free"
    assert choose_device(config, "claude", {"full": 1, "free": 2}) is None


def test_choose_device_and_free_slots_skip_a_draining_device() -> None:
    config = MachinesConfig(
        devices=(
            device("pc", 4, "claude"),
            device("box", 6, "claude", drain=True),
        )
    )

    chosen = choose_device(config, "claude", {"pc": 3})
    assert chosen is not None and chosen.name == "pc", "the emptier draining box is skipped"
    assert choose_device(config, "claude", {"pc": 4}) is None
    assert device_pool.free_slots(config, "claude", {"pc": 3, "box": 2}) == 1


def test_a_claim_with_every_device_draining_leaves_the_job_queued(tmp_path: Path) -> None:
    pool, queue = make_pool(
        tmp_path,
        device("box", 6, "claude", drain=True),
        device("pc", 4, "claude", "codex", drain=True),
    )
    enqueue(queue, "claude", "1")

    assert pool.claim("claude") is None
    assert pool.used() == {}
    assert [job.state for job in queue.outstanding()] == ["queued"]


BOX_FILE = """\
devices:
  - name: box
    local: true
    enabled: true
    drain: false
    slot_max: 2
    harnesses: {claude: claude-bot}
"""


def file_pool(tmp_path: Path) -> tuple[DevicePool, SQLiteJobQueue, Path]:
    machines = tmp_path / "machines.yml"
    machines.write_text(BOX_FILE, encoding="utf-8")
    config, findings = load_machines_yml(machines)
    assert config is not None, findings
    queue = SQLiteJobQueue(tmp_path / "d.db", clock=lambda: 100.0)
    enqueue(queue, "claude", "1")
    poller_adopts(queue, machines)
    return DevicePool(queue, config, machines_path=machines), queue, machines


@pytest.mark.parametrize("edit", [("drain: false", "drain: true"), ("enabled: true", "enabled: false")])
def test_a_claim_reads_drain_and_enabled_from_the_file_as_it_is_now(
    tmp_path: Path, edit: tuple[str, str]
) -> None:
    pool, queue, machines = file_pool(tmp_path)
    machines.write_text(BOX_FILE.replace(*edit), encoding="utf-8")

    assert pool.claim("claude") is None, "the file changed after the pool loaded it"
    assert [job.state for job in queue.outstanding()] == ["queued"]


def test_a_claim_places_while_the_file_is_unchanged(tmp_path: Path) -> None:
    pool, _, _ = file_pool(tmp_path)

    claimed = pool.claim("claude")

    assert claimed is not None and claimed[1].device == "box"


def test_an_invalid_file_leaves_the_row_in_force(tmp_path: Path) -> None:
    pool, _, machines = file_pool(tmp_path)
    machines.write_text(BOX_FILE.replace("drain: false", "drain: maybe"), encoding="utf-8")

    claimed = pool.claim("claude")

    assert claimed is not None and claimed[1].device == "box"


def test_nothing_is_placed_before_the_poller_first_adopts(tmp_path: Path) -> None:
    machines = tmp_path / "machines.yml"
    machines.write_text(BOX_FILE, encoding="utf-8")
    queue = SQLiteJobQueue(tmp_path / "d.db", clock=lambda: 100.0)
    enqueue(queue, "claude", "1")
    pool = restarted(queue, machines)

    assert pool.claim("claude") is None
    poller_adopts(queue, machines)
    claimed = pool.claim("claude")
    assert claimed is not None and claimed[1].device == "box"


def test_an_invalid_file_with_no_row_places_nothing(tmp_path: Path) -> None:
    machines = tmp_path / "machines.yml"
    machines.write_text(BOX_FILE, encoding="utf-8")
    queue = SQLiteJobQueue(tmp_path / "d.db", clock=lambda: 100.0)
    enqueue(queue, "claude", "1")
    pool = restarted(queue, machines)
    machines.write_text(BOX_FILE.replace("drain: false", "drain: maybe"), encoding="utf-8")

    assert pool.claim("claude") is None
    assert [job.state for job in queue.outstanding()] == ["queued"]


def adopted_and_seen(queue: SQLiteJobQueue) -> tuple[int, dict[str, int]]:
    with queue.connect() as connection:
        live = live_settings.peek(connection)
    assert live is not None
    return live.adopted, dict(live.seen)


def test_every_claim_reports_the_adopted_version_it_placed_by_job_or_none(
    tmp_path: Path,
) -> None:
    pool, queue, machines = file_pool(tmp_path)
    adopted, seen = adopted_and_seen(queue)
    assert seen == {}

    assert pool.claim("codex") is None, "box takes no codex job"
    claimed = pool.claim("claude")

    assert claimed is not None
    assert adopted_and_seen(queue) == (adopted, {"claude": adopted, "codex": adopted})
    machines.write_text(BOX_FILE.replace("slot_max: 2", "slot_max: 3"), encoding="utf-8")
    os.utime(machines, ns=(10**18, 10**18))
    poller_adopts(queue, machines)
    assert pool.claim("claude") is None, "no queued job left"
    newer, seen = adopted_and_seen(queue)
    assert newer > adopted and seen == {"claude": newer, "codex": adopted}


def test_a_pool_that_does_not_place_by_the_row_reports_nothing(tmp_path: Path) -> None:
    _, queue, machines = file_pool(tmp_path)
    config, _ = load_machines_yml(machines)
    assert config is not None

    claimed = DevicePool(queue, config).claim("claude")

    assert claimed is not None
    assert adopted_and_seen(queue)[1] == {}


def edited_after_load(
    tmp_path: Path, loaded: str, edited: str, jobs: int
) -> tuple[DevicePool, SQLiteJobQueue, Path]:
    machines = tmp_path / "machines.yml"
    machines.write_text(loaded, encoding="utf-8")
    config, findings = load_machines_yml(machines)
    assert config is not None, findings
    queue = SQLiteJobQueue(tmp_path / "d.db", clock=lambda: 100.0)
    for n in range(jobs):
        enqueue(queue, "claude", str(n))
    poller_adopts(queue, machines)
    machines.write_text(edited, encoding="utf-8")
    os.utime(machines, ns=(10**18, 10**18))
    return DevicePool(queue, config, machines_path=machines), queue, machines


def restarted(queue: SQLiteJobQueue, machines: Path) -> DevicePool:
    config, findings = load_machines_yml(machines)
    assert config is not None, findings
    return DevicePool(queue, config, machines_path=machines)


def placements(pool: DevicePool, attempts: int) -> int:
    return sum(pool.claim("claude") is not None for _ in range(attempts))


@pytest.mark.parametrize("edit", [("enabled: true", "enabled: false"), ("drain: false", "drain: true")])
def test_a_file_that_enables_or_undrains_waits_for_the_pollers_adopt(
    tmp_path: Path, edit: tuple[str, str]
) -> None:
    loaded = BOX_FILE.replace(*edit)
    pool, queue, machines = edited_after_load(tmp_path, loaded, BOX_FILE, jobs=1)

    assert pool.claim("claude") is None, "the live file never widens placement"
    assert [job.state for job in queue.outstanding()] == ["queued"]
    assert restarted(queue, machines).claim("claude") is None, "nor does a restart alone"
    poller_adopts(queue, machines)
    claimed = pool.claim("claude")
    assert claimed is not None and claimed[1].device == "box", (
        "the same unrestarted pool places once the poller adopts the file"
    )


def test_a_lower_slot_max_in_the_file_caps_placement_live(tmp_path: Path) -> None:
    pool, _, _ = edited_after_load(
        tmp_path, BOX_FILE.replace("slot_max: 2", "slot_max: 3"),
        BOX_FILE.replace("slot_max: 2", "slot_max: 1"), jobs=3,
    )

    assert placements(pool, 3) == 1
    assert pool.used() == {"box": 1}


def test_a_higher_slot_max_in_the_file_waits_for_the_pollers_adopt(tmp_path: Path) -> None:
    pool, queue, machines = edited_after_load(
        tmp_path, BOX_FILE.replace("slot_max: 2", "slot_max: 1"),
        BOX_FILE.replace("slot_max: 2", "slot_max: 3"), jobs=3,
    )

    assert placements(pool, 3) == 1, "the loaded slot_max still caps the box"
    assert placements(restarted(queue, machines), 3) == 0, "a restart alone does not raise it"
    poller_adopts(queue, machines)
    assert placements(pool, 3) == 2, "the same unrestarted pool places up to the adopted 3"
    assert pool.used() == {"box": 3}


def test_pools_loaded_from_different_files_never_pass_the_slot_max_on_disk_together(
    tmp_path: Path,
) -> None:
    machines = tmp_path / "machines.yml"
    queue = SQLiteJobQueue(tmp_path / "d.db", clock=lambda: 100.0)
    for n in range(8):
        enqueue(queue, "claude", str(n))
    pools = []
    for at, slots in enumerate((6, 4)):
        machines.write_text(BOX_FILE.replace("slot_max: 2", f"slot_max: {slots}"), encoding="utf-8")
        os.utime(machines, ns=(10**18 + at, 10**18 + at))
        pools.append(restarted(queue, machines))
        if at == 0:
            poller_adopts(queue, machines)
    machines.write_text(BOX_FILE, encoding="utf-8")
    os.utime(machines, ns=(10**18 + 2, 10**18 + 2))

    placed = sum(pool.claim("claude") is not None for _ in range(4) for pool in pools)

    assert placed == 2, "the file's slot_max of 2 caps both pools together"
    assert pools[0].used() == {"box": 2}


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

    def count_then_race(connection: sqlite3.Connection, config=None) -> dict[str, int]:
        counts = original(connection, config)
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


def test_device_power_loss_keeps_slot_held_and_never_done(
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
    # Started the way `orch job run` starts a remote job: a runner and a
    # remote start recorded (#91: a placed job with neither never started).
    pool.attach_runner(job.id, owner="claude", pid=4242, start=None, alive=lambda *_: False)
    pool.attach_worker(job.id, owner="claude", remote=True)
    placement = pool.placement(job.id)
    now[0] = 600
    queue.heartbeat(job.id, owner="claude")

    # The device loses power: no more heartbeats, no done, no release.
    now[0] = 600 + 3599
    dispatcher.reconcile_jobs()
    assert job_state(queue, job.id) == "running"
    assert pool.placement(job.id) == placement

    # Past its timeout the started remote job is held, never failed beside
    # a worker that may still run (#87 r3 F2.1); the DeviceMonitor's offline
    # hour makes it stuck (tests/test_timeout_remote_worker.py).
    now[0] = 600 + 3600
    dispatcher.reconcile_jobs()
    assert job_state(queue, job.id) == "running"
    assert [item.id for item in queue.outstanding()] == [job.id]  # no attempt 2
    now[0] = 600 + 3 * 3600
    dispatcher.reconcile_jobs()
    assert job_state(queue, job.id) == "running"
    with queue.connect() as connection:
        done = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE recipient = 'claude' AND state = 'done'"
        ).fetchone()[0]
        held = connection.execute(
            "SELECT COUNT(*) FROM job_events WHERE job_id = ? AND event = 'timeout-held'",
            (job.id,),
        ).fetchone()[0]
    assert done == 0
    assert held == 1
    assert pool.placement(job.id) == placement
    assert pool.used() == {"pc": 1}


def test_shipped_inventory_fills_pc_and_box_and_never_places_on_the_mac(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    config, _ = load_machines_yml(root / "machines.yml")
    assert config is not None
    queue = SQLiteJobQueue(tmp_path / "d.db", clock=lambda: 100.0)
    pool = DevicePool(queue, config)
    harnesses = ("claude", "codex", "cursor")
    for index in range(6):
        for harness in harnesses:
            enqueue(queue, harness, f"{harness}-{index}")

    placements = []
    for index in range(17):
        claimed = pool.claim(harnesses[index % 3])
        if claimed is not None:
            placements.append(claimed[1])

    assert len(placements) == 16
    assert pool.used() == {"pc": 10, "box": 6}
    assert {placement.os_user for placement in placements} == {
        "claude-bot",
        "codex-bot",
        "cursor-bot",
    }
    assert [job.state for job in queue.outstanding()].count("queued") == 2


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


def test_the_timeout_sweep_never_fails_an_autostart_claim_that_has_not_started(
    tmp_path: Path,
) -> None:
    now = [100.0]
    pool, queue = make_pool(tmp_path, device("box", 2, "claude"), now=now)
    enqueue(queue, "claude", "1")
    enqueue(queue, "claude", "2")
    assert pool.claim("claude", autostart=(4242, "start")) is not None
    assert pool.claim("claude") is not None
    now[0] = 100.0 + 5400
    autostart, plain = queue.outstanding()

    assert queue.mark_timed_out(plain) is True, "a bot's own claim keeps its timeout"
    assert queue.mark_timed_out(autostart) is False
    (held,) = queue.outstanding()
    assert held.state == "running"

    now[0] += 1
    queue.heartbeat(held.id, owner="claude")
    now[0] += 5400
    (started,) = queue.outstanding()
    assert queue.mark_timed_out(started) is True, "once it heartbeats, its timeout applies"


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


def test_forget_worker_drops_only_the_worker_that_was_confirmed_gone(tmp_path: Path) -> None:
    pool, queue = make_pool(tmp_path, device("pc", 1, "codex"))
    enqueue(queue, "codex", "1")
    job, _ = pool.claim("codex")
    pool.attach_worker(job.id, owner="codex", remote=True, tag="old")
    pool.attach_runner(job.id, owner="codex", pid=11, start=None, alive=lambda *_: False)
    confirmed = pool.placement(job.id)
    pool.attach_worker(job.id, owner="codex", remote=True, tag="newer")

    with pytest.raises(QueueError, match="changed while its earlier worker was checked"):
        pool.forget_worker(job.id, owner="codex", runner=11, earlier=confirmed)
    assert pool.placement(job.id).worker_tag == "newer"
    with pytest.raises(QueueError, match="changed"):
        pool.forget_worker(job.id, owner="codex", runner=12, earlier=pool.placement(job.id))

    pool.forget_worker(job.id, owner="codex", runner=11, earlier=pool.placement(job.id))
    slot = pool.placement(job.id)
    assert (slot.worker_pid, slot.remote_started, slot.worker_tag) == (None, False, None)
    assert pool.requeue_unstarted(job.id, owner="codex", runner=11, reason="preflight")
    assert job_state(queue, job.id) == "queued"


def mark_sync(queue: SQLiteJobQueue, device_name: str, harness: str, state: str) -> None:
    with queue.connect() as connection:
        device_sync.upsert(
            connection,
            device=device_name,
            bot=f"{harness}-bot",
            harness=harness,
            checkout=f"/home/{harness}-bot/factory",
            target="a" * 40,
            sha="b" * 40,
            state=state,
            detail="",
            now=100.0,
        )


@pytest.mark.parametrize("state", ["lagging", "missing", "refused"])
def test_a_lagging_checkout_keeps_its_harness_off_the_device(tmp_path: Path, state: str) -> None:
    pool, queue = make_pool(tmp_path, device("mac", 2, "claude", "codex"), device("pc", 1, "claude"))
    mark_sync(queue, "mac", "claude", state)
    enqueue(queue, "claude", "1")
    enqueue(queue, "codex", "2")

    claimed = pool.claim("claude")
    assert claimed is not None and claimed[1].device == "pc"
    codex = pool.claim("codex")
    assert codex is not None and codex[1].device == "mac"


def test_a_deploying_device_takes_no_harness(tmp_path: Path) -> None:
    pool, queue = make_pool(tmp_path, device("mac", 2, "claude", "codex"))
    mark_sync(queue, "mac", "claude", "deploying")
    enqueue(queue, "codex", "1")

    assert pool.claim("codex") is None
    assert device_pool.free_slots(pool.config, "codex", {}, device_pool.placement_blocks(queue, pool.config, None)) == 0


@pytest.mark.parametrize("state", ["synced", "unreachable"])
def test_a_synced_or_unreachable_checkout_stays_placeable(tmp_path: Path, state: str) -> None:
    pool, queue = make_pool(tmp_path, device("mac", 2, "claude"))
    mark_sync(queue, "mac", "claude", state)
    enqueue(queue, "claude", "1")

    claimed = pool.claim("claude")
    assert claimed is not None and claimed[1].device == "mac"


def test_a_device_with_no_sync_row_stays_placeable(tmp_path: Path) -> None:
    pool, queue = make_pool(tmp_path, device("mac", 2, "claude"))
    enqueue(queue, "claude", "1")

    claimed = pool.claim("claude")
    assert claimed is not None and claimed[1].device == "mac"
    assert device_pool.free_slots(pool.config, "claude", {}, device_pool.placement_blocks(queue, pool.config, None)) == 2


def test_orphans_are_read_and_forgotten_and_a_missing_table_reads_empty(tmp_path: Path) -> None:
    pool, queue = make_pool(tmp_path, device("pc", 2, "codex"))
    assert pool.orphans() == []
    pool.forget_orphan("never-lost")
    enqueue(queue, "codex", "1")
    enqueue(queue, "codex", "2")
    first, _ = pool.claim("codex")
    second, _ = pool.claim("codex")
    pool.attach_worker(first.id, owner="codex", remote=True, tag="t" * 32)
    with queue.connect() as connection:
        device_pool.orphan_slot(connection, second.id, 300.0)
        device_pool.orphan_slot(connection, first.id, 200.0)

    assert pool.orphans() == [
        Orphan(first.id, "pc", "codex", "codex-bot", "t" * 32, 200.0),
        Orphan(second.id, "pc", "codex", "codex-bot", None, 300.0),
    ]

    pool.forget_orphan(first.id)
    assert [orphan.job_id for orphan in pool.orphans()] == [second.id]


def test_an_orphan_keeps_its_runner_and_worker_pids_and_a_legacy_table_gains_them(
    tmp_path: Path,
) -> None:
    pool, queue = make_pool(tmp_path, device("box", 2, "claude"))
    enqueue(queue, "claude", "1")
    enqueue(queue, "claude", "2")
    legacy, _ = pool.claim("claude")
    job, _ = pool.claim("claude")
    with queue.connect() as connection:
        connection.execute(
            "CREATE TABLE device_orphans (job_id TEXT PRIMARY KEY, device TEXT NOT NULL, "
            "harness TEXT NOT NULL, os_user TEXT NOT NULL, worker_tag TEXT, lost_at REAL NOT NULL)"
        )
        connection.execute(
            "INSERT INTO device_orphans VALUES (?, 'box', 'claude', 'claude-bot', NULL, 100.0)",
            (legacy.id,),
        )
        connection.commit()
    assert pool.orphans()[0].worker_pid is None

    pool.attach_runner(job.id, owner="claude", pid=11, start="r", alive=lambda *_: False)
    pool.attach_worker(job.id, owner="claude", pid=12, start="w", run_scope=SCOPE)
    with queue.connect() as connection:
        device_pool.orphan_slot(connection, job.id, 200.0)

    legacy_row, lost = pool.orphans()
    assert legacy_row.run_scope is None
    assert (lost.runner_pid, lost.runner_start, lost.worker_pid, lost.worker_start) == (
        11, "r", 12, "w",
    )
    assert lost.run_scope == SCOPE


SCOPE = f"orch-run-{'a' * 32}.scope"


def test_a_slot_table_from_before_run_scope_gains_it_and_records_the_scope(
    tmp_path: Path,
) -> None:
    pool, queue = make_pool(tmp_path, device("box", 1, "claude"))
    with queue.connect() as connection:
        connection.execute("ALTER TABLE device_slots DROP COLUMN run_scope")
        connection.commit()
    pool = DevicePool(queue, pool.config)
    enqueue(queue, "claude", "1")
    job, _ = pool.claim("claude")
    pool.attach_runner(job.id, owner="claude", pid=11, start="r", alive=lambda *_: False)

    pool.attach_worker(job.id, owner="claude", pid=12, start="w", run_scope=SCOPE)

    assert pool.placement(job.id).run_scope == SCOPE


PRE_1077_SLOTS = (
    "CREATE TABLE device_slots (job_id TEXT PRIMARY KEY, device TEXT NOT NULL, "
    "harness TEXT NOT NULL, os_user TEXT NOT NULL, placed_at REAL NOT NULL, "
    "runner_pid INTEGER, runner_start TEXT, worker_pid INTEGER, worker_start TEXT, "
    "remote_started INTEGER NOT NULL DEFAULT 0, worker_tag TEXT, autostart_pid INTEGER, "
    "autostart_start TEXT, run_token_hash TEXT, run_tag TEXT, run_scope TEXT)"
)
LOGIN_ACCOUNTS = [
    pytest.param("{claude: claude-bot}", "", "claude-default", id="bot-user"),
    pytest.param(
        "{claude: {bot: claude-bot, account: claude-max}}",
        "accounts:\n  - {id: claude-max, harness: claude}\n",
        "claude-max",
        id="legacy-bot-and-account",
    ),
]


def account_config(tmp_path: Path, harnesses: str, accounts: str) -> MachinesConfig:
    path = tmp_path / "machines.yml"
    path.write_text(
        "orch_host: pc\n"
        + accounts
        + "devices:\n"
        "  - name: pc\n"
        "    tailscale_name: pc.example.ts.net\n"
        "    slot_max: 2\n"
        f"    harnesses: {harnesses}\n"
        "    workdir_root: /home/work\n"
        "    ssh_alias: pc-{bot}\n"
        "    factory_root: /srv/factory\n",
        encoding="utf-8",
    )
    config, findings = load_machines_yml(path, overlay=False)
    assert config is not None, findings
    return config


@pytest.mark.parametrize(("harnesses", "accounts", "login"), LOGIN_ACCOUNTS)
def test_a_slot_row_from_before_the_account_column_gains_it_in_place(
    tmp_path: Path, harnesses: str, accounts: str, login: str
) -> None:
    config = account_config(tmp_path, harnesses, accounts)
    queue = SQLiteJobQueue(tmp_path / "d.db", clock=lambda: 100.0)
    with queue.connect() as connection:
        connection.execute(PRE_1077_SLOTS)
        connection.execute(
            "CREATE TABLE device_health (device TEXT PRIMARY KEY, last_ok_at REAL, failing_since REAL)"
        )
        connection.execute(
            "INSERT INTO device_slots (job_id, device, harness, os_user, placed_at, "
            "autostart_pid, autostart_start) VALUES ('old-1', 'pc', 'claude', 'claude-bot', 50.0, 7, 's')"
        )
        connection.commit()
        before = [tuple(row) for row in connection.execute("SELECT rowid, * FROM device_slots")]
        assert read_placements(connection)["old-1"].account is None

    pool = DevicePool(queue, config)

    with queue.connect() as connection:
        columns = [str(row["name"]) for row in connection.execute("PRAGMA table_info(device_slots)")]
        after = [tuple(row) for row in connection.execute("SELECT rowid, * FROM device_slots")]
        (sql,) = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'device_slots'"
        ).fetchone()
    assert "account" in columns
    assert after == [(*before[0], None)]
    assert sql.startswith(PRE_1077_SLOTS.removesuffix(")"))
    placement = pool.placement("old-1")
    assert placement.account is None
    assert placed_account(config, placement) == login


@pytest.mark.parametrize(("harnesses", "accounts", "login"), LOGIN_ACCOUNTS)
def test_a_claim_records_the_devices_login_account_on_the_slot(
    tmp_path: Path, harnesses: str, accounts: str, login: str
) -> None:
    config = account_config(tmp_path, harnesses, accounts)
    queue = SQLiteJobQueue(tmp_path / "d.db", clock=lambda: 100.0)
    pool = DevicePool(queue, config)
    enqueue(queue, "claude", "1")

    job, placement = pool.claim("claude")

    assert placement.account == login
    assert pool.placement(job.id).account == login
    assert placed_account(config, pool.placement(job.id)) == login
    with queue.connect() as connection:
        assert read_placements(connection)[job.id].account == login
        (stored,) = connection.execute(
            "SELECT account FROM device_slots WHERE job_id = ?", (job.id,)
        ).fetchone()
    assert stored == login


def target_pool(tmp_path: Path) -> tuple[DevicePool, SQLiteJobQueue]:
    config, findings = load_machines_yml(write_target_machines(tmp_path))
    assert config is not None, findings
    queue = SQLiteJobQueue(tmp_path / "d.db", clock=lambda: 100.0)
    return DevicePool(queue, config), queue


def hold(queue: SQLiteJobQueue, device_name: str, count: int) -> None:
    with queue.connect() as connection:
        connection.executemany(
            f"INSERT INTO {device_pool.SLOTS_TABLE} "
            "(job_id, device, harness, os_user, placed_at) "
            "VALUES (?, ?, 'claude', 'claude-bot', 1)",
            [(f"{device_name}-held-{n}", device_name) for n in range(count)],
        )


def test_with_the_orch_host_full_a_claim_lands_on_box(tmp_path: Path) -> None:
    pool, queue = target_pool(tmp_path)
    hold(queue, "pc", 10)
    enqueue(queue, "claude", "1")

    claimed = pool.claim("claude")

    assert claimed is not None
    assert (claimed[1].device, claimed[1].os_user) == ("box", "claude-bot")


def test_the_disabled_mac_is_never_chosen_even_with_pc_and_box_full(tmp_path: Path) -> None:
    pool, queue = target_pool(tmp_path)
    hold(queue, "pc", 10)
    hold(queue, "box", 6)
    enqueue(queue, "claude", "1")

    assert pool.claim("claude") is None
    assert [job.state for job in queue.outstanding()] == ["queued"]
    assert pool.used() == {"pc": 10, "box": 6}


def test_a_utilization_tie_goes_to_pc_first_in_file_order(tmp_path: Path) -> None:
    pool, queue = target_pool(tmp_path)
    hold(queue, "pc", 5)
    hold(queue, "box", 3)
    enqueue(queue, "codex", "1")

    claimed = pool.claim("codex")

    assert claimed is not None and claimed[1].device == "pc"


def test_waiting_polling_and_an_unclaimed_autostart_hold_no_slot(tmp_path: Path) -> None:
    now = [0.0]
    dispatcher, queue = setup(tmp_path, now)
    config, _ = load_machines_yml(write_target_machines(tmp_path))
    pool = DevicePool(queue, config)

    class Waited(Exception):
        pass

    def sleep(_seconds: float) -> None:
        assert slot_rows(queue) == []
        raise Waited

    with pytest.raises(Waited):
        wait_for_job(queue, "claude", io.StringIO(), sleep=sleep, pool=pool)
    assert pool.claim("claude", autostart=(4242, "start")) is None
    assert slot_rows(queue) == []

    dispatcher.process(ticket(comment("go-1", GO.format(state="building"), author="grok-login")))
    dispatcher.reconcile_jobs()
    (job,) = queue.outstanding()
    assert job.state == "queued"
    assert slot_rows(queue) == []

    claimed = pool.claim("claude", autostart=(4242, "start"))
    assert claimed is not None
    assert slot_rows(queue) == [(job.id, "pc")]


UNREACHABLE = "preflight-unreachable"
HELD = "memory-wait: MemAvailable 6.0 GiB < 8 GiB"


def backoff_row(queue: SQLiteJobQueue, device_name: str) -> tuple[float, str] | None:
    with queue.connect() as connection:
        row = connection.execute(
            "SELECT until, reason FROM device_backoff WHERE device = ?", (device_name,)
        ).fetchone()
    return (float(row[0]), str(row[1])) if row is not None else None


def unreachable(queue: SQLiteJobQueue, device_name: str, until: float) -> None:
    with queue.connect() as connection:
        device_pool.back_off(connection, device_name, until=until, reason=UNREACHABLE)


def memory_hold(queue: SQLiteJobQueue, device_name: str, until: float) -> None:
    with queue.connect() as connection:
        device_pool.hold_memory(connection, device_name, until=until, reason=HELD)


def clear_memory(queue: SQLiteJobQueue, device_name: str) -> None:
    with queue.connect() as connection:
        device_pool.clear_memory_hold(connection, device_name)


def test_a_memory_held_device_takes_no_claim_until_the_hold_clears(tmp_path: Path) -> None:
    pool, queue = make_pool(tmp_path, device("pc", 4, "claude"))
    config = pool.config
    enqueue(queue, "claude", "1")
    memory_hold(queue, "pc", 280.0)

    assert pool.claim("claude") is None
    assert [job.state for job in queue.outstanding()] == ["queued"]
    blocked = device_pool.placement_blocks(queue, config, None)
    assert choose_device(config, "claude", {}, blocked) is None
    assert device_pool.free_slots(config, "claude", {}, blocked) == 0

    clear_memory(queue, "pc")

    claimed = pool.claim("claude")
    assert claimed is not None and claimed[1].device == "pc"
    assert [job.state for job in queue.outstanding()] == ["running"]


def test_a_memory_hold_on_the_emptier_device_sends_the_claim_to_the_other(tmp_path: Path) -> None:
    pool, queue = make_pool(tmp_path, device("pc", 4, "claude"), device("box", 4, "claude"))
    enqueue(queue, "claude", "busy")
    busy = pool.claim("claude")
    assert busy is not None
    held = "box" if busy[1].device == "pc" else "pc"
    enqueue(queue, "claude", "next")
    memory_hold(queue, held, 280.0)

    claimed = pool.claim("claude")

    assert claimed is not None and claimed[1].device == busy[1].device


def test_a_pinned_qa_job_waits_while_its_device_is_memory_held(tmp_path: Path) -> None:
    queue = SQLiteJobQueue(tmp_path / "d.db", clock=lambda: 100.0)
    pool = DevicePool(
        queue,
        MachinesConfig(devices=(device("pc", 4, "claude"),)),
        pins=lambda: {"owner/repo": frozenset({"pc"})},
    )
    memory_hold(queue, "pc", 280.0)

    def place() -> bool:
        with queue.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            placed = pool.place_qa(connection, "qa-1", "owner/repo")
            connection.commit()
            return placed

    def qa_slots() -> list[str]:
        with queue.connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'qa_device_slots'"
            ).fetchone():
                return []
            return [str(row[0]) for row in connection.execute("SELECT device FROM qa_device_slots")]

    assert place() is False
    assert qa_slots() == []

    clear_memory(queue, "pc")

    assert place() is True
    assert qa_slots() == ["pc"]


def blocked(queue: SQLiteJobQueue, pool: DevicePool) -> bool:
    return "pc" in device_pool.placement_blocks(queue, pool.config, None).devices


@pytest.mark.parametrize("held_until", [280.0, 500.0])
def test_a_memory_hold_and_an_unreachable_backoff_each_block_to_their_own_deadline(
    tmp_path: Path, held_until: float
) -> None:
    now = [100.0]
    pool, queue = make_pool(tmp_path, device("pc", 4, "claude"), now=now)
    unreachable(queue, "pc", 400.0)
    memory_hold(queue, "pc", held_until)

    now[0] = 399.0
    assert blocked(queue, pool)
    now[0] = 450.0
    assert blocked(queue, pool) is (held_until > 450.0)

    clear_memory(queue, "pc")
    now[0] = 399.0
    assert blocked(queue, pool)
    now[0] = 400.0
    assert not blocked(queue, pool)


def test_a_memory_hold_over_an_expired_unreachable_backoff_blocks_until_cleared(
    tmp_path: Path,
) -> None:
    now = [100.0]
    pool, queue = make_pool(tmp_path, device("pc", 4, "claude"), now=now)
    enqueue(queue, "claude", "1")
    unreachable(queue, "pc", 400.0)
    now[0] = 500.0
    memory_hold(queue, "pc", 680.0)

    assert pool.claim("claude") is None
    clear_memory(queue, "pc")
    assert pool.claim("claude") is not None


def test_a_later_back_off_never_shortens_a_longer_one(tmp_path: Path) -> None:
    _, queue = make_pool(tmp_path, device("pc", 4, "claude"))
    unreachable(queue, "pc", 400.0)
    with queue.connect() as connection:
        device_pool.back_off(connection, "pc", until=200.0, reason="lost")

    assert backoff_row(queue, "pc") == (400.0, "lost")


def test_clearing_a_memory_hold_that_was_never_set_is_a_no_op(
    tmp_path: Path,
) -> None:
    pool, queue = make_pool(tmp_path, device("pc", 4, "claude"))
    enqueue(queue, "claude", "1")
    clear_memory(queue, "pc")

    assert pool.claim("claude") is not None
