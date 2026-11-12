"""The poller pushes the dashboard: each poll cycle, or every N minutes.

`orch poll` (which deploy/poller-service.sh runs through run-poller.sh)
starts `orch dashboard push --once` as a separate process after a cycle when
dashboard.push is configured. Nothing here runs ssh, scp or rsync: the spawn
is a fake, and so is the clock.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from lib import cli
from lib.dashboard_config import PushConfig
from lib.dashboard_push import PollPusher, poll_pusher_for
from lib.dispatcher import PollLoop

ROOT = Path(__file__).resolve().parents[1]
DB = Path("/state/orch.db")
SSH_PUSH = PushConfig(
    transport="ssh",
    host="pc-wsl-cursor-bot",
    ssh_config="/home/box/.ssh/config",
    remote_dir="factory-dashboard",
    method="scp",
)
GH = "gh" + "p_" + "A1b2C3d4E5" * 4
HOOK = "https://hooks.example.test/" + "Zx9" * 12
ENV = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/box",
    "USER": "box",
    "LANG": "C.UTF-8",
    "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock",
    "ORCH_DB": str(DB),
    "ORCH_POLL_LOG": "/state/poller.log",
    "ORCH_DASHBOARD_DIR": "/state/dashboard",
    "GH_TOKEN": GH,
    "GITHUB_TOKEN": GH,
    "FACTORY_WEBHOOK_URL": HOOK,
    "FACTORY_WEBHOOK_KEY": "k" * 40,
    "LINEAR_API_KEY": "lin" + "_api_" + "Q7w8E9r0T1" * 4,
    "ANTHROPIC_API_KEY": "sk-" + "x" * 40,
}


class Child:
    def __init__(self) -> None:
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


class Spawn:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.children: list[Child] = []
        self.error = error

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.error is not None:
            raise self.error
        child = Child()
        self.children.append(child)
        return child


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def pusher(config: PushConfig = SSH_PUSH, *, orch: Path | None = None, **kwargs):
    spawn = kwargs.pop("spawn", Spawn())
    clock = kwargs.pop("clock", Clock())
    lines: list[str] = []
    result = poll_pusher_for(
        config,
        db=DB,
        orch=orch,
        environ=ENV,
        spawn=spawn,
        clock=clock,
        log=lines.append,
    )
    return result, spawn, clock, lines


def test_no_push_config_means_no_poll_push() -> None:
    result, spawn, _, lines = pusher(PushConfig())
    assert result is None
    assert spawn.calls == []
    assert lines == []


def test_each_cycle_starts_one_detached_push_once() -> None:
    push, spawn, clock, lines = pusher(orch=Path("/etc/factory/orch.yml"))
    assert isinstance(push, PollPusher)
    for _ in range(3):
        push()
        spawn.children[-1].returncode = 0
        clock.now += 60
    assert len(spawn.calls) == 3
    argv, kwargs = spawn.calls[0]
    assert argv == [
        sys.executable,
        str(ROOT / "bin" / "orch"),
        "dashboard",
        "push",
        "--once",
        "--db",
        str(DB),
        "--orch",
        "/etc/factory/orch.yml",
    ]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs.get("shell", False) is False
    assert kwargs["close_fds"] is True
    assert lines == []


def test_without_orch_flag_the_child_uses_the_default_orch_yml() -> None:
    push, spawn, _, _ = pusher()
    push()
    assert "--orch" not in spawn.calls[0][0]


def test_every_n_minutes_pushes_at_most_once_per_interval() -> None:
    push, spawn, clock, _ = pusher(
        PushConfig(**{**SSH_PUSH.__dict__, "every_minutes": 5})
    )
    started: list[float] = []
    for _ in range(11):  # one cycle a minute for ten minutes
        before = len(spawn.calls)
        push()
        if len(spawn.calls) > before:
            started.append(clock.now)
            spawn.children[-1].returncode = 0
        clock.now += 60
    assert started == [1000.0, 1300.0, 1600.0]


def test_a_push_still_running_is_not_doubled() -> None:
    push, spawn, clock, lines = pusher()
    push()
    clock.now += 60
    push()  # first child still running
    assert len(spawn.calls) == 1
    assert lines == ["dashboard push: previous push still running; skipped this cycle"]
    spawn.children[0].returncode = 1
    clock.now += 60
    push()
    assert len(spawn.calls) == 2


def test_spawn_failure_is_logged_redacted_and_never_raised() -> None:
    push, spawn, clock, lines = pusher(spawn=Spawn(OSError(f"cannot exec {GH} {HOOK}")))
    push()
    clock.now += 60
    push()
    assert len(spawn.calls) == 2
    assert len(lines) == 2
    for line in lines:
        assert line.startswith("dashboard push: ")
        assert GH not in line and HOOK not in line


def test_child_environment_holds_no_secrets() -> None:
    push, spawn, _, _ = pusher()
    push()
    env = spawn.calls[0][1]["env"]
    assert env == {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/box",
        "USER": "box",
        "LANG": "C.UTF-8",
        "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock",
        "ORCH_DB": str(DB),
        "ORCH_POLL_LOG": "/state/poller.log",
        "ORCH_DASHBOARD_DIR": "/state/dashboard",
        "PYTHONUNBUFFERED": "1",
    }
    text = repr(spawn.calls)
    for secret in (GH, HOOK, ENV["FACTORY_WEBHOOK_KEY"], ENV["LINEAR_API_KEY"],
                   ENV["ANTHROPIC_API_KEY"]):
        assert secret not in text


# --- the poll loop ---------------------------------------------------------------


class Dispatcher:
    def __init__(self, fail_on: set[int] = frozenset()) -> None:
        self.polls = 0
        self.fail_on = fail_on

    def poll_once(self, source) -> None:
        self.polls += 1
        if self.polls in self.fail_on:
            raise RuntimeError("tracker down")


def test_poll_loop_runs_after_poll_after_every_cycle_even_a_failed_one() -> None:
    dispatcher = Dispatcher(fail_on={2})
    order: list[str] = []
    errors: list[Exception] = []
    loop = PollLoop(dispatcher, object(), sleep=lambda _: order.append("sleep"))
    loop.run(
        max_polls=3,
        on_error=errors.append,
        after_poll=lambda: order.append(f"push after poll {dispatcher.polls}"),
    )
    assert order == [
        "push after poll 1", "sleep",
        "push after poll 2", "sleep",
        "push after poll 3",
    ]
    assert len(errors) == 1


def test_a_push_hook_that_raises_never_breaks_polling(capsys) -> None:
    dispatcher = Dispatcher()

    def broken() -> None:
        raise RuntimeError(f"boom {GH}")

    loop = PollLoop(dispatcher, object(), sleep=lambda _: None)
    loop.run(max_polls=3, after_poll=broken)
    assert dispatcher.polls == 3
    err = capsys.readouterr().err
    assert err.count("dashboard push: RuntimeError") == 3
    assert GH not in err


def test_poll_loop_without_a_hook_is_unchanged() -> None:
    dispatcher = Dispatcher()
    PollLoop(dispatcher, object(), sleep=lambda _: None).run(max_polls=2)
    assert dispatcher.polls == 2


# --- `orch poll` wiring ----------------------------------------------------------

POLL_ORCH = """\
poll_seconds: 60
projects:
  - repo: maxharris1/factory
"""


def _run_poll(tmp_path: Path, monkeypatch, orch_text: str) -> dict:
    orch = tmp_path / "orch.yml"
    orch.write_text(orch_text, encoding="utf-8")
    database = tmp_path / "orch.db"
    database.write_bytes(b"")
    seen: dict = {}

    class Loop:
        def __init__(self, dispatcher, source) -> None:
            pass

        def run(self, **kwargs) -> None:
            seen.update(kwargs)

    monkeypatch.delenv("ORCH_DB", raising=False)
    # `orch poll` installs a SIGTERM handler; keep pytest's own.
    monkeypatch.setattr(cli.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(cli, "_runtime", lambda path, orch, create, **_: (object(), object()))
    monkeypatch.setattr(cli, "PollLoop", Loop)
    code = cli.main(["poll", "--db", str(database), "--orch", str(orch), "--once",
                     "--pidfile", str(tmp_path / "poller.pid")])
    assert code == 0
    return seen


def test_orch_poll_without_dashboard_push_passes_no_hook(tmp_path: Path, monkeypatch) -> None:
    seen = _run_poll(tmp_path, monkeypatch, POLL_ORCH)
    assert seen.get("after_poll") is None


def test_orch_poll_with_dashboard_push_passes_the_pusher(tmp_path: Path, monkeypatch) -> None:
    seen = _run_poll(
        tmp_path,
        monkeypatch,
        POLL_ORCH
        + """\
dashboard:
  push:
    transport: ssh
    host: pc-wsl-cursor-bot
    ssh_config: /home/box/.ssh/config
    method: scp
    every_minutes: 2
""",
    )
    push = seen["after_poll"]
    assert isinstance(push, PollPusher)
    assert push.every_seconds == 120
    assert push.argv[-4:] == ["--db", str(tmp_path / "orch.db"), "--orch", str(tmp_path / "orch.yml")]


def test_poller_service_runs_orch_poll_which_owns_the_dashboard_push() -> None:
    service = (ROOT / "deploy" / "poller-service.sh").read_text(encoding="utf-8")
    runner = (ROOT / "deploy" / "run-poller.sh").read_text(encoding="utf-8")
    assert 'exec "${HOME}/.local/bin/orch" poll "$@"' in runner
    assert "dashboard push" in service
    assert "dashboard.push" in service
