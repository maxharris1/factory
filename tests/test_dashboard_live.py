"""Real-time dashboard push: a fresh status.json whenever orch.db changes.

With `dashboard.push.live: true` the poller's push hook is a `LivePusher`.
A watcher notices orch.db changes (a job claimed, finished, heartbeat, a
verdict recorded) by stat-ing orch.db and its WAL, and the end of every poll
cycle counts as a change too. Each change starts `orch dashboard push --once
--status-only` (status.json alone, temp-then-rename over scp), never while
the previous push is still running and never until 5 seconds after the later
of the previous push's start and the tick that saw it end. The full
pair (index.html too) still goes at the old cadence: the first cycle, then
every cycle or every `every_minutes`.

Nothing here runs a real ssh or scp: the spawn and clocks are fakes, and the
one end-to-end CLI test puts fake `scp` and `ssh` programs first on PATH.
"""

import gc
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from lib import cli
from lib.dashboard import collect_snapshot
from lib.dashboard_config import PushConfig, parse_dashboard
from lib.dashboard_push import (
    LIVE_MIN_SECONDS,
    LivePusher,
    PollPusher,
    db_fingerprint,
    poll_pusher_for,
)
from lib.load import Finding
from lib.queue import JobRequest, SQLiteJobQueue

ROOT = Path(__file__).resolve().parents[1]
DB = Path("/state/orch.db")
LIVE_PUSH = PushConfig(
    transport="ssh",
    host="pc-wsl-cursor-bot",
    ssh_config="/home/box/.ssh/config",
    remote_dir="factory-dashboard",
    method="scp",
    live=True,
)
GH = "gh" + "p_" + "A1b2C3d4E5" * 4
HOOK = "https://hooks.example.test/" + "Zx9" * 12
ENV = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/box",
    "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock",
    "ORCH_DB": str(DB),
    "GH_TOKEN": GH,
    "FACTORY_WEBHOOK_URL": HOOK,
    "LINEAR_API_KEY": "lin" + "_api_" + "Q7w8E9r0T1" * 4,
}
BASE = [sys.executable, str(ROOT / "bin" / "orch"), "dashboard", "push", "--once", "--db", str(DB)]


class Child:
    def __init__(self) -> None:
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


class Spawn:
    def __init__(self, error: Exception | None = None, *, finish: bool = False) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.children: list[Child] = []
        self.error = error
        self.finish = finish

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.error is not None:
            raise self.error
        child = Child()
        if self.finish:
            child.returncode = 0
        self.children.append(child)
        return child

    def argvs(self) -> list[list[str]]:
        return [argv for argv, _ in self.calls]


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Fingerprint:
    """Stands in for stat-ing orch.db: bump() is one committed change."""

    def __init__(self) -> None:
        self.value = 0
        self.paths: list[Path] = []

    def bump(self) -> None:
        self.value += 1

    def __call__(self, path: Path) -> int:
        self.paths.append(path)
        return self.value


def live(config: PushConfig = LIVE_PUSH, **kwargs):
    spawn = kwargs.pop("spawn", Spawn(finish=True))
    clock = kwargs.pop("clock", Clock())
    wall = kwargs.pop("wall", Clock(1_800_000_000.0))
    fingerprint = kwargs.pop("fingerprint", Fingerprint())
    lines: list[str] = []
    pusher = poll_pusher_for(
        config,
        db=DB,
        orch=kwargs.pop("orch", None),
        environ=ENV,
        spawn=spawn,
        clock=clock,
        wall=wall,
        fingerprint=fingerprint,
        log=lines.append,
    )
    return pusher, spawn, clock, fingerprint, lines


def status_only(argv: list[str]) -> bool:
    return "--status-only" in argv


# --- config -----------------------------------------------------------------------


def parse(value: object):
    findings: list[Finding] = []
    return parse_dashboard(value, findings), [
        item.message for item in findings if item.severity == "error"
    ]


def test_live_is_off_unless_asked_for() -> None:
    config, errors = parse({"push": {"transport": "ssh", "host": "pc-wsl-cursor-bot"}})
    assert errors == []
    assert config.push.live is False
    assert PushConfig().live is False


def test_live_true_parses_for_ssh() -> None:
    config, errors = parse(
        {"push": {"transport": "ssh", "host": "pc-wsl-cursor-bot", "live": True}}
    )
    assert errors == []
    assert config.push.live is True


@pytest.mark.parametrize("value", ["yes", 1, None, "true"])
def test_live_must_be_a_boolean(value: object) -> None:
    _, errors = parse({"push": {"transport": "ssh", "host": "pc-wsl-cursor-bot", "live": value}})
    assert errors == ["dashboard.push.live must be true or false"]


def test_live_is_only_for_ssh() -> None:
    _, errors = parse({"push": {"transport": "none", "live": True}})
    assert errors == ["dashboard.push.live is only for transport ssh"]


def test_shipped_and_example_configs_document_live_push() -> None:
    # Documented, commented out: nothing pushes until Max turns it on.
    for path in (ROOT / "orch.yml", ROOT / "config" / "orch.example.yml"):
        assert re.search(r"(?m)^#\s+live: true|^\s+# live: true", path.read_text(encoding="utf-8")), path


# --- the hook ---------------------------------------------------------------------


def test_live_config_gives_a_live_pusher_and_plain_config_does_not() -> None:
    pusher, *_ = live()
    assert isinstance(pusher, LivePusher)
    # Still the cycle pusher underneath: same full-pair argv and cadence.
    assert isinstance(pusher, PollPusher)
    assert pusher.argv == BASE
    plain, *_ = live(PushConfig(**{**LIVE_PUSH.__dict__, "live": False}))
    assert type(plain) is PollPusher


def test_the_watcher_watches_the_dispatcher_database() -> None:
    pusher, _, _, fingerprint, _ = live()
    pusher.tick()
    assert fingerprint.paths and set(fingerprint.paths) == {DB}


def test_an_unchanged_database_pushes_nothing() -> None:
    pusher, spawn, clock, _, lines = live()
    for _ in range(20):
        pusher.tick()
        clock.now += 0.25
    assert spawn.calls == []
    assert lines == []


def test_a_database_change_pushes_status_json_only() -> None:
    pusher, spawn, _, fingerprint, lines = live()
    fingerprint.bump()
    pusher.tick()
    assert len(spawn.calls) == 1
    argv, kwargs = spawn.calls[0]
    assert argv[: len(BASE)] == BASE
    assert status_only(argv)
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["close_fds"] is True
    assert kwargs.get("shell", False) is False
    assert lines == []


def test_the_live_child_environment_holds_no_secrets() -> None:
    pusher, spawn, _, fingerprint, _ = live()
    fingerprint.bump()
    pusher.tick()
    env = spawn.calls[0][1]["env"]
    assert env == {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/box",
        "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock",
        "ORCH_DB": str(DB),
        "PYTHONUNBUFFERED": "1",
    }
    text = repr(spawn.calls)
    for secret in (GH, HOOK, ENV["LINEAR_API_KEY"]):
        assert secret not in text


def test_each_separate_change_is_pushed() -> None:
    pusher, spawn, clock, fingerprint, _ = live()
    for _ in range(3):
        fingerprint.bump()
        for _ in range(24):
            pusher.tick()
            clock.now += 0.25
    assert len(spawn.calls) == 3
    assert all(status_only(argv) for argv in spawn.argvs())


def test_a_burst_of_changes_is_rate_limited_to_one_push_every_5_seconds() -> None:
    pusher, spawn, clock, fingerprint, _ = live()
    started: list[float] = []
    for _ in range(300):
        fingerprint.bump()
        before = len(spawn.calls)
        pusher.tick()
        if len(spawn.calls) > before:
            started.append(clock.now)
        clock.now = round(clock.now + 0.1, 3)
    assert LIVE_MIN_SECONDS == 5.0
    gaps = [later - earlier for earlier, later in zip(started, started[1:])]
    assert all(gap >= LIVE_MIN_SECONDS - 1e-9 for gap in gaps), gaps
    assert 5 <= len(started) <= 6


def test_a_long_push_is_followed_by_5_idle_seconds_then_the_pending_change() -> None:
    pusher, spawn, clock, fingerprint, lines = live(spawn=Spawn())
    fingerprint.bump()
    pusher.tick()
    for _ in range(80):
        clock.now += 0.25
        fingerprint.bump()
        pusher.tick()
    assert len(spawn.calls) == 1
    spawn.children[0].returncode = 0
    clock.now += 0.25
    settled = clock.now
    pusher.tick()
    assert len(spawn.calls) == 1
    for _ in range(19):
        clock.now += 0.25
        pusher.tick()
    assert clock.now - settled == 4.75
    assert len(spawn.calls) == 1
    clock.now += 0.25
    pusher.tick()
    assert len(spawn.calls) == 2
    assert clock.now - settled == LIVE_MIN_SECONDS
    assert lines == []


def test_a_change_after_a_quiet_spell_is_pushed_on_the_tick_that_sees_it() -> None:
    pusher, spawn, clock, fingerprint, _ = live(spawn=Spawn())
    outcomes: list[bool] = []
    pusher.record = outcomes.append
    fingerprint.bump()
    pusher.tick()
    clock.now += 0.25
    spawn.children[0].returncode = 0
    pusher.tick()
    assert outcomes == [True]
    for _ in range(240):
        clock.now += 0.25
        pusher.tick()
    assert len(spawn.calls) == 1
    fingerprint.bump()
    clock.now += 0.25
    pusher.tick()
    assert len(spawn.calls) == 2


def test_the_last_change_of_a_burst_is_still_pushed() -> None:
    pusher, spawn, clock, fingerprint, _ = live()
    fingerprint.bump()
    pusher.tick()  # pushed at once
    clock.now += 0.1
    settled = clock.now
    fingerprint.bump()
    pusher.tick()
    assert len(spawn.calls) == 1
    clock.now = settled + 3
    pusher.tick()
    assert len(spawn.calls) == 1
    clock.now = settled + 4.9
    pusher.tick()
    assert len(spawn.calls) == 1
    clock.now = settled + LIVE_MIN_SECONDS
    pusher.tick()
    assert len(spawn.calls) == 2
    clock.now += 5
    pusher.tick()  # nothing new since
    assert len(spawn.calls) == 2


def test_changes_during_a_running_push_wait_for_it_and_go_out_once() -> None:
    pusher, spawn, clock, fingerprint, lines = live(spawn=Spawn())
    fingerprint.bump()
    pusher.tick()
    for _ in range(10):  # the scp is slow; changes keep coming
        clock.now += 1
        fingerprint.bump()
        pusher.tick()
    assert len(spawn.calls) == 1
    spawn.children[0].returncode = 0
    clock.now += 0.25
    pusher.tick()
    assert len(spawn.calls) == 1
    clock.now += LIVE_MIN_SECONDS
    pusher.tick()
    assert len(spawn.calls) == 2
    assert lines == []


def test_the_end_of_a_poll_cycle_pushes_the_full_pair_then_status_only() -> None:
    pusher, spawn, clock, _, _ = live(
        PushConfig(**{**LIVE_PUSH.__dict__, "every_minutes": 5})
    )
    kinds: list[tuple[float, str]] = []
    for _ in range(11):  # one cycle a minute for ten minutes
        before = len(spawn.calls)
        pusher()  # the poll loop's after_poll
        pusher.tick()  # the watcher's next look
        for argv in spawn.argvs()[before:]:
            kinds.append((clock.now - 1000, "status" if status_only(argv) else "pair"))
        clock.now += 60
    pairs = [at for at, kind in kinds if kind == "pair"]
    assert pairs == [0, 300, 600]
    # Every cycle end is a change: the other eight cycles push status.json.
    assert [at for at, kind in kinds if kind == "status"] == [
        60, 120, 180, 240, 360, 420, 480, 540,
    ]


def test_a_cycle_end_is_pushed_even_while_a_change_push_runs() -> None:
    pusher, spawn, clock, fingerprint, lines = live(spawn=Spawn())
    fingerprint.bump()
    pusher.tick()  # status-only push, still running
    clock.now += 2
    pusher()  # first cycle end: the full pair is due
    assert len(spawn.calls) == 1
    spawn.children[0].returncode = 0
    pusher.tick()
    assert len(spawn.calls) == 1
    clock.now += LIVE_MIN_SECONDS
    pusher.tick()
    assert len(spawn.calls) == 2
    assert not status_only(spawn.argvs()[1])
    assert lines == []


def test_pushes_carry_the_last_cycle_time_for_poll_health() -> None:
    wall = Clock(1_800_000_000.0)
    pusher, spawn, clock, fingerprint, _ = live(wall=wall)
    fingerprint.bump()
    pusher.tick()
    assert "--last-cycle" not in spawn.argvs()[0]  # no cycle has ended yet
    pusher.tick()
    clock.now += 5
    wall.now += 5
    pusher()
    argv = spawn.argvs()[1]
    assert argv[argv.index("--last-cycle") + 1] == "1800000005.000"
    pusher.tick()
    clock.now += 5
    fingerprint.bump()
    pusher.tick()
    argv = spawn.argvs()[2]
    assert argv[argv.index("--last-cycle") + 1] == "1800000005.000"


def test_a_failed_spawn_is_logged_redacted_at_most_once_a_minute() -> None:
    pusher, spawn, clock, fingerprint, lines = live(
        spawn=Spawn(OSError(f"cannot exec {GH} {HOOK}"))
    )
    for _ in range(30):
        fingerprint.bump()
        pusher.tick()
        clock.now += 1
    assert len(spawn.calls) == 6
    assert 1 <= len(lines) <= 2
    for line in lines:
        assert line.startswith("dashboard push: ")
        assert GH not in line and HOOK not in line


def test_a_broken_fingerprint_never_raises() -> None:
    def broken(path: Path) -> int:
        raise OSError(f"stat failed {GH}")

    pusher, spawn, _, _, lines = live(fingerprint=broken)
    pusher.tick()
    assert spawn.calls == []
    assert len(lines) == 1 and GH not in lines[0]


def test_the_watcher_thread_pushes_on_change_and_stops() -> None:
    fingerprint = Fingerprint()
    pusher, spawn, _, _, _ = live(
        fingerprint=fingerprint, clock=time.monotonic, wall=time.time
    )
    pusher.start(interval=0.01)
    try:
        assert pusher.watching
        fingerprint.bump()
        deadline = time.monotonic() + 5
        while not spawn.calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(spawn.calls) == 1
    finally:
        pusher.stop()
    assert not pusher.watching
    assert not any(
        thread.name == "dashboard-live-watch" for thread in threading.enumerate()
    )


# --- what counts as a change -------------------------------------------------------


def _queue(tmp_path: Path) -> tuple[Path, SQLiteJobQueue]:
    db = tmp_path / "orch.db"
    clock = iter(range(1_800_000_000, 1_800_100_000, 7))
    queue = SQLiteJobQueue(db, clock=lambda: float(next(clock)))
    return db, queue


def _changed(db: Path, action) -> bool:
    before = db_fingerprint(db)
    action()
    return db_fingerprint(db) != before


def test_claim_heartbeat_finish_and_verdict_each_change_the_fingerprint(
    tmp_path: Path,
) -> None:
    db, queue = _queue(tmp_path)
    request = JobRequest(
        repo="maxharris1/factory",
        ticket_id="58",
        action="review",
        recipient="codex",
        source_comment_id="c1",
        timeout_seconds=600,
    )
    assert _changed(db, lambda: queue.enqueue(request, dedupe_key="k1"))
    job = queue.claim("codex")
    assert job is not None
    assert _changed(db, lambda: queue.heartbeat(job.id, owner="codex"))
    assert _changed(db, lambda: queue.heartbeat(job.id, owner="codex"))

    def verdict() -> None:
        with sqlite3.connect(db) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS handled_comments (repo TEXT, ticket_id TEXT, "
                "comment_id TEXT, outcome TEXT, PRIMARY KEY (repo, ticket_id, comment_id))"
            )
            connection.execute(
                "INSERT INTO handled_comments VALUES ('maxharris1/factory', '58', 'c2', "
                "'queued-review-fix')"
            )
        connection.close()

    assert _changed(db, verdict)
    assert _changed(db, lambda: queue.finish(job.id, owner="codex", handoff_comment_id="c3"))
    queue.enqueue(
        JobRequest(**{**request.__dict__, "source_comment_id": "c4"}), dedupe_key="k2"
    )
    assert _changed(db, lambda: queue.claim("codex"))


def test_reading_a_snapshot_does_not_change_the_fingerprint(tmp_path: Path) -> None:
    """No feedback loop: the push child's own read-only snapshot is not a change."""
    db, queue = _queue(tmp_path)
    queue.enqueue(
        JobRequest(
            repo="maxharris1/factory",
            ticket_id="58",
            action="build",
            recipient="claude",
            source_comment_id="c1",
            timeout_seconds=600,
        ),
        dedupe_key="k1",
    )
    # The queue leaves its connections to the garbage collector; closing the
    # last one checkpoints the WAL (a real writer-side change). Settle that
    # first, so only the reader is under test.
    del queue
    gc.collect()
    before = db_fingerprint(db)
    for _ in range(3):
        collect_snapshot(db, tmp_path / "poller.log", now=1_800_000_100.0)
    assert db_fingerprint(db) == before


def test_fingerprint_of_a_missing_database_is_stable(tmp_path: Path) -> None:
    assert db_fingerprint(tmp_path / "missing.db") == db_fingerprint(tmp_path / "missing.db")


# --- `orch poll` wiring --------------------------------------------------------------

POLL_ORCH = """\
poll_seconds: 60
projects:
  - repo: maxharris1/factory
dashboard:
  push:
    transport: ssh
    host: pc-wsl-cursor-bot
    method: scp
    live: true
"""


def _run_poll(tmp_path: Path, monkeypatch, *flags: str) -> tuple[dict, list[str]]:
    orch = tmp_path / "orch.yml"
    orch.write_text(POLL_ORCH, encoding="utf-8")
    database = tmp_path / "orch.db"
    database.write_bytes(b"")
    seen: dict = {}
    events: list[str] = []

    class Loop:
        def __init__(self, dispatcher, source, **_kwargs) -> None:
            pass

        def cycle_state(self):
            return None, None

        def run(self, **kwargs) -> None:
            events.append("run")
            seen.update(kwargs)

    monkeypatch.delenv("ORCH_DB", raising=False)
    monkeypatch.setattr(cli.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(cli, "_runtime", lambda path, orch, create, **_: (object(), object()))
    monkeypatch.setattr(cli, "PollLoop", Loop)
    monkeypatch.setattr(LivePusher, "start", lambda self, **_: events.append("start"))
    monkeypatch.setattr(LivePusher, "stop", lambda self: events.append("stop"))
    monkeypatch.setattr(LivePusher, "on_timer", lambda self, every: None)
    code = cli.main(["poll", "--db", str(database), "--orch", str(orch),
                     "--pidfile", str(tmp_path / "poller.pid"), *flags])
    assert code == 0
    return seen, events


def test_orch_poll_with_live_push_starts_and_stops_the_watcher(
    tmp_path: Path, monkeypatch
) -> None:
    seen, events = _run_poll(tmp_path, monkeypatch)
    assert isinstance(seen["after_poll"].pusher, LivePusher)
    assert seen["after_poll"].pusher.db == tmp_path / "orch.db"
    assert events == ["start", "run", "stop"]


def test_orch_poll_once_does_not_start_the_watcher(tmp_path: Path, monkeypatch) -> None:
    seen, events = _run_poll(tmp_path, monkeypatch, "--once")
    assert isinstance(seen["after_poll"].pusher, LivePusher)
    assert events == ["run"]


# --- `orch dashboard push --status-only` end to end, with fake scp and ssh ----------

FAKE_SCP = """\
import os, shutil, sys
from pathlib import Path
log = Path(os.environ["FAKE_REMOTE_LOG"])
with log.open("a") as handle:
    handle.write(repr(["scp", *sys.argv[1:]]) + "\\n")
args = sys.argv[1:]
source, destination = args[args.index("--") + 1:]
target = Path(os.environ["FAKE_REMOTE_HOME"]) / destination.split(":", 1)[1].lstrip("/")
shutil.copyfile(source, target)
"""
FAKE_SSH = """\
import os, sys
from pathlib import Path
log = Path(os.environ["FAKE_REMOTE_LOG"])
with log.open("a") as handle:
    handle.write(repr(["ssh", *sys.argv[1:]]) + "\\n")
args = sys.argv[1:]
command = args[args.index("--") + 2]
steps = command.split(" && ")
here = Path(os.environ["FAKE_REMOTE_HOME"]) / steps[0].split()[2].lstrip("/")
for step in steps[1:]:
    words = step.split()
    assert words[:3] == ["mv", "-f", "--"], command
    os.replace(here / words[3], here / words[4])
"""


def _fake_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    for name, body in (("scp", FAKE_SCP), ("ssh", FAKE_SSH)):
        path = bin_dir / name
        path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def test_cli_status_only_push_publishes_status_json_atomically(tmp_path: Path) -> None:
    db, queue = _queue(tmp_path)
    queue.enqueue(
        JobRequest(
            repo="maxharris1/factory",
            ticket_id="58",
            action="build",
            recipient="claude",
            source_comment_id="c1",
            timeout_seconds=600,
        ),
        dedupe_key="k1",
    )
    orch = tmp_path / "orch.yml"
    orch.write_text(
        "poll_seconds: 60\nprojects:\n  - repo: maxharris1/factory\n"
        "dashboard:\n  push:\n    transport: ssh\n    host: pc-wsl-cursor-bot\n"
        "    remote_dir: factory-dashboard\n    method: scp\n    live: true\n",
        encoding="utf-8",
    )
    remote_home = tmp_path / "remote-home"
    site = remote_home / "factory-dashboard"
    site.mkdir(parents=True)
    (site / "index.html").write_text("<!doctype html><p>published page</p>", encoding="utf-8")
    (site / "status.json").write_text('{"old": true}', encoding="utf-8")
    remote_log = tmp_path / "remote.log"
    env = {key: value for key, value in os.environ.items() if not key.startswith("ORCH_")}
    env.update(
        PATH=f"{_fake_bin(tmp_path)}{os.pathsep}{env.get('PATH', '')}",
        FAKE_REMOTE_HOME=str(remote_home),
        FAKE_REMOTE_LOG=str(remote_log),
    )
    out = tmp_path / "local-site"
    result = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "orch"), "dashboard", "push", "--once",
         "--status-only", "--last-cycle", "1800000000.5", "--db", str(db),
         "--orch", str(orch), "--out", str(out), "--log", str(tmp_path / "poller.log")],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    calls = [eval(line) for line in remote_log.read_text().splitlines()]  # noqa: S307
    assert [call[0] for call in calls] == ["scp", "ssh"]
    assert calls[0][-2] == str(out / "status.json")
    assert calls[0][-1] == "pc-wsl-cursor-bot:factory-dashboard/.status.json.push-tmp"
    assert calls[1][-1] == (
        "cd -- factory-dashboard && mv -f -- .status.json.push-tmp status.json"
    )
    # Published: the new status.json, the page left alone, no temp file behind.
    assert sorted(path.name for path in site.iterdir()) == ["index.html", "status.json"]
    assert (site / "index.html").read_text() == "<!doctype html><p>published page</p>"
    published = json.loads((site / "status.json").read_text(encoding="utf-8"))
    assert published == json.loads((out / "status.json").read_text(encoding="utf-8"))
    assert published["queue"]["totals"]["queued"] == 1
    assert published["poller"]["health"]["last_cycle_age_seconds"] is not None


def test_cli_rejects_a_bad_last_cycle(tmp_path: Path) -> None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("ORCH_")}
    for value in ("soon", "nan", "-5", "inf"):
        result = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "orch"), "dashboard", "push", "--once",
             "--status-only", "--last-cycle", value, "--db", str(tmp_path / "x.db")],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert result.returncode == 2, (value, result.stderr)
        assert "--last-cycle" in result.stderr


def test_a_failed_live_push_backs_off_and_keeps_the_pending_change() -> None:
    pusher, spawn, clock, fingerprint, lines = live(spawn=Spawn())
    outcomes: list[bool] = []
    pusher.record = outcomes.append
    fingerprint.bump()
    pusher.tick()
    assert len(spawn.calls) == 1
    spawn.children[0].returncode = 255
    for _ in range(100):  # 25 s of changes while the backoff runs
        clock.now += 0.25
        fingerprint.bump()
        pusher.tick()
    assert len(spawn.calls) == 1
    assert lines == ["dashboard push: failed (exit 255), 1 in a row; pushes paused for 30s"]
    clock.now = 1000 + 30
    pusher.tick()  # no new change, but the pending one goes out now
    assert len(spawn.calls) == 2
    spawn.children[1].returncode = 0
    clock.now += 0.25
    pusher.tick()
    fingerprint.bump()
    clock.now += LIVE_MIN_SECONDS
    pusher.tick()
    assert len(spawn.calls) == 3
    assert outcomes == [False, True]
