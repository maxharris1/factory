"""Tests for E2-5: even same-harness multi-account usage balancing in DevicePool."""

from __future__ import annotations

import itertools
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from lib import dashboard_usage, lane_limit, usage_readings
from lib.device_pool import BALANCE_WINDOW_SECONDS, DevicePool, account_loads
from lib.machines_yml import MachinesConfig, load_machines_yml
from lib.queue import JobRequest, SQLiteJobQueue
from tests.test_pair_placement import local

NOW = 1_800_000_000.0
KEYS = itertools.count()

TWO_CLAUDE = (
    "accounts:\n"
    "  - {id: claude-a, harness: claude}\n"
    "  - {id: claude-b, harness: claude, signed_in_on: [box]}\n"
    "devices:\n" + local(8, "{claude: {bot: claude-bot, account: claude-a}, codex: codex-bot}")
)

THREE_CLAUDE = (
    "accounts:\n"
    "  - {id: claude-a, harness: claude}\n"
    "  - {id: claude-b, harness: claude, signed_in_on: [box]}\n"
    "  - {id: claude-c, harness: claude, signed_in_on: [box]}\n"
    "devices:\n" + local(8, "{claude: {bot: claude-bot, account: claude-a}, codex: codex-bot}")
)


def _setup(tmp_path: Path, text: str, now: list[float] | None = None) -> tuple[DevicePool, SQLiteJobQueue, MachinesConfig]:
    clock = [NOW] if now is None else now
    path = tmp_path / "machines.yml"
    path.write_text(text, encoding="utf-8")
    config, findings = load_machines_yml(path)
    assert config is not None, findings
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: clock[0])
    return DevicePool(queue, config), queue, config


def _enqueue(queue: SQLiteJobQueue, harness: str = "claude") -> str:
    n = next(KEYS)
    return queue.enqueue(
        JobRequest("owner/repo", f"t-{n}", "build", harness, f"go-{n}", 5400),
        dedupe_key=f"{harness}:t-{n}",
    ).id


def _write_reading(queue: SQLiteJobQueue, account: str, used: float, at: float, *, harness: str = "claude") -> None:
    with queue.connect() as connection:
        connection.execute(usage_readings.ACCOUNT_READINGS_DDL)
        for window in usage_readings.WINDOWS:
            connection.execute(
                usage_readings.ACCOUNT_READINGS_UPSERT,
                (account, "box", window, harness, f"{harness}-bot", used, int(at) + 3600, at, "probe", 0, at),
            )


def _sequential_claims(
    pool: DevicePool,
    queue: SQLiteJobQueue,
    count: int,
    *,
    now: list[float] | None = None,
    step: float = 0.0,
    harness: str = "claude",
) -> list[str]:
    picked: list[str] = []
    for _ in range(count):
        _enqueue(queue, harness)
        claimed = pool.claim(harness)
        assert claimed is not None
        job, placement = claimed
        picked.append(placement.account or "")
        # Simulate job finishing and freeing its slot before the next sequential job arrives.
        queue.finish(job.id, owner=harness, handoff_comment_id=None)
        pool.release(job.id, owner=harness)
        if now is not None and step:
            now[0] += step
    return picked


def test_identical_or_unmeasured_sequential_jobs_round_robin_across_two_accounts(tmp_path: Path) -> None:
    pool, queue, _ = _setup(tmp_path, TWO_CLAUDE)
    picked = _sequential_claims(pool, queue, 6)
    assert picked == ["claude-a", "claude-b", "claude-a", "claude-b", "claude-a", "claude-b"]
    assert Counter(picked) == {"claude-a": 3, "claude-b": 3}


def test_identical_or_unmeasured_sequential_jobs_round_robin_across_three_accounts(tmp_path: Path) -> None:
    now = [NOW]
    pool, queue, _ = _setup(tmp_path, THREE_CLAUDE, now)
    picked = _sequential_claims(pool, queue, 9, now=now, step=30.0)
    assert picked == [
        "claude-a", "claude-b", "claude-c",
        "claude-a", "claude-b", "claude-c",
        "claude-a", "claude-b", "claude-c",
    ]
    assert Counter(picked) == {"claude-a": 3, "claude-b": 3, "claude-c": 3}


def test_skewed_fresh_readings_drain_higher_headroom_account_first(tmp_path: Path) -> None:
    pool, queue, _ = _setup(tmp_path, TWO_CLAUDE)
    _write_reading(queue, "claude-a", 0.70, NOW - 30)  # 0.30 remaining
    _write_reading(queue, "claude-b", 0.20, NOW - 30)  # 0.80 remaining

    picked = _sequential_claims(pool, queue, 4)
    assert picked == ["claude-b"] * 4

    # Once readings converge to equal headroom, rolling placement share prefers the underused peer.
    _write_reading(queue, "claude-a", 0.50, NOW)
    _write_reading(queue, "claude-b", 0.50, NOW)
    next_picked = _sequential_claims(pool, queue, 4)
    assert next_picked == ["claude-a"] * 4


def test_concurrent_claims_distribute_evenly_across_three_accounts(tmp_path: Path) -> None:
    _, queue, config = _setup(tmp_path, THREE_CLAUDE)
    for _ in range(6):
        _enqueue(queue, "claude")

    def claim_one(_: int) -> str:
        pool = DevicePool(SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: NOW), config)
        claimed = pool.claim("claude")
        assert claimed is not None
        return claimed[1].account or ""

    with ThreadPoolExecutor(max_workers=6) as executor:
        picked = list(executor.map(claim_one, range(6)))

    assert Counter(picked) == {"claude-a": 2, "claude-b": 2, "claude-c": 2}


def test_paused_or_usage_limited_peer_is_skipped_and_never_cross_routes_harness(tmp_path: Path) -> None:
    text = (
        "accounts:\n"
        "  - {id: claude-a, harness: claude, paused: true}\n"
        "  - {id: claude-b, harness: claude, signed_in_on: [box]}\n"
        "  - {id: claude-c, harness: claude, signed_in_on: [box]}\n"
        "devices:\n" + local(8, "{claude: {bot: claude-bot, account: claude-a}, codex: codex-bot}")
    )
    pool, queue, _ = _setup(tmp_path, text)
    # Also usage-limit claude-b.
    limit_job = _enqueue(queue, "codex")
    with queue.connect() as connection:
        lane_limit.record(
            connection,
            job_id=limit_job,
            harness="claude",
            bot="claude-bot",
            account="claude-b",
            limit=lane_limit.UsageLimit(int(NOW) + 3600),
            now=NOW,
        )
        connection.execute("DELETE FROM jobs WHERE id = ?", (limit_job,))

    picked = _sequential_claims(pool, queue, 3)
    assert picked == ["claude-c", "claude-c", "claude-c"]

    # Usage-limit claude-c too: claude jobs must NOT cross-route to codex.
    limit_job2 = _enqueue(queue, "codex")
    with queue.connect() as connection:
        lane_limit.record(
            connection,
            job_id=limit_job2,
            harness="claude",
            bot="claude-bot",
            account="claude-c",
            limit=lane_limit.UsageLimit(int(NOW) + 3600),
            now=NOW,
        )
        connection.execute("DELETE FROM jobs WHERE id = ?", (limit_job2,))
    _enqueue(queue, "claude")
    assert pool.claim("claude") is None


def test_rolling_window_prunes_old_placements_and_dashboard_reports_balance_and_skew(tmp_path: Path) -> None:
    now = [NOW]
    pool, queue, config = _setup(tmp_path, TWO_CLAUDE, now)
    _sequential_claims(pool, queue, 4, now=now, step=10.0)

    with queue.connect() as connection:
        usage = dashboard_usage.read_usage(connection, now[0], config)
    by_acct = {entry["account"]: entry for entry in usage["accounts"]}
    assert by_acct["claude-a"]["recent_placements"] == 2
    assert by_acct["claude-b"]["recent_placements"] == 2
    assert by_acct["claude-a"]["placement_share"] == pytest.approx(0.5)
    assert by_acct["claude-b"]["placement_share"] == pytest.approx(0.5)
    assert by_acct["claude-a"]["skew"] == "balanced"
    assert by_acct["claude-b"]["skew"] == "balanced"
    assert by_acct["claude-a"]["effective_score"] == pytest.approx(0.5)

    # Skew claude-b heavily via headroom reading and verify skew indicator flags it.
    _write_reading(queue, "claude-a", 0.80, now[0])
    _write_reading(queue, "claude-b", 0.10, now[0])
    _sequential_claims(pool, queue, 8, now=now, step=10.0)
    with queue.connect() as connection:
        usage_skewed = dashboard_usage.read_usage(connection, now[0], config)
    skewed = {entry["account"]: entry for entry in usage_skewed["accounts"]}
    assert skewed["claude-b"]["skew"] == "high"
    assert skewed["claude-a"]["skew"] == "low"

    # Advance past the rolling balance window: old placements drop out cleanly.
    now[0] += BALANCE_WINDOW_SECONDS + 60.0
    with queue.connect() as connection:
        loads = account_loads(connection, config, now[0])
    assert loads.recent_placements == {}
