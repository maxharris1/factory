from __future__ import annotations

import ast
import contextlib
import inspect
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from lib import account_doctor, cli, queue, self_deploy
from lib.grok_delivery import PullDriverDelivery
from lib.machines_yml import MachinesConfig
from lib.queue import Job, SQLiteJobQueue
from lib.signin_wake import announce
from tests.test_account_doctor import (
    CLAUDE_B_HOME,
    KEY,
    NOW,
    SIGNED_OUT_CLAUDE,
    DoctorTransport,
    claude_reply,
    drops,
    framed,
    poller,
    poller_config,
    sign_out_claude_b,
)
from tests.test_account_doctor import World as SshWorld
from tests.test_account_pair_requests import Box, Logout
from tests.test_dispatcher import FakeGrokDelivery, RaisingGrokDelivery

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


def wakes(job_queue: SQLiteJobQueue) -> list[tuple[object, ...]]:
    with contextlib.closing(job_queue.connect()) as connection:
        return [tuple(row) for row in connection.execute(
            "SELECT action, ticket_id, dedupe_key, state, failure_handled FROM jobs ORDER BY created_at, ticket_id")]


def wake(account: str, since: float, state: str = "done", handled: int = 0) -> tuple[object, ...]:
    return ("account-signin", f"{account}@pc", key(account, since), state, handled)


def test_a_new_signed_out_pair_raises_the_flag_and_one_wake_naming_account_and_device(tmp_path: Path) -> None:
    delivery = FakeGrokDelivery()
    world = poller(tmp_path, grok_delivery=delivery)
    sign_out_claude_b(world.transport)
    world.poll(0.0)
    assert [flag[:3] for flag in world.flags()] == [KEY]
    assert wakes(world.queue) == [wake("claude-b", 0.0)]
    [job] = delivery.jobs
    detail = json.loads(job.request.detail or "")
    assert (detail["account"], detail["device"], detail["link"]) == ("claude-b", "pc", LINK)


def test_the_wake_reaches_driver_inbox_through_deliver_grok_jobs(tmp_path: Path) -> None:
    world = poller(tmp_path, grok_delivery=PullDriverDelivery())
    sign_out_claude_b(world.transport)
    world.poll(0.0)
    with contextlib.closing(world.queue.connect()) as connection:
        [(receipt, pull_state, logical_key, payload)] = connection.execute(
            "SELECT jobs.handoff_comment_id, inbox.pull_state, inbox.logical_key, inbox.payload "
            "FROM driver_inbox AS inbox JOIN jobs ON jobs.id = inbox.job_id").fetchall()
    assert (receipt, pull_state) == (f"pull:{logical_key}", "queued")
    body = json.loads(payload)
    assert (body["action"], body["ticket_id"]) == ("account-signin", "claude-b@pc")


def test_a_flagged_pair_is_not_woken_again_until_it_recovers_and_drops_again(tmp_path: Path) -> None:
    world = poller(tmp_path)
    sign_out_claude_b(world.transport)
    for at in (0.0, 120.0, 240.0):
        world.poll(at)
    assert len(world.transport.exchanges) == 6
    assert wakes(world.queue) == [wake("claude-b", 0.0)]
    world.transport.replies = DoctorTransport().replies
    world.poll(360.0)
    assert world.flags() == []
    sign_out_claude_b(world.transport)
    world.poll(2160.0)
    assert wakes(world.queue) == [wake("claude-b", 0.0), wake("claude-b", 2160.0)]


def test_unreachable_then_signed_out_wakes_once(tmp_path: Path) -> None:
    world = poller(tmp_path)
    world.transport.replies["pc-claude-bot"] = b""
    world.poll(0.0)
    assert KEY in [flag[:3] for flag in world.flags()]
    assert wakes(world.queue) == []
    sign_out_claude_b(world.transport)
    world.poll(1800.0)
    world.poll(1920.0)
    assert wakes(world.queue) == [wake("claude-b", 1800.0)]


ORCH_YML = (
    "poll_seconds: 60\n"
    "driver: pull\n"
    "trusted_logins:\n  grok-login: [grok-bot]\n"
    "projects:\n  - repo: owner/factory\n"
)


def orch_db(ssh: SshWorld, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(self_deploy, "own_repo", lambda *_: "owner/factory")
    orch = ssh.tmp / "orch.yml"
    orch.write_text(ORCH_YML, encoding="utf-8")
    cli._runtime(ssh.db, orch, create=True)
    return orch


def orch_poll(ssh: SshWorld, orch: Path) -> list[tuple[object, ...]]:
    dispatcher, _ = cli._runtime(ssh.db, orch, create=False, machines_path=ssh.machines)
    dispatcher.reconcile_jobs()
    return [(action, ticket, state) for action, ticket, _, state, _ in wakes(dispatcher.queue)]


def test_a_settings_sign_out_does_not_wake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ssh = SshWorld(tmp_path, monkeypatch)
    orch = orch_db(ssh, monkeypatch)
    box = Box(ssh, tmp_path)
    Logout(box, "claude")
    box.request("signout", account="claude-b")
    (entry,) = box.run()
    assert entry.result == "signed-out"
    assert orch_poll(ssh, orch) == [("account-signin", "claude-c@pc", "done")]
    assert ssh.statuses()[("claude-b", "pc")] == "signed-out"


def test_orch_accounts_doctor_records_a_drop_and_creates_no_job_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ssh = SshWorld(tmp_path, monkeypatch)
    orch = orch_db(ssh, monkeypatch)
    assert ssh.doctor() == 0
    job_queue = SQLiteJobQueue(ssh.db, clock=lambda: NOW)
    with contextlib.closing(job_queue.connect()) as connection:
        assert [(account, device, announced) for account, device, _, announced in drops(connection)] == [
            ("claude-c", "pc", 0), ("codex-b", "pc", 0)]
    assert wakes(job_queue) == []
    assert orch_poll(ssh, orch) == [("account-signin", "claude-c@pc", "done"),
                                    ("account-signin", "codex-b@pc", "done")]


def test_an_undelivered_wake_that_times_out_leaves_no_stuck_ticket(tmp_path: Path) -> None:
    world = poller(tmp_path, grok_delivery=RaisingGrokDelivery(OSError("webhook down")))
    sign_out_claude_b(world.transport)
    world.poll(0.0)
    world.poll(DAY - 1)
    assert wakes(world.queue) == [wake("claude-b", 0.0, "queued")]
    for at in (DAY, 2 * DAY, 3 * DAY):
        world.poll(at)
        assert wakes(world.queue) == [wake("claude-b", 0.0, "failed", 1)]
        with contextlib.closing(world.queue.connect()) as connection:
            assert connection.execute("SELECT * FROM ticket_state").fetchall() == []
        assert [flag[:3] for flag in world.flags()] == [KEY]
