"""#1315: claim places each job on an (account, device) pair."""

from __future__ import annotations

import itertools
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from lib import lane_limit
from lib.device_pool import (
    DevicePool,
    choose_device,
    free_slots,
    placement_blocks,
    used_slots,
)
from lib.machines_yml import MachinesConfig, load_machines_yml
from lib.queue import JobRequest, SQLiteJobQueue
from tests.pool_helpers import ROOT

NOW = 1_800_000_000.0
KEYS = itertools.count()

REMOTE = """\
    workdir_root: /home/{{bot}}/work
    ssh_alias: {name}-{{bot}}
    factory_root: /srv/factory
    home: /home/{{bot}}
"""


def remote(name: str, slot_max: int, harnesses: str = "{claude: claude-bot, codex: codex-bot}") -> str:
    return (
        f"  - name: {name}\n"
        f"    tailscale_name: {name}.example.ts.net\n"
        f"    slot_max: {slot_max}\n"
        f"    harnesses: {harnesses}\n"
        + REMOTE.format(name=name)
    )


def local(slot_max: int, harnesses: str = "{claude: claude-bot, codex: codex-bot}") -> str:
    return (
        "  - name: box\n    local: true\n"
        f"    slot_max: {slot_max}\n    home: /home/{{bot}}\n    harnesses: {harnesses}\n"
    )


def build(tmp_path: Path, text: str) -> tuple[DevicePool, SQLiteJobQueue, MachinesConfig]:
    path = tmp_path / "machines.yml"
    path.write_text(text, encoding="utf-8")
    config, findings = load_machines_yml(path)
    assert config is not None, findings
    queue = SQLiteJobQueue(tmp_path / "d.db", clock=lambda: NOW)
    return DevicePool(queue, config), queue, config


def enqueue(queue: SQLiteJobQueue, key: str, recipient: str = "claude") -> str:
    return queue.enqueue(
        JobRequest("owner/repo", key, "build", recipient, f"go-{key}", 5400),
        dedupe_key=f"{recipient}:{key}",
    ).id


def limit(queue: SQLiteJobQueue, account: str) -> None:
    job_id = enqueue(queue, f"limit-{account}", "codex")
    with queue.connect() as connection:
        lane_limit.record(
            connection, job_id=job_id, harness="claude", bot="claude-bot", account=account,
            limit=lane_limit.UsageLimit(int(NOW) + 3600), now=NOW,
        )
        connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))


def claims(pool: DevicePool, queue: SQLiteJobQueue, count: int) -> list[tuple[str, str]]:
    placed = []
    for _ in range(count):
        enqueue(queue, f"job-{next(KEYS)}")
        claimed = pool.claim("claude")
        assert claimed is not None
        job, placement = claimed
        with queue.connect() as connection:
            row = connection.execute(
                "SELECT device, account FROM device_slots WHERE job_id = ?", (job.id,)
            ).fetchone()
        assert (row[0], row[1]) == (placement.device, placement.account), "the slot row names the pair"
        placed.append((placement.device, placement.account))
    return placed


TWO_ON_BOX = (
    "accounts:\n"
    "  - {id: claude-a, harness: claude}\n"
    "  - {id: claude-b, harness: claude, signed_in_on: [box]}\n"
    "devices:\n" + local(3, "{claude: {bot: claude-bot, account: claude-a}, codex: codex-bot}")
)


def test_a_limited_account_leaves_its_device_to_the_other_account_signed_in_there(tmp_path: Path) -> None:
    pool, queue, config = build(tmp_path, TWO_ON_BOX)
    limit(queue, "claude-a")

    blocked = placement_blocks(queue, config, None)
    assert not blocked.blocks("box", "claude"), "claude-b can still take work on box"
    assert free_slots(config, "claude", used_slots(queue, config), blocked) == 3
    assert claims(pool, queue, 2) == [("box", "claude-b"), ("box", "claude-b")]


def test_a_device_whose_every_account_is_limited_is_blocked_and_claims_nothing(tmp_path: Path) -> None:
    pool, queue, config = build(tmp_path, TWO_ON_BOX)
    limit(queue, "claude-a")
    limit(queue, "claude-b")
    enqueue(queue, "waiting")

    blocked = placement_blocks(queue, config, None)
    assert blocked.blocks("box", "claude")
    assert not blocked.blocks("box", "codex")
    assert free_slots(config, "claude", used_slots(queue, config), blocked) == 0
    assert pool.claim("claude") is None


CAPPED = (
    "accounts:\n"
    "  - {id: claude-b, harness: claude, signed_in_on: [box, pc], max_jobs: 1}\n"
    "devices:\n" + local(3) + remote("pc", 3)
)


def test_an_account_at_max_jobs_on_any_device_is_skipped_everywhere(tmp_path: Path) -> None:
    pool, queue, _ = build(tmp_path, CAPPED)

    assert claims(pool, queue, 3) == [
        ("pc", "claude-b"),
        ("box", "claude-default"),
        ("pc", "claude-default"),
    ]


def test_accounts_all_limited_or_at_max_jobs_take_nothing(tmp_path: Path) -> None:
    pool, queue, config = build(tmp_path, CAPPED)
    assert claims(pool, queue, 1) == [("pc", "claude-b")]
    limit(queue, "claude-default")
    enqueue(queue, "waiting")

    blocked = placement_blocks(queue, config, None)
    assert blocked.blocks("box", "claude") and blocked.blocks("pc", "claude")
    assert free_slots(config, "claude", used_slots(queue, config), blocked) == 0
    assert pool.claim("claude") is None


def test_fewer_running_jobs_outranks_a_better_device(tmp_path: Path) -> None:
    pool, queue, config = build(
        tmp_path,
        "accounts:\n"
        "  - {id: claude-a, harness: claude}\n"
        "  - {id: claude-b, harness: claude}\n"
        "devices:\n"
        + local(10, "{claude: {bot: claude-bot, account: claude-a}}")
        + remote("pc", 3, "{claude: {bot: claude-bot, account: claude-b}, codex: codex-bot}"),
    )
    for key in ("x", "y"):
        enqueue(queue, key, "codex")
        assert pool.claim("codex")[1].device == "pc"
    assert claims(pool, queue, 1) == [("box", "claude-a")]
    assert choose_device(config, "claude", used_slots(queue, config)).name == "box"

    assert claims(pool, queue, 1) == [("pc", "claude-b")], "claude-b runs none, claude-a runs one"


def test_a_tie_on_running_jobs_and_device_goes_to_the_lower_account_id(tmp_path: Path) -> None:
    pool, queue, _ = build(
        tmp_path,
        "accounts:\n"
        "  - {id: claude-z, harness: claude}\n"
        "  - {id: claude-m, harness: claude, signed_in_on: [box]}\n"
        "devices:\n" + local(4, "{claude: {bot: claude-bot, account: claude-z}}"),
    )

    assert claims(pool, queue, 3) == [("box", "claude-m"), ("box", "claude-z"), ("box", "claude-m")]


THREE_DEVICES = local(4) + remote("pc", 3) + remote("lab", 2)


@pytest.mark.parametrize(
    ("text", "limited", "account"),
    [
        ("devices:\n" + THREE_DEVICES, (), "claude-default"),
        (
            "accounts:\n  - {id: claude-b, harness: claude, signed_in_on: [box, pc, lab]}\n"
            "devices:\n" + THREE_DEVICES,
            ("claude-default",),
            "claude-b",
        ),
    ],
    ids=["single-default-account", "chosen-account-on-three-devices"],
)
def test_the_chosen_account_gets_its_devices_in_the_existing_order(
    tmp_path: Path, text: str, limited: tuple[str, ...], account: str
) -> None:
    pool, queue, config = build(tmp_path, text)
    for name in limited:
        limit(queue, name)
    for key in ("c1", "c2"):
        enqueue(queue, key, "codex")
        assert pool.claim("codex") is not None

    for n in range(7):
        expected = choose_device(config, "claude", used_slots(queue, config))
        assert expected is not None
        assert claims(pool, queue, 1) == [(expected.name, account)], f"claim {n}"
    enqueue(queue, "full")
    assert pool.claim("claude") is None


def test_the_shipped_config_places_exactly_as_choose_device_did(tmp_path: Path) -> None:
    config, _ = load_machines_yml(ROOT / "machines.yml")
    assert config is not None
    queue = SQLiteJobQueue(tmp_path / "d.db", clock=lambda: NOW)
    pool = DevicePool(queue, config)

    while (expected := choose_device(config, "claude", used_slots(queue, config))) is not None:
        assert claims(pool, queue, 1) == [(expected.name, expected.account_for("claude"))]
    enqueue(queue, "full")
    assert pool.claim("claude") is None


def test_two_concurrent_claims_never_put_a_capped_account_over_max_jobs(tmp_path: Path) -> None:
    text = (
        "accounts:\n  - {id: claude-a, harness: claude, max_jobs: 1}\n"
        "devices:\n" + local(4, "{claude: {bot: claude-bot, account: claude-a}}")
    )
    _, queue, config = build(tmp_path, text)
    for key in ("1", "2"):
        enqueue(queue, key)
    pools = [
        DevicePool(SQLiteJobQueue(tmp_path / "d.db", clock=lambda: NOW), config) for _ in range(2)
    ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda pool: pool.claim("claude"), pools))

    assert sorted(result is None for result in results) == [False, True]
    with sqlite3.connect(tmp_path / "d.db") as connection:
        assert connection.execute("SELECT account FROM device_slots").fetchall() == [("claude-a",)]
