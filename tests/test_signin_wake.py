from __future__ import annotations

import ast
import contextlib
import inspect
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from lib import account_doctor, queue
from lib.machines_yml import MachinesConfig
from lib.queue import Job, SQLiteJobQueue
from lib.signin_wake import announce
from tests.test_account_doctor import (
    CLAUDE_B_HOME,
    NOW,
    SIGNED_OUT_CLAUDE,
    DoctorTransport,
    claude_reply,
    drops,
    framed,
    poller_config,
    sign_out_claude_b,
)

REPO = "Owner/Factory"
DAY = 24 * 60 * 60
LINK = f"https://github.com/{REPO}/blob/HEAD/docs/ACCOUNTS.md#when-a-sign-in-drops"
TOKEN_REPLY = claude_reply(method="oauth_token")


@dataclass
class World:
    queue: SQLiteJobQueue
    config: MachinesConfig
    transport: DoctorTransport = field(default_factory=DoctorTransport)
    now: list[float] = field(default_factory=lambda: [NOW])

    def doctor(self, at: float, *, deliberate: tuple[str, str] | None = None) -> None:
        self.now[0] = at
        with contextlib.closing(self.queue.connect()) as connection:
            account_doctor.run(connection, self.config, self.transport, at, REPO, deliberate=deliberate)

    def announce(self, repo: str | None = REPO) -> list[Job]:
        return announce(self.queue, self.config, repo)

    def jobs(self) -> list[tuple[object, ...]]:
        with contextlib.closing(self.queue.connect()) as connection:
            return [tuple(row) for row in connection.execute(
                "SELECT action, recipient, repo, ticket_id, timeout_seconds, source_comment_id, dedupe_key, state "
                "FROM jobs ORDER BY created_at, ticket_id")]

    def drops(self) -> list[tuple[object, ...]]:
        with contextlib.closing(self.queue.connect()) as connection:
            return drops(connection)


@pytest.fixture
def world(tmp_path: Path) -> World:
    now = [NOW]
    return World(SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: now[0]), poller_config(tmp_path), now=now)


def both_down(world: World) -> None:
    world.transport.replies["pc-claude-bot"] = framed((0, 0, TOKEN_REPLY), (1, 1, SIGNED_OUT_CLAUDE))


def key(account: str, since: float) -> str:
    return f"signin-drop:owner/factory:{account}@pc:{since}"


def row(account: str, since: float = NOW) -> tuple[object, ...]:
    return ("account-signin", "grok", REPO, f"{account}@pc", DAY, key(account, since), key(account, since), "queued")


def test_announce_queues_one_wake_per_unannounced_drop_and_marks_it(world: World) -> None:
    both_down(world)
    world.doctor(NOW)
    jobs = world.announce()
    assert [job.dedupe_key for job in jobs] == [key("claude-b", NOW), key("claude-default", NOW)]
    assert world.jobs() == [row("claude-b"), row("claude-default")]
    assert world.drops() == [("claude-b", "pc", NOW, 1), ("claude-default", "pc", NOW, 1)]
    assert "account-signin" in queue.ACTIONS


def test_an_announce_that_raises_marks_nothing_and_the_next_one_wakes_once(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    both_down(world)
    world.doctor(NOW)
    real = world.queue.enqueue_in_transaction
    calls: list[str] = []

    def second_raises(*args: object, **kwargs: object) -> Job:
        calls.append(str(kwargs["dedupe_key"]))
        if len(calls) == 2:
            raise RuntimeError("enqueue failed")
        return real(*args, **kwargs)

    monkeypatch.setattr(world.queue, "enqueue_in_transaction", second_raises)
    with pytest.raises(RuntimeError, match="enqueue failed"):
        world.announce()
    monkeypatch.undo()
    assert world.jobs() == []
    assert world.drops() == [("claude-b", "pc", NOW, 0), ("claude-default", "pc", NOW, 0)]
    assert len(world.announce()) == 2
    assert world.announce() == []
    assert world.jobs() == [row("claude-b"), row("claude-default")]


def test_announce_without_a_repo_queues_nothing_and_leaves_the_drop_unannounced(world: World) -> None:
    sign_out_claude_b(world.transport)
    world.doctor(NOW)
    assert world.announce(None) == []
    assert world.jobs() == []
    assert world.drops() == [("claude-b", "pc", NOW, 0)]


def test_a_pair_that_stays_down_is_queued_once_and_again_only_after_it_recovers(world: World) -> None:
    down = DoctorTransport()
    sign_out_claude_b(down)
    for at, transport in [(NOW, down), (NOW + 120, down), (NOW + 240, down),
                          (NOW + 360, DoctorTransport()), (NOW + 480, down)]:
        world.transport = transport
        world.doctor(at)
        world.announce()
    assert world.jobs() == [row("claude-b", NOW), row("claude-b", NOW + 480)]


def test_a_deliberate_drop_queues_nothing(world: World) -> None:
    sign_out_claude_b(world.transport)
    world.doctor(NOW, deliberate=("claude-b", "pc"))
    assert world.announce() == []
    assert world.jobs() == []


def test_the_wake_names_account_device_and_harness_and_asks_for_a_settings_sign_in(world: World) -> None:
    sign_out_claude_b(world.transport)
    world.doctor(NOW)
    [job] = world.announce()
    detail = json.loads(job.request.detail or "")
    reading = (f"signed-out; run: sign in as claude-bot on pc: env -u CLAUDE_CODE_OAUTH_TOKEN "
               f"CLAUDE_CONFIG_DIR={CLAUDE_B_HOME} claude auth login, then orch accounts doctor; "
               f"{account_doctor.NOT_LOGGED_IN}")
    text = detail.pop("detail")
    assert detail == {"account": "claude-b", "device": "pc", "harness": "claude", "reading": reading,
                      "takes_jobs": False, "link": LINK}
    assert text == (
        f"The claude sign-in of account claude-b on device pc dropped. The doctor reads: {reading}. "
        "No job is placed there. Ask Max once to sign in from the dashboard Settings page. "
        "When Settings has no Sign in for this account and device, give Max the step in the reading. "
        "Do not run a login, and do not copy a login from another device."
    )


def test_a_default_login_that_still_takes_jobs_says_so(world: World) -> None:
    world.transport.replies["pc-claude-bot"] = framed((0, 0, TOKEN_REPLY), (1, 0, claude_reply(
        org="4b0c6c5e-0000-4000-8000-00000000b002", email="bot-two@example.com")))
    world.doctor(NOW)
    [job] = world.announce()
    detail = json.loads(job.request.detail or "")
    assert (detail["account"], detail["takes_jobs"]) == ("claude-default", True)
    assert "Jobs are still placed there." in detail["detail"]
    assert "No job is placed there." not in detail["detail"]


def test_announce_takes_no_transport_and_imports_no_sign_in_module() -> None:
    assert list(inspect.signature(announce).parameters) == ["queue", "config", "repo"]
    tree = ast.parse((Path(__file__).resolve().parents[1] / "lib" / "signin_wake.py").read_text(encoding="utf-8"))
    names = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)] + [
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names]
    banned = ("account_signin", "signin_attempt", "signin_broker", "remote")
    assert [name for name in names if any(word in name for word in banned)] == []
