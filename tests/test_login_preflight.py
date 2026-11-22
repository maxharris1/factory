"""The waiter preflights the harness login before it launches a job (#1334).

A placed run whose `claude auth status` (or `codex login status`) says not
logged in never starts. The waiter holds that account@device pair for the
doctor (lib/account_doctor.py) and requeues the job with no round, attempt or
free requeue charged, so a bad login never burns a ticket retry or sticks the
ticket job-failed-2.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest

from lib import account_doctor
from lib.job_state import MAX_FREE_REQUEUES, free_requeues
from lib.machines_yml import load_machines_yml
from lib.remote import SSH_UNREACHABLE, Exchanged
from tests.test_account_doctor import SIGNED_OUT_CLAUDE, claude_reply, framed
from tests.test_waiter import FakeSpawn, enqueue, slots, state
from tests.test_waiter_remote import make_waiter, remote_machines


@pytest.fixture
def _login_preflight():
    """The real login check, over the suite-wide no-op in tests/conftest.py."""


class LoginTransport:
    def __init__(self, reply: bytes, code: int = 0) -> None:
        self.reply = reply
        self.code = code
        self.aliases: list[str] = []
        self.scripts: list[str] = []

    def run(self, argv: Sequence[str], *, timeout: float) -> int:
        return 0

    def exchange(self, argv: Sequence[str], *, timeout: float, input: bytes = b"") -> Exchanged:
        self.aliases.append(argv[-2])
        self.scripts.append(argv[-1])
        return Exchanged(self.code, self.reply, "")


def pc_config(tmp_path: Path):
    config, findings = load_machines_yml(remote_machines(tmp_path, box_enabled=False))
    assert config is not None, findings
    return config


def check(tmp_path: Path, harness: str, transport: LoginTransport) -> None:
    config = pc_config(tmp_path)
    account = config.device("pc").account_for(harness)
    account_doctor.check_login(transport, config, config.device("pc"), harness, account)


@pytest.mark.parametrize("harness, reply", [
    ("claude", framed((0, 1, SIGNED_OUT_CLAUDE))),
    ("codex", framed((0, 1, b"Not logged in\n"))),
], ids=["claude", "codex"])
def test_a_signed_out_login_raises_signed_out(tmp_path: Path, harness: str, reply: bytes) -> None:
    transport = LoginTransport(reply)
    with pytest.raises(account_doctor.SignedOut) as raised:
        check(tmp_path, harness, transport)
    assert transport.aliases == [f"fakepc-{harness}-bot"]
    assert account_doctor._STATUS[harness].command in transport.scripts[0]
    assert raised.value.device == "pc"
    assert raised.value.account == pc_config(tmp_path).device("pc").account_for(harness)


@pytest.mark.parametrize("harness, transport", [
    ("claude", LoginTransport(framed((0, 0, claude_reply())))),
    ("codex", LoginTransport(framed((0, 0, b"Logged in using ChatGPT\n")))),
    ("claude", LoginTransport(b"", code=SSH_UNREACHABLE)),
    ("claude", LoginTransport(framed((0, 0, b"not json")))),
    ("claude", LoginTransport(b"no frame at all")),
], ids=["claude-ok", "codex-ok", "unreachable", "not-understood", "no-reply"])
def test_a_login_not_read_as_signed_out_passes(tmp_path: Path, harness: str, transport: LoginTransport) -> None:
    check(tmp_path, harness, transport)
    assert len(transport.aliases) == 1


def test_a_harness_without_a_status_command_is_not_checked(tmp_path: Path) -> None:
    transport = LoginTransport(b"")
    check(tmp_path, "cursor", transport)
    assert transport.aliases == []


def pc_waiter(tmp_path: Path, transport: LoginTransport):
    spawn = FakeSpawn()
    log: list[str] = []
    waiter, queue, db = make_waiter(
        tmp_path, remote_machines(tmp_path, box_enabled=False), spawn, log=log, transport=transport
    )
    return waiter, queue, db, spawn, log


def events_of(db: Path, job_id: str) -> list[tuple[str, str]]:
    with sqlite3.connect(db) as connection:
        return connection.execute(
            "SELECT event, detail FROM job_events WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()


def counters(db: Path, job_id: str) -> tuple[int, int]:
    with sqlite3.connect(db) as connection:
        return connection.execute("SELECT attempt, generation FROM jobs WHERE id = ?", (job_id,)).fetchone()


def holds(db: Path) -> set[tuple[str, str, str]]:
    with contextlib.closing(sqlite3.connect(db)) as connection:
        return {
            (hold.account, hold.device, hold.source)
            for hold in account_doctor._holds(connection).values()
        }


def test_a_signed_out_preflight_does_not_launch_and_holds_the_pair(tmp_path: Path) -> None:
    transport = LoginTransport(framed((0, 1, SIGNED_OUT_CLAUDE)))
    waiter, queue, db, spawn, log = pc_waiter(tmp_path, transport)
    job_id = enqueue(queue, "a")
    before = counters(db, job_id)

    assert waiter.step() is None

    assert spawn.calls == [], "the harness never starts on a signed-out login"
    assert transport.aliases == ["fakepc-claude-bot"]
    assert state(db, job_id) == "queued"
    assert counters(db, job_id) == before, "no attempt or generation is charged"
    assert slots(db) == {}
    assert holds(db) == {("claude-bot", "pc", "preflight")}
    [(event, detail)] = events_of(db, job_id)
    assert event == "requeued-signed-out"
    assert detail.endswith("; requeued, no round or attempt charged")
    with sqlite3.connect(db) as connection:
        assert free_requeues(connection, job_id) == 0, "no free requeue is spent"
    assert any("signed out" in line and "requeued" in line for line in log), log


def test_repeated_signed_out_preflights_never_burn_a_retry(tmp_path: Path) -> None:
    transport = LoginTransport(framed((0, 1, SIGNED_OUT_CLAUDE)))
    waiter, queue, db, spawn, _ = pc_waiter(tmp_path, transport)
    job_id = enqueue(queue, "a")
    before = counters(db, job_id)

    for _ in range(MAX_FREE_REQUEUES + 3):
        assert waiter.step() is None

    assert spawn.calls == []
    assert state(db, job_id) == "queued", "a bad login never fails the job, so T6 never retries it"
    assert counters(db, job_id) == before
    assert [event for event, _ in events_of(db, job_id)] == ["requeued-signed-out"] * (MAX_FREE_REQUEUES + 3)
    assert holds(db) == {("claude-bot", "pc", "preflight")}, "one hold however many launches fail"


def test_a_signed_in_preflight_launches(tmp_path: Path) -> None:
    transport = LoginTransport(framed((0, 0, claude_reply())))
    waiter, queue, db, spawn, _ = pc_waiter(tmp_path, transport)
    job_id = enqueue(queue, "a")

    assert waiter.step() == job_id

    assert len(spawn.calls) == 1
    assert transport.aliases == ["fakepc-claude-bot"]
    assert holds(db) == set()
