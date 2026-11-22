from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from lib import usage_readings
from lib.device_pool import DevicePool, free_slots, placement_blocks, used_slots
from lib.dispatcher import Dispatcher
from lib.machines_yml import MachinesConfig, load_machines_yml
from lib.queue import JobRequest, SQLiteJobQueue
from tests.test_dispatcher import TRUSTED_LOGINS, FakeGrokDelivery, FakeReviewActions
from tests.test_pair_placement import claims, local

NOW = 1_800_000_000.0

TWO_ON_BOX = (
    "accounts:\n"
    "  - {id: claude-a, harness: claude}\n"
    "  - {id: claude-b, harness: claude, signed_in_on: [box]}\n"
    "devices:\n" + local(8, "{claude: {bot: claude-bot, account: claude-a}, codex: codex-bot}")
)


def build(tmp_path: Path, now: list[float]) -> tuple[DevicePool, SQLiteJobQueue, MachinesConfig]:
    path = tmp_path / "machines.yml"
    path.write_text(TWO_ON_BOX, encoding="utf-8")
    config, findings = load_machines_yml(path)
    assert config is not None, findings
    queue = SQLiteJobQueue(tmp_path / "d.db", clock=lambda: now[0])
    return DevicePool(queue, config), queue, config


def read(queue: SQLiteJobQueue, account: str, used: float, at: float) -> None:
    with queue.connect() as connection:
        connection.execute(usage_readings.ACCOUNT_READINGS_DDL)
        for window in usage_readings.WINDOWS:
            connection.execute(
                usage_readings.ACCOUNT_READINGS_UPSERT,
                (account, "box", window, "claude", "claude-bot", used, int(NOW) + 3600, at, "probe", 0, at),
            )


def test_the_account_with_more_fresh_headroom_wins_before_fewer_running_jobs(tmp_path: Path) -> None:
    pool, queue, _ = build(tmp_path, [NOW])
    read(queue, "claude-a", 0.70, NOW - 60)
    read(queue, "claude-b", 0.30, NOW - 60)

    assert claims(pool, queue, 3) == [("box", "claude-b")] * 3, "0.70 left beats 0.30 even at 2 running"


def test_a_burst_within_one_reading_alternates_between_two_equal_accounts(tmp_path: Path) -> None:
    pool, queue, _ = build(tmp_path, [NOW])
    read(queue, "claude-a", 0.40, NOW - 60)
    read(queue, "claude-b", 0.40, NOW - 60)

    assert [account for _, account in claims(pool, queue, 5)] == [
        "claude-a", "claude-b", "claude-a", "claude-b", "claude-a",
    ]


def test_a_new_reading_clears_the_pending_charge(tmp_path: Path) -> None:
    pool, queue, _ = build(tmp_path, [NOW])
    read(queue, "claude-a", 0.40, NOW - 60)
    read(queue, "claude-b", 0.41, NOW - 60)
    assert claims(pool, queue, 1) == [("box", "claude-a")]

    read(queue, "claude-a", 0.40, NOW + 30)
    assert claims(pool, queue, 1) == [("box", "claude-a")], "its job started before the new reading"


def test_an_account_with_no_fresh_reading_ranks_at_half(tmp_path: Path) -> None:
    pool, queue, _ = build(tmp_path, [NOW])
    read(queue, "claude-a", 0.99, NOW - 2 * usage_readings.STALE_SECONDS)
    read(queue, "claude-b", 0.49, NOW - 60)

    assert claims(pool, queue, 1) == [("box", "claude-b")], "0.51 left beats the stale reading's 0.5"
    assert claims(pool, queue, 1) == [("box", "claude-a")], "a stale reading is no floor; 0.5 beats 0.49"


def test_a_floored_account_is_skipped(tmp_path: Path) -> None:
    pool, queue, config = build(tmp_path, [NOW])
    read(queue, "claude-a", 0.96, NOW - 60)
    read(queue, "claude-b", 0.80, NOW - 60)

    assert not placement_blocks(queue, config, None).blocks("box", "claude")
    assert claims(pool, queue, 4) == [("box", "claude-b")] * 4, "0.04 left is under the floor"


def test_a_queued_job_behind_floored_accounts_waits_and_does_not_time_out(tmp_path: Path) -> None:
    now = [NOW]
    pool, queue, config = build(tmp_path, now)
    read(queue, "claude-a", 0.96, NOW - 60)
    read(queue, "claude-b", 0.99, NOW - 60)
    job_id = queue.enqueue(
        JobRequest("owner/repo", "floored", "build", "claude", "go-floored", 600),
        dedupe_key="claude:floored",
    ).id

    blocked = placement_blocks(queue, config, None)
    assert blocked.blocks("box", "claude") and not blocked.blocks("box", "codex")
    assert free_slots(config, "claude", used_slots(queue, config), blocked) == 0
    assert pool.claim("claude") is None

    dispatcher = Dispatcher(
        queue.path,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=FakeReviewActions(),
        grok_delivery=FakeGrokDelivery(),
        devices=SimpleNamespace(config=config, pool=pool, reconcile=lambda *_: None),
    )
    now[0] = NOW + 601
    dispatcher.reconcile_jobs()

    (job,) = [job for job in queue.outstanding() if job.id == job_id]
    assert job.state == "queued", "not timed out while no account can take it"
    assert job.last_activity_at == NOW + 601, "its queue wait restarted"


def test_equal_adjusted_scores_go_to_the_account_running_fewer_jobs(tmp_path: Path) -> None:
    pool, queue, _ = build(tmp_path, [NOW])
    read(queue, "claude-a", 0.80, NOW - 60)
    read(queue, "claude-b", 0.82, NOW - 60)

    assert claims(pool, queue, 2) == [("box", "claude-a"), ("box", "claude-b")], "0.20 less one charge ties 0.18"


def test_a_queued_job_keeps_its_timeout_through_a_floor_that_clears(tmp_path: Path) -> None:
    now = [NOW]
    pool, queue, config = build(tmp_path, now)
    read(queue, "claude-a", 0.96, NOW - 60)
    read(queue, "claude-b", 0.96, NOW - 60)
    with queue.connect() as connection:
        connection.execute(f"UPDATE {usage_readings.ACCOUNT_READINGS} SET resets_at = ?", (int(NOW) + 590,))
    job_id = queue.enqueue(
        JobRequest("owner/repo", "floored", "build", "claude", "go-floored", 600),
        dedupe_key="claude:floored",
    ).id
    assert pool.claim("claude") is None

    dispatcher = Dispatcher(
        queue.path,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=FakeReviewActions(),
        grok_delivery=FakeGrokDelivery(),
        devices=SimpleNamespace(config=config, pool=pool, reconcile=lambda *_: None),
    )
    for seconds in range(60, 601, 60):
        now[0] = NOW + seconds
        dispatcher.reconcile_jobs()

    assert not placement_blocks(queue, config, None).blocks("box", "claude"), "the windows reset at +590"
    (job,) = [job for job in queue.outstanding() if job.id == job_id]
    assert job.state == "queued", "the floor's hold did not use up its timeout"
    assert pool.claim("claude") is not None
