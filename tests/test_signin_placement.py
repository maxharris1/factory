from __future__ import annotations

from pathlib import Path

import pytest

from lib import account_doctor
from lib.device_pool import DevicePool, placement_blocks
from lib.machines_yml import MachinesConfig
from lib.queue import SQLiteJobQueue
from tests.test_headroom_placement import NOW, build, read
from tests.test_pair_placement import claims, enqueue, local, remote

B_ON_BOX_AND_FAR = (
    "accounts:\n"
    "  - {id: claude-a, harness: claude}\n"
    "  - {id: claude-b, harness: claude, signed_in_on: [box, far]}\n"
    "  - {id: claude-c, harness: claude}\n"
    "devices:\n"
    + local(8, "{claude: {bot: claude-bot, account: claude-a}, codex: codex-bot}")
    + remote("far", 1, "{claude: {bot: claude-bot, account: claude-c}, codex: codex-bot}")
)

B_ON_BOX = (
    "accounts:\n"
    "  - {id: claude-a, harness: claude}\n"
    "  - {id: claude-b, harness: claude, signed_in_on: [box]}\n"
    "devices:\n" + local(8, "{claude: {bot: claude-bot, account: claude-a}, codex: codex-bot}")
)


def signins(queue: SQLiteJobQueue, *rows: tuple[str, str, str]) -> None:
    with queue.connect() as connection:
        account_doctor.ensure_table(connection)
        connection.executemany(
            f"INSERT INTO {account_doctor.TABLE} (account, device, status, checked_at) VALUES (?, ?, ?, ?)",
            [(account, device, status, NOW) for account, device, status in rows],
        )
        connection.commit()


def setup(tmp_path: Path, text: str) -> tuple[DevicePool, SQLiteJobQueue, MachinesConfig]:
    pool, queue, config = build(tmp_path, [NOW], text)
    read(queue, "claude-a", 0.80, NOW - 60)
    read(queue, "claude-b", 0.10, NOW - 60)
    read(queue, "claude-c", 0.70, NOW - 60)
    return pool, queue, config


def test_without_the_signins_table_a_signed_in_account_places_on_every_listed_device(tmp_path: Path) -> None:
    pool, queue, _ = setup(tmp_path, B_ON_BOX_AND_FAR)

    assert claims(pool, queue, 2) == [("far", "claude-b"), ("box", "claude-b")]


@pytest.mark.parametrize("box_status", [None, "signed-out", "mismatch", "unreachable"])
def test_a_signed_in_account_places_only_where_it_has_an_ok_row(tmp_path: Path, box_status: str | None) -> None:
    pool, queue, _ = setup(tmp_path, B_ON_BOX_AND_FAR)
    rows = [("claude-b", "far", "ok")]
    if box_status is not None:
        rows.append(("claude-b", "box", box_status))
    signins(queue, *rows)

    assert claims(pool, queue, 2) == [("far", "claude-b"), ("box", "claude-a")], (
        "claude-b has the most headroom but no ok row on box, so box runs its own login"
    )


def test_the_devices_login_account_places_with_no_signin_row(tmp_path: Path) -> None:
    pool, queue, _ = setup(tmp_path, B_ON_BOX)
    signins(queue)

    assert claims(pool, queue, 2) == [("box", "claude-a")] * 2


def test_placement_blocked_drops_a_signed_in_account_with_no_ok_row(tmp_path: Path) -> None:
    pool, queue, config = build(tmp_path, [NOW], B_ON_BOX)
    read(queue, "claude-a", 0.99, NOW - 60)
    read(queue, "claude-b", 0.10, NOW - 60)
    assert not placement_blocks(queue, config, None).blocks("box", "claude"), "no table: claude-b serves box"

    signins(queue, ("claude-b", "box", "signed-out"))
    assert placement_blocks(queue, config, None).blocks("box", "claude"), (
        "claude-a is floored and claude-b is not signed in on box"
    )
    enqueue(queue, "blocked-job")
    assert pool.claim("claude") is None


def test_placement_blocked_keeps_a_signed_in_account_with_an_ok_row(tmp_path: Path) -> None:
    _, queue, config = build(tmp_path, [NOW], B_ON_BOX)
    read(queue, "claude-a", 0.99, NOW - 60)
    read(queue, "claude-b", 0.10, NOW - 60)
    signins(queue, ("claude-b", "box", "ok"))

    assert not placement_blocks(queue, config, None).blocks("box", "claude")
