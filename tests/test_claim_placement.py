"""Issue #90: every harness claim goes through device placement.

A running harness job with no device_slots row (a claim the old way, with
no device) used to sit outside the box's slot_max, so the box ran 4 jobs
on 3 slots. Now a claim with no device pool is refused, a running job with
no placement counts against the box, and `orch status` and the dashboard
show one slot count per device. Fakes only: nothing reaches a host.
"""

from __future__ import annotations

import io
import signal
import sqlite3
import time
from pathlib import Path
from types import MappingProxyType

import pytest

from lib.dashboard import collect_snapshot
from lib.device_pool import DevicePool, used_slots
from lib.machines_yml import Device, MachinesConfig, load_machines_yml
from lib.queue import JobRequest, QueueError, SQLiteJobQueue
from lib.status import build_status, device_rows, render
from lib.wait import WaitError, claim_once, wait_for_job
from tests.pool_helpers import write_target_machines
from tests.test_dashboard import NOW, add_job, seeded
from tests.test_status import FakeStatusSource, job


def box_machines(tmp_path: Path, slots: int = 3) -> Path:
    path = tmp_path / "machines.yml"
    path.write_text(
        "devices:\n"
        "  - name: box\n    local: true\n"
        f"    slot_max: {slots}\n"
        "    harnesses: {claude: claude-bot, codex: codex-bot, cursor: cursor-bot}\n",
        encoding="utf-8",
    )
    return path


def setup(tmp_path: Path, slots: int = 3) -> tuple[SQLiteJobQueue, DevicePool, Path]:
    db = tmp_path / "orch.db"
    queue = SQLiteJobQueue(db, clock=time.time)
    config, findings = load_machines_yml(box_machines(tmp_path, slots))
    assert config is not None, findings
    return queue, DevicePool(queue, config), db


def enqueue(queue: SQLiteJobQueue, key: str, recipient: str = "claude") -> str:
    return queue.enqueue(
        JobRequest(
            repo="owner/repo", ticket_id=key, action="build", recipient=recipient,
            source_comment_id=f"c-{key}", timeout_seconds=3600,
        ),
        dedupe_key=key,
    ).id


def states(db: Path) -> dict[str, str]:
    with sqlite3.connect(db) as connection:
        return dict(connection.execute("SELECT id, state FROM jobs"))


@pytest.fixture
def restore_signals():
    """wait_for_job ignores SIGINT/SIGTERM once it leaves; put them back."""
    saved = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
    yield
    for signum, handler in saved.items():
        signal.signal(signum, handler)


def test_with_the_box_full_a_non_device_claim_does_not_start_a_fourth_job(
    tmp_path: Path, restore_signals: None
) -> None:
    queue, pool, db = setup(tmp_path)
    ids = [enqueue(queue, key) for key in "abcd"]
    for _ in range(3):
        assert claim_once(queue, "claude", pool) is not None
    assert claim_once(queue, "claude", pool) is None  # the box is full

    with pytest.raises(WaitError, match="device pool"):
        claim_once(queue, "claude", None)
    with pytest.raises(WaitError, match="device pool"):
        wait_for_job(queue, "claude", io.StringIO())

    assert states(db)[ids[3]] == "queued"
    assert used_slots(queue, pool.config) == {"box": 3}


def test_a_claim_with_no_pool_is_refused_even_with_room(tmp_path: Path) -> None:
    queue, _, db = setup(tmp_path)
    job_id = enqueue(queue, "a")
    with pytest.raises(WaitError, match="device pool"):
        claim_once(queue, "claude", None)
    assert states(db)[job_id] == "queued"


def test_grok_still_claims_without_a_device(tmp_path: Path) -> None:
    queue, _, _ = setup(tmp_path)
    enqueue(queue, "a", recipient="grok")
    claimed = claim_once(queue, "grok", None)
    assert claimed is not None and claimed[1] is None


def test_a_running_job_with_no_placement_counts_against_the_box(tmp_path: Path) -> None:
    queue, pool, db = setup(tmp_path)
    legacy = enqueue(queue, "old")
    assert queue.claim("claude").id == legacy  # an older `orch wait`, with no device
    ids = [enqueue(queue, key) for key in "abc"]

    assert claim_once(queue, "claude", pool) is not None
    assert claim_once(queue, "claude", pool) is not None
    assert claim_once(queue, "claude", pool) is None  # 1 unplaced + 2 placed = 3/3

    assert states(db)[ids[2]] == "queued"
    assert used_slots(queue, pool.config) == {"box": 3}


def test_a_grok_wake_holds_no_box_slot(tmp_path: Path) -> None:
    queue, pool, _ = setup(tmp_path, slots=1)
    enqueue(queue, "w", recipient="grok")
    queue.claim("grok")
    enqueue(queue, "a")
    assert claim_once(queue, "claude", pool) is not None


def _box_and_pc() -> MachinesConfig:
    return MachinesConfig(
        devices=(
            Device(name="box", slot_max=3, harnesses=MappingProxyType({"claude": "claude-bot"}),
                   local=True),
            Device(name="pc", slot_max=3, harnesses=MappingProxyType({"codex": "codex-bot"}),
                   tailscale_name="pc.example.ts.net", workdir_root="/home/work"),
        )
    )


def test_orch_status_shows_one_slot_count_per_device() -> None:
    running = [
        job(f"placed-{n}", repo="o/repo", ticket_id=str(n), state="running",
            created_at=10, last_activity_at=90)
        for n in range(3)
    ]
    unplaced = job("unplaced", repo="o/repo", ticket_id="9", state="running",
                   created_at=10, last_activity_at=80, recipient="codex")
    source = FakeStatusSource(jobs=(*running, unplaced))
    placements = {f"placed-{n}": "box" for n in range(3)}

    output = render(
        build_status(
            source,
            source,
            clock=lambda: 100,
            devices=device_rows(_box_and_pc(), {"box": 3}),
            placements=placements,
        )
    )

    assert "Slots — box" not in output
    assert "  box [claude] 4/3 slots\n" in output
    assert "    o/repo#9 build -> codex running 1m (beat 20s ago) no-placement\n" in output
    assert "  pc [codex] 0/3 slots\n" in output


def test_orch_status_lists_a_running_grok_wake_without_a_slot() -> None:
    wake = job("wake", repo="o/repo", ticket_id="1", state="running", created_at=10,
               last_activity_at=90, action="clarify", recipient="grok")
    source = FakeStatusSource(jobs=(wake,))
    output = render(
        build_status(source, source, clock=lambda: 100,
                     devices=device_rows(_box_and_pc(), {}), placements={})
    )
    assert "  box [claude] 0/3 slots\n" in output
    assert "Grok wakes (1)\n  o/repo#1 clarify -> grok running 1m (beat 10s ago)\n" in output


def test_the_dashboard_counts_an_unplaced_job_on_the_box_device(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)  # two running harness jobs, neither placed
    with sqlite3.connect(db) as connection:
        connection.execute(
            "CREATE TABLE device_slots (job_id TEXT PRIMARY KEY, device TEXT, "
            "harness TEXT, os_user TEXT, placed_at REAL)"
        )
        for n in range(2):
            add_job(connection, f"placed-{n}", state="running", ticket_id=f"2{n}",
                    last_activity_at=NOW - 30)
            connection.execute(
                "INSERT INTO device_slots VALUES (?, 'box', 'claude', 'claude-bot', ?)",
                (f"placed-{n}", NOW),
            )
    machines = box_machines(tmp_path)

    slots = collect_snapshot(db, log, now=NOW, machines_path=machines)["slots"]

    box = next(device for device in slots["devices"] if device["name"] == "box")
    assert box["held"] == 4  # 2 placed + 2 with no placement: one count, over slot_max
    assert slots["box"] == {"name": "box", "running": 4, "slot_max": 3}


def test_the_dashboard_counts_an_unplaced_job_on_the_orch_host(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)  # two running harness jobs, neither placed

    snapshot = collect_snapshot(db, log, now=NOW, machines_path=write_target_machines(tmp_path))

    slots = snapshot["slots"]
    assert slots["box"] == {"name": "pc", "running": 2, "slot_max": 10}
    held = {device["name"]: device["held"] for device in slots["devices"]}
    assert held == {"pc": 2, "box": 0, "mac": 0}
    assert {job["device"] for job in snapshot["running"] if job["recipient"] != "grok"} == {"pc"}


# --- #70 F2.1: takeover is decided on the row attach_runner read ------------


class _CheckTransport:
    """A device whose check answers `code` (1: the tagged worker runs)."""

    def __init__(self, code: int):
        self.code = code
        self.calls = 0

    def run(self, argv, *, timeout: float) -> int:
        self.calls += 1
        return self.code


def _pc_machines(tmp_path: Path) -> MachinesConfig:
    path = tmp_path / "machines-pc.yml"
    path.write_text(
        "orch_host: pc\ndevices:\n"
        "  - name: pc\n    tailscale_name: pc.example.ts.net\n    slot_max: 1\n"
        "    harnesses: {codex: codex-bot}\n"
        f"    workdir_root: {tmp_path}/work\n    ssh_alias: pc-{{bot}}\n"
        "    factory_root: /srv/factory\n",
        encoding="utf-8",
    )
    config, findings = load_machines_yml(path)
    assert config is not None, findings
    return config


RUNNER_A, RUNNER_B, WORKER_W = 1001, 2002, 3003
WORKER_TAG = "a" * 32


@pytest.mark.parametrize("remote", [False, True], ids=["box", "remote"])
def test_overlapping_runner_refuses_after_worker_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote: bool
) -> None:
    """Runner B reads the slot; A records worker W and dies; B resumes.

    B must refuse, and W's identity must stay on the slot.
    """
    from lib import placed_run

    if remote:
        db = tmp_path / "orch.db"
        queue = SQLiteJobQueue(db, clock=time.time)
        config = _pc_machines(tmp_path)
        pool = DevicePool(queue, config)
        owner, bot = "codex", "codex-bot"
    else:
        queue, pool, _ = setup(tmp_path)
        config = pool.config
        owner, bot = "claude", "claude-bot"
    job_id = enqueue(queue, "a", recipient=owner)
    assert pool.claim(owner) is not None
    dead: set[int] = set()

    def alive(pid: int | None, _start: str | None) -> bool:
        return pid is not None and pid not in dead

    pool.attach_runner(job_id, owner=owner, pid=RUNNER_A, start=None, alive=alive)
    real_placement = pool.placement

    def paused_after_read(read_job_id: str):
        snapshot = real_placement(read_job_id)
        # B is paused here: A records its worker W, then dies.
        if remote:
            pool.attach_worker(job_id, owner=owner, remote=True, tag=WORKER_TAG)
        else:
            pool.attach_worker(job_id, owner=owner, pid=WORKER_W, start=None)
        dead.add(RUNNER_A)
        return snapshot

    monkeypatch.setattr(pool, "placement", paused_after_read)
    transport = _CheckTransport(1)

    with pytest.raises(placed_run.PlacementError, match="earlier worker"):
        placed_run.prepare(
            pool, config, job_id=job_id, bot=bot, owner=owner, worktree=False,
            log=[].append, transport=transport, alive=alive, pid=RUNNER_B,
        )

    slot = real_placement(job_id)
    assert slot is not None
    if remote:
        assert (slot.remote_started, slot.worker_tag) == (True, WORKER_TAG)
        assert transport.calls == 1
    else:
        assert slot.worker_pid == WORKER_W


def test_attach_worker_refuses_to_overwrite_another_worker(tmp_path: Path) -> None:
    queue, pool, _ = setup(tmp_path)
    job_id = enqueue(queue, "a")
    assert pool.claim("claude") is not None
    pool.attach_runner(job_id, owner="claude", pid=RUNNER_A, start=None, alive=lambda *_: False)
    pool.attach_worker(job_id, owner="claude", pid=WORKER_W, start=None, runner=RUNNER_A)

    with pytest.raises(QueueError, match="already names worker"):
        pool.attach_worker(job_id, owner="claude", pid=4004, start=None, runner=RUNNER_A)
    pool.attach_runner(job_id, owner="claude", pid=RUNNER_B, start=None, alive=lambda *_: False)
    with pytest.raises(QueueError, match="run by"):
        pool.attach_worker(job_id, owner="claude", remote=True, tag="t" * 32, runner=RUNNER_A)

    slot = pool.placement(job_id)
    assert slot is not None
    assert (slot.worker_pid, slot.remote_started) == (WORKER_W, False)
