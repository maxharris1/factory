from pathlib import Path
from types import MappingProxyType

import pytest

from lib.device_pool import DevicePool
from lib.dispatcher import Dispatcher
from lib.machines_yml import Device, MachinesConfig
from lib.orch_yml import Project, ProjectPins
from lib.queue import Job, JobRequest, SQLiteJobQueue
from tests.test_dispatcher import TRUSTED_LOGINS, FakeGrokDelivery, FakeReviewActions

PINNED = "owner/pinned"
FREE = "owner/free"
PINS = {PINNED: frozenset({"pc"})}


def device(name: str, slot_max: int, *, local: bool = False) -> Device:
    return Device(
        name=name,
        slot_max=slot_max,
        harnesses=MappingProxyType({"claude": "claude-bot", "codex": "codex-bot"}),
        tailscale_name=f"{name}.example.ts.net",
        workdir_root="/home/work",
        local=local,
    )


CONFIG = MachinesConfig(devices=(device("box", 2, local=True), device("mac", 2), device("pc", 1)))


def make_pool(tmp_path: Path, pins=lambda: PINS, now: list[float] | None = None):
    current = now if now is not None else [100.0]
    queue = SQLiteJobQueue(tmp_path / "d.db", clock=lambda: current[0])
    return DevicePool(queue, CONFIG, pins=pins), queue


def enqueue(
    queue: SQLiteJobQueue,
    repo: str,
    key: str,
    *,
    action: str = "build",
    recipient: str = "claude",
    timeout_seconds: int = 5400,
) -> Job:
    return queue.enqueue(
        JobRequest(
            repo=repo,
            ticket_id=key,
            action=action,
            recipient=recipient,
            source_comment_id=f"go-{key}",
            timeout_seconds=timeout_seconds,
        ),
        dedupe_key=key,
    )


def state(queue: SQLiteJobQueue, job_id: str) -> str:
    with queue.connect() as connection:
        return str(connection.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()[0])


def test_a_pinned_job_waits_while_only_unlisted_devices_are_free_then_lands_on_pc(
    tmp_path: Path,
) -> None:
    pool, queue = make_pool(tmp_path)
    holder = enqueue(queue, PINNED, "1")
    held = pool.claim("claude")
    assert held is not None and held[0].id == holder.id and held[1].device == "pc"

    waiting = enqueue(queue, PINNED, "2")
    assert pool.claim("claude") is None
    assert state(queue, waiting.id) == "queued"
    assert pool.used() == {"pc": 1}

    queue.finish(holder.id, owner="claude", handoff_comment_id="ready-1")
    assert pool.release(holder.id, owner="claude") is True
    placed = pool.claim("claude")
    assert placed is not None
    assert placed[0].id == waiting.id
    assert placed[1].device == "pc"


def test_a_waiting_pinned_job_does_not_hold_back_the_jobs_behind_it(tmp_path: Path) -> None:
    pool, queue = make_pool(tmp_path)
    enqueue(queue, PINNED, "1")
    assert pool.claim("claude")[1].device == "pc"
    waiting = enqueue(queue, PINNED, "2")
    behind = enqueue(queue, FREE, "3")

    placed = pool.claim("claude")

    assert placed is not None
    assert placed[0].id == behind.id
    assert placed[1].device in {"box", "mac"}
    assert state(queue, waiting.id) == "queued"


@pytest.mark.parametrize(
    ("action", "recipient"),
    [
        ("plan", "claude"),
        ("build", "claude"),
        ("fix", "claude"),
        ("review", "codex"),
        ("qa", "claude"),
        ("update-from-main", "codex"),
    ],
)
def test_every_action_of_a_pinned_project_is_placed_only_on_a_listed_device(
    tmp_path: Path, action: str, recipient: str
) -> None:
    pool, queue = make_pool(tmp_path)
    first = enqueue(queue, PINNED, "1", action=action, recipient=recipient)
    second = enqueue(queue, PINNED, "2", action=action, recipient=recipient)

    placed = pool.claim(recipient)
    assert placed is not None and placed[1].device == "pc"
    assert pool.claim(recipient) is None
    assert sorted(state(queue, job.id) for job in (first, second)) == ["queued", "running"]
    assert pool.used() == {"pc": 1}


def test_a_repo_matches_its_pin_whatever_its_case(tmp_path: Path) -> None:
    pool, queue = make_pool(tmp_path)
    enqueue(queue, "Owner/Pinned", "1")

    placed = pool.claim("claude")

    assert placed is not None and placed[1].device == "pc"


def test_unpinned_placement_is_the_same_with_or_without_other_projects_pinned(
    tmp_path: Path,
) -> None:
    def placements(pins) -> list[str]:
        root = tmp_path / str(len(list(tmp_path.iterdir())))
        root.mkdir()
        pool, queue = make_pool(root, pins=pins)
        for key in range(6):
            enqueue(queue, FREE, str(key))
        devices = []
        while (placed := pool.claim("claude")) is not None:
            devices.append(placed[1].device)
        return devices

    assert placements(dict) == placements(lambda: PINS) == ["mac", "pc", "box", "mac", "box"]


def test_a_pool_whose_pins_never_loaded_claims_nothing(tmp_path: Path) -> None:
    pool, queue = make_pool(tmp_path, pins=lambda: None)
    job = enqueue(queue, FREE, "1")

    assert pool.claim("claude") is None
    assert state(queue, job.id) == "queued"


def write_orch(path: Path, devices: str | None) -> None:
    line = "" if devices is None else f"    devices: {devices}\n"
    path.write_text(
        "poll_seconds: 30\nprojects:\n  - repo: Owner/Pinned\n"
        + line
        + "  - repo: owner/free\n"
        + "trusted_logins:\n  maxharris1: [grok-bot]\n",
        encoding="utf-8",
    )


@pytest.fixture
def no_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCH_NO_LOCAL_OVERLAY", "1")


def test_project_pins_read_orch_yml_on_each_call(tmp_path: Path, no_overlay: None) -> None:
    orch = tmp_path / "orch.yml"
    pins = ProjectPins(orch)
    assert pins() == {}

    write_orch(orch, "[pc]")
    assert pins() == {PINNED: frozenset({"pc"})}

    write_orch(orch, "[pc, mac]")
    assert pins() == {PINNED: frozenset({"pc", "mac"})}

    write_orch(orch, None)
    assert pins() == {}


def test_project_pins_keep_the_last_good_map_when_orch_yml_breaks(
    tmp_path: Path, no_overlay: None
) -> None:
    orch = tmp_path / "orch.yml"
    write_orch(orch, "[pc]")
    pins = ProjectPins(orch)
    assert pins() == {PINNED: frozenset({"pc"})}

    write_orch(orch, "[]")
    assert pins() == {PINNED: frozenset({"pc"})}


def test_project_pins_are_unknown_until_orch_yml_first_loads(
    tmp_path: Path, no_overlay: None
) -> None:
    orch = tmp_path / "orch.yml"
    orch.write_text("projects: [", encoding="utf-8")
    pins = ProjectPins(orch)
    assert pins() is None

    write_orch(orch, "[pc]")
    assert pins() == {PINNED: frozenset({"pc"})}


def dispatcher_for(tmp_path: Path, now: list[float]) -> tuple[Dispatcher, DevicePool, SQLiteJobQueue]:
    class Devices:
        config = CONFIG

        def reconcile(self, now, lost_after, on_lost):
            return None

    pool, queue = make_pool(tmp_path, now=now)
    dispatcher = Dispatcher(
        tmp_path / "d.db",
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=FakeReviewActions(),
        grok_delivery=FakeGrokDelivery(),
        devices=Devices(),
        projects=(Project(repo="Owner/Pinned", devices=frozenset({"pc"})), Project(repo=FREE)),
    )
    return dispatcher, pool, queue


def test_the_dispatcher_never_times_out_a_pinned_job_waiting_on_a_full_pc(
    tmp_path: Path,
) -> None:
    now = [0.0]
    dispatcher, pool, queue = dispatcher_for(tmp_path, now)
    holder = enqueue(queue, PINNED, "1")
    assert pool.claim("claude")[1].device == "pc"
    waiting = enqueue(queue, PINNED, "2", timeout_seconds=60)

    for minute in (1, 2, 3, 4):
        now[0] = minute * 60
        queue.heartbeat(holder.id, owner="claude")
        dispatcher.reconcile_jobs()
        assert state(queue, waiting.id) == "queued", f"timed out at minute {minute}"

    queue.finish(holder.id, owner="claude", handoff_comment_id="ready-1")
    assert pool.release(holder.id, owner="claude") is True
    placed = pool.claim("claude")
    assert placed is not None and placed[0].id == waiting.id and placed[1].device == "pc"


def test_the_dispatcher_still_times_out_an_unpinned_job_while_a_slot_is_free(
    tmp_path: Path,
) -> None:
    now = [0.0]
    dispatcher, pool, queue = dispatcher_for(tmp_path, now)
    holder = enqueue(queue, PINNED, "1")
    assert pool.claim("claude")[1].device == "pc"
    unpinned = enqueue(queue, FREE, "2", timeout_seconds=60)

    now[0] = 60
    queue.heartbeat(holder.id, owner="claude")
    dispatcher.reconcile_jobs()

    assert state(queue, unpinned.id) == "failed"


def test_the_orch_wait_pool_reads_its_pins_from_orch_yml(tmp_path: Path, no_overlay: None) -> None:
    from lib import cli

    orch = tmp_path / "orch.yml"
    write_orch(orch, "[pc]")
    queue = SQLiteJobQueue(tmp_path / "d.db", clock=lambda: 100.0)
    pool = cli._device_pool(queue, CONFIG, None, orch=orch)
    pool.machines_path = None
    enqueue(queue, PINNED, "1")
    waiting = enqueue(queue, PINNED, "2")

    assert pool.claim("claude")[1].device == "pc"
    assert pool.claim("claude") is None
    assert state(queue, waiting.id) == "queued"
