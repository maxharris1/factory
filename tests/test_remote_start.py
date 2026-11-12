"""F2-02: remote job start over the device pool (docs/SPEC.md F2-02).

Every test here uses a fake SSH transport: tests/fake_ssh.py on PATH for
the CLI, or FakeTransport in process. No test reaches a real host.
"""

from __future__ import annotations

import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lib import remote
from lib.device_monitor import DeviceMonitor
from lib.device_pool import DevicePool
from lib.dispatcher import Dispatcher
from lib.machines_yml import MachinesConfig, load_machines_yml
from lib.queue import JobRequest, SQLiteJobQueue
from lib.wait import WaitError, deliver, wait_for_job
from tests.test_dispatcher import (
    GO,
    TRUSTED_LOGINS,
    FakeGrokDelivery,
    FakeReviewActions,
    comment,
    ticket,
)

ROOT = Path(__file__).resolve().parents[1]
ORCH = ROOT / "bin" / "orch"
FAKE_SSH = ROOT / "tests" / "fake_ssh.py"
BOTS = ("claude-bot", "codex-bot", "cursor-bot")


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_signals():
    """wait_for_job ignores SIGINT/SIGTERM once it delivers; children inherit that."""
    saved = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
    yield
    for signum, handler in saved.items():
        signal.signal(signum, handler)
    signal.pthread_sigmask(signal.SIG_SETMASK, mask)


def machines_file(tmp_path: Path, *, box_enabled: bool = True, box_slots: int = 1) -> Path:
    path = tmp_path / "machines.yml"
    path.write_text(
        f"""
devices:
  - name: box
    local: true
    enabled: {str(box_enabled).lower()}
    slot_max: {box_slots}
    harnesses: {{claude: claude-bot, codex: codex-bot, cursor: cursor-bot}}
  - name: pc
    tailscale_name: pc.example.ts.net
    slot_max: 3
    harnesses: {{claude: claude-bot, codex: codex-bot, cursor: cursor-bot}}
    workdir_root: {tmp_path}/remote/{{bot}}/work
    ssh_alias: fakepc-{{bot}}
""".lstrip(),
        encoding="utf-8",
    )
    return path


def config_of(path: Path) -> MachinesConfig:
    config, findings = load_machines_yml(path, allow_placeholders=True)
    assert config is not None, findings
    return config


class Ssh:
    """tests/fake_ssh.py as `ssh` first on PATH, with one alias per bot."""

    def __init__(self, tmp_path: Path):
        self.bin = tmp_path / "fake-bin"
        self.bin.mkdir()
        self.hosts = tmp_path / "fake-hosts"
        self.hosts.mkdir()
        self.log = tmp_path / "ssh.log"
        self.log.touch()
        wrapper = self.bin / "ssh"
        wrapper.write_text(f'#!/bin/sh\nexec {sys.executable} {FAKE_SSH} "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)
        for bot in BOTS:
            (self.hosts / f"fakepc-{bot}").write_text(bot, encoding="utf-8")

    def env(self) -> dict[str, str]:
        return {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_SSH_LOG": str(self.log),
            "FAKE_SSH_HOSTS": str(self.hosts),
        }

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def offline(self, alias: str) -> Path:
        return self.hosts / f"{alias}.offline"


def enqueue(queue: SQLiteJobQueue, recipient: str, key: str, *, timeout: int = 60) -> str:
    return queue.enqueue(
        JobRequest(
            repo="owner/repo",
            ticket_id=key,
            action="build",
            recipient=recipient,
            source_comment_id=f"go-{key}",
            timeout_seconds=timeout,
        ),
        dedupe_key=key,
    ).id


def orch(*args: str, env: dict[str, str] | None = None, **kwargs) -> subprocess.Popen:
    full = {k: v for k, v in os.environ.items() if k != "ORCH_DB"}
    full.update(env or {})
    return subprocess.Popen(
        [sys.executable, str(ORCH), *args],
        env=full,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **kwargs,
    )


def state(queue: SQLiteJobQueue, job_id: str) -> str:
    with queue.connect() as connection:
        return str(connection.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()[0])


def remote_setup(tmp_path: Path, recipient: str):
    """A queued job placed on the pc (box disabled), and the fake ssh."""
    machines = machines_file(tmp_path, box_enabled=False)
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=time.time)
    job_id = enqueue(queue, recipient, "1")
    pool = DevicePool(queue, config_of(machines))
    claimed = pool.claim(recipient)
    assert claimed is not None and claimed[1].device == "pc"
    return machines, queue, pool, job_id, Ssh(tmp_path)


# --- machines.yml: config-driven alias per bot -------------------------------


def _remote_yaml(**fields: str) -> str:
    values = {
        "tailscale_name": "pc.example.ts.net",
        "slot_max": "2",
        "harnesses": "{codex: codex-bot}",
        "workdir_root": "/home/work",
        "ssh_alias": "pc-wsl-{bot}",
        **fields,
    }
    lines = ["devices:", "  - name: pc"]
    lines.extend(f"    {key}: {value}" for key, value in values.items() if value != "-")
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize(
    ("alias", "message"),
    [
        ("-", "missing required field 'devices[0].ssh_alias'"),
        ("pc-wsl-codex-bot", "must contain '{bot}'"),
        ("codex-bot@pc-{bot}", "must be a bare SSH Host alias"),
        ("'-oProxyCommand=sh {bot}'", "must be a bare SSH Host alias"),
        ("'pc {bot}'", "must be a bare SSH Host alias"),
        ("'pc-{bot};rm'", "must be a bare SSH Host alias"),
        ("'pc-{user}-{bot}'", "must be a bare SSH Host alias"),
    ],
)
def test_remote_device_needs_a_bare_ssh_alias_naming_its_bot(
    tmp_path: Path, alias: str, message: str
) -> None:
    path = tmp_path / "machines.yml"
    path.write_text(_remote_yaml(ssh_alias=alias), encoding="utf-8")
    config, findings = load_machines_yml(path)
    assert config is None
    assert any(message in item.message for item in findings), findings


def test_the_local_device_names_no_ssh_alias(tmp_path: Path) -> None:
    path = tmp_path / "machines.yml"
    path.write_text(
        "devices:\n  - name: box\n    local: true\n    slot_max: 1\n"
        "    harnesses: {claude: claude-bot}\n    ssh_alias: box-{bot}\n",
        encoding="utf-8",
    )
    config, findings = load_machines_yml(path)
    assert config is None
    assert [item.message for item in findings] == [
        "devices[0].ssh_alias must be omitted on the local device"
    ]


def test_a_placeholder_alias_is_reported_and_never_scheduled(tmp_path: Path) -> None:
    path = tmp_path / "machines.yml"
    path.write_text(_remote_yaml(ssh_alias="REPLACE_WITH_PC_SSH_ALIAS"), encoding="utf-8")
    assert load_machines_yml(path)[0] is None
    config, findings = load_machines_yml(path, allow_placeholders=True)
    assert config is not None
    assert "accepted placeholder devices[0].ssh_alias" in findings[0].message
    pc = config.device("pc")
    assert pc is not None and not pc.eligible("codex") and pc.ssh_alias_for("codex") is None


def test_shipped_pc_resolves_each_bot_to_its_own_box_alias_and_stays_disabled() -> None:
    config = config_of(ROOT / "machines.yml")
    pc, mac, box = config.device("pc"), config.device("mac"), config.device("box")
    assert pc is not None and mac is not None and box is not None
    assert {harness: pc.ssh_alias_for(harness) for harness in pc.harnesses} == {
        "claude": "pc-wsl-claude-bot",
        "codex": "pc-wsl-codex-bot",
        "cursor": "pc-wsl-cursor-bot",
    }
    assert (pc.enabled, mac.enabled) == (False, False)
    assert box.ssh_alias_for("claude") is None
    assert mac.ssh_alias_for("claude") is None


def test_ssh_argv_logs_in_as_the_bot_through_its_alias_and_names_no_key() -> None:
    target = remote.RemoteTarget(
        device="pc",
        alias="pc-wsl-codex-bot",
        user="codex-bot",
        workdir_root="/home/codex-bot/factory-work",
        job_id="abc123",
    )
    argv = remote.ssh_argv(target, "true")
    assert argv[0] == "ssh"
    assert argv[-4:] == ["codex-bot", "--", "pc-wsl-codex-bot", "true"]
    assert argv[-5] == "-l"
    assert "BatchMode=yes" in argv
    joined = " ".join(argv)
    for forbidden in ("-i", "IdentityFile", ".ssh", "@", "-F"):
        assert forbidden not in argv and forbidden not in joined.split()
    assert ".ssh" not in joined and "IdentityFile" not in joined


@pytest.mark.parametrize(
    "fields",
    [
        {"alias": "-oProxyCommand=sh"},
        {"alias": "codex-bot@pc"},
        {"user": "-froot"},
        {"job_id": "../../etc"},
        {"workdir_root": "relative"},
    ],
)
def test_remote_target_refuses_option_or_path_injection(fields: dict) -> None:
    values = {
        "device": "pc",
        "alias": "pc-wsl-codex-bot",
        "user": "codex-bot",
        "workdir_root": "/home/codex-bot/factory-work",
        "job_id": "abc123",
        **fields,
    }
    with pytest.raises(remote.RemoteError):
        remote.RemoteTarget(**values)


# --- orch wait claims through the device pool --------------------------------


def test_orch_wait_claims_a_box_slot_and_names_the_device(tmp_path: Path) -> None:
    machines = machines_file(tmp_path, box_slots=1)
    db = tmp_path / "orch.db"
    queue = SQLiteJobQueue(db, clock=time.time)
    first = enqueue(queue, "claude", "1")
    proc = orch("wait", "claude-bot", "--db", str(db), "--machines", str(machines))
    out, err = proc.communicate(timeout=20)
    assert proc.returncode == 0, err
    line = json.loads(out)
    assert (line["id"], line["device"], line["remote"]) == (first, "box", False)
    assert "workdir" not in line
    pool = DevicePool(queue, config_of(machines))
    placement = pool.placement(first)
    assert placement is not None
    assert (placement.device, placement.os_user) == ("box", "claude-bot")


def test_orch_wait_keeps_waiting_while_no_eligible_slot_is_free(tmp_path: Path) -> None:
    machines = machines_file(tmp_path, box_enabled=True, box_slots=1)
    text = machines.read_text(encoding="utf-8").replace("  - name: pc\n", "  - name: pc\n    enabled: false\n")
    machines.write_text(text, encoding="utf-8")
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=time.time)
    pool = DevicePool(queue, config_of(machines))
    first = enqueue(queue, "claude", "1")
    second = enqueue(queue, "claude", "2")
    out = io.StringIO()
    assert wait_for_job(queue, "claude", out, pool=pool).id == first

    class Stop(Exception):
        pass

    def sleep(_seconds: float) -> None:
        raise Stop

    with pytest.raises(Stop):
        wait_for_job(queue, "claude", io.StringIO(), pool=pool, sleep=sleep)
    assert state(queue, second) == "queued"
    assert pool.used() == {"box": 1}


def test_orch_wait_places_on_the_remote_device_once_the_box_is_full(tmp_path: Path) -> None:
    machines = machines_file(tmp_path, box_slots=1)
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=time.time)
    pool = DevicePool(queue, config_of(machines))
    enqueue(queue, "codex", "1")
    second = enqueue(queue, "codex", "2")
    wait_for_job(queue, "codex", io.StringIO(), pool=pool)
    out = io.StringIO()
    wait_for_job(queue, "codex", out, pool=pool)
    line = json.loads(out.getvalue())
    assert (line["id"], line["device"], line["remote"]) == (second, "pc", True)
    assert line["workdir"] == f"{tmp_path}/remote/codex-bot/work/jobs/{second}"


def test_an_undelivered_job_is_failed_and_its_slot_released(tmp_path: Path) -> None:
    machines = machines_file(tmp_path)
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=time.time)
    pool = DevicePool(queue, config_of(machines))
    job_id = enqueue(queue, "claude", "1")
    job, placement = pool.claim("claude")

    class Broken(io.StringIO):
        def write(self, _text: str) -> int:
            raise BrokenPipeError("closed")

    with pytest.raises(WaitError):
        deliver(queue, job, Broken(), pool=pool, placement=placement)
    assert state(queue, job_id) == "failed"
    assert pool.placement(job_id) is None


# --- orch job run on a remote device -----------------------------------------


@pytest.mark.parametrize("bot", BOTS)
def test_remote_job_starts_over_the_bots_own_alias_as_that_bot(tmp_path: Path, bot: str) -> None:
    recipient = bot.removesuffix("-bot")
    machines, queue, pool, job_id, ssh = remote_setup(tmp_path, recipient)
    report = tmp_path / "report.json"
    script = (
        "import json, os, sys\n"
        f"keys = ('ORCH_JOB_ID', 'ORCH_JOB_BOT', 'ORCH_JOB_DEVICE', 'ORCH_JOB_WORKDIR', 'ORCH_DB')\n"
        f"json.dump({{'cwd': os.getcwd(), **{{k: os.environ.get(k) for k in keys}}}}, open({str(report)!r}, 'w'))\n"
        "sys.exit(3)\n"
    )
    proc = orch(
        "job", "run", bot, job_id, "--db", str(tmp_path / "orch.db"), "--machines", str(machines),
        "--", sys.executable, "-c", script,
        env=ssh.env(),
    )
    _, err = proc.communicate(timeout=30)

    assert proc.returncode == 3, err
    workdir = f"{tmp_path}/remote/{bot}/work/jobs/{job_id}"
    seen = json.loads(report.read_text(encoding="utf-8"))
    assert seen == {
        "cwd": workdir,
        "ORCH_JOB_ID": job_id,
        "ORCH_JOB_BOT": bot,
        "ORCH_JOB_DEVICE": "pc",
        "ORCH_JOB_WORKDIR": workdir,
        "ORCH_DB": None,
    }
    calls = ssh.calls()
    assert len(calls) == 2  # the start, then the check that the worker is gone
    for call in calls:
        assert (call["user"], call["alias"]) == (bot, f"fakepc-{bot}")
        assert "BatchMode=yes" in call["options"]
    assert sys.executable in calls[0]["script"]
    assert "kill -0" in calls[1]["script"]
    assert pool.placement(job_id) is None  # released: the worker is gone
    assert state(queue, job_id) == "running"  # job run never finishes the job


def test_only_the_owning_bot_starts_a_remote_job(tmp_path: Path) -> None:
    machines, queue, pool, job_id, ssh = remote_setup(tmp_path, "codex")
    marker = tmp_path / "ran"
    proc = orch(
        "job", "run", "claude-bot", job_id, "--db", str(tmp_path / "orch.db"),
        "--machines", str(machines), "--", "touch", str(marker),
        env=ssh.env(),
    )
    _, err = proc.communicate(timeout=20)
    assert proc.returncode == 2
    assert "belongs to codex-bot" in err
    assert ssh.calls() == []
    assert not marker.exists()
    assert pool.placement(job_id) is not None


def test_remote_job_refuses_worktree_starts_nothing_and_keeps_its_slot(tmp_path: Path) -> None:
    machines, queue, pool, job_id, ssh = remote_setup(tmp_path, "codex")
    proc = orch(
        "job", "run", "codex-bot", job_id, "--db", str(tmp_path / "orch.db"),
        "--machines", str(machines), "--worktree", "--", "true",
        env=ssh.env(),
    )
    _, err = proc.communicate(timeout=20)
    assert proc.returncode == 2
    assert "--worktree is box-only" in err
    assert ssh.calls() == []
    assert pool.placement(job_id) is not None
    assert not pool.placement(job_id).remote_started


def test_killed_remote_job_stops_its_worker_before_the_slot_is_released(tmp_path: Path) -> None:
    machines, queue, pool, job_id, ssh = remote_setup(tmp_path, "codex")
    pid_file = tmp_path / "worker.pid"
    script = (
        "import os, time\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    proc = orch(
        "job", "run", "codex-bot", job_id, "--db", str(tmp_path / "orch.db"),
        "--machines", str(machines), "--", sys.executable, "-c", script,
        env=ssh.env(),
    )
    deadline = time.monotonic() + 20
    while not pid_file.exists() or not pid_file.read_text():
        assert time.monotonic() < deadline, proc.stderr.read() if proc.poll() is not None else ""
        time.sleep(0.05)
    worker = int(pid_file.read_text())
    assert pool.placement(job_id).remote_started

    proc.send_signal(signal.SIGTERM)
    _, err = proc.communicate(timeout=30)

    assert proc.returncode == 128 + signal.SIGTERM, err
    assert "stopping it" in err
    with pytest.raises(ProcessLookupError):
        os.kill(worker, 0)
    scripts = [call["script"] for call in ssh.calls()]
    assert any("kill -TERM" in item for item in scripts)
    assert pool.placement(job_id) is None


def test_an_unreachable_device_after_the_job_keeps_the_slot(tmp_path: Path) -> None:
    machines, queue, pool, job_id, ssh = remote_setup(tmp_path, "codex")
    offline = ssh.offline("fakepc-codex-bot")
    proc = orch(
        "job", "run", "codex-bot", job_id, "--db", str(tmp_path / "orch.db"),
        "--machines", str(machines), "--", "touch", str(offline),
        env=ssh.env(),
    )
    _, err = proc.communicate(timeout=20)
    assert proc.returncode == 0, err
    assert "not confirmed gone (unreachable); slot held" in err
    assert pool.placement(job_id) is not None


# --- orch job run on the box --------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [("pass", 0), ("import sys; sys.exit(1)", 1), ("import os, signal; os.kill(os.getpid(), signal.SIGKILL)", 137)],
    ids=["done", "failed", "killed"],
)
def test_box_job_releases_its_slot_once_the_command_has_ended(
    tmp_path: Path, source: str, expected: int
) -> None:
    machines = machines_file(tmp_path)
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=time.time)
    pool = DevicePool(queue, config_of(machines))
    job_id = enqueue(queue, "claude", "1")
    assert pool.claim("claude")[1].device == "box"
    proc = orch(
        "job", "run", "claude-bot", job_id, "--db", str(tmp_path / "orch.db"),
        "--machines", str(machines), "--", sys.executable, "-c", source,
    )
    _, err = proc.communicate(timeout=20)
    assert proc.returncode == expected, err
    assert pool.placement(job_id) is None
    assert f"released box slot of job {job_id}" in err


def test_a_job_without_a_slot_is_refused_and_nothing_runs(tmp_path: Path) -> None:
    machines = machines_file(tmp_path)
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=time.time)
    job_id = enqueue(queue, "claude", "1")
    assert queue.claim("claude") is not None  # the pre-F2-02 claim: no slot
    marker = tmp_path / "ran"
    proc = orch(
        "job", "run", "claude-bot", job_id, "--db", str(tmp_path / "orch.db"),
        "--machines", str(machines), "--", "touch", str(marker),
    )
    _, err = proc.communicate(timeout=20)
    assert proc.returncode == 2
    assert "holds no device slot" in err
    assert not marker.exists()


def test_a_second_runner_is_refused_while_the_first_still_runs(tmp_path: Path) -> None:
    machines = machines_file(tmp_path)
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=time.time)
    pool = DevicePool(queue, config_of(machines))
    job_id = enqueue(queue, "claude", "1")
    pool.claim("claude")
    started = tmp_path / "started"
    first = orch(
        "job", "run", "claude-bot", job_id, "--db", str(tmp_path / "orch.db"),
        "--machines", str(machines), "--", "sh", "-c", f"touch {started}; exec sleep 30",
    )
    deadline = time.monotonic() + 20
    while not started.exists():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    second = orch(
        "job", "run", "claude-bot", job_id, "--db", str(tmp_path / "orch.db"),
        "--machines", str(machines), "--", "true",
    )
    _, err = second.communicate(timeout=20)
    first.send_signal(signal.SIGTERM)
    first.communicate(timeout=20)
    assert second.returncode == 2
    assert f"already run by pid {first.pid}" in err
    assert pool.placement(job_id) is None  # the first runner released it on exit


# --- DeviceMonitor: release only once the worker is gone ----------------------


class FakeTransport:
    """Scripted SSH results: 0 gone, 1 running, 255 unreachable."""

    def __init__(self, check: int = 0):
        self.check = check
        self.calls: list[tuple[str, str, str]] = []

    def run(self, argv, *, timeout: float) -> int:
        assert argv[0] == "ssh" and timeout > 0
        user, alias, script = argv[-4], argv[-2], argv[-1]
        kind = "stop" if "kill -TERM" in script else "check"
        self.calls.append((kind, user, alias))
        if kind == "stop":
            return 255 if self.check == 255 else 0
        return self.check


def monitor_setup(tmp_path: Path, recipient: str, *, box: bool, now: list[float] | None = None):
    machines = machines_file(tmp_path, box_enabled=box)
    clock = now if now is not None else [1000.0]
    queue = SQLiteJobQueue(tmp_path / "d.db", clock=lambda: clock[0])
    config = config_of(machines)
    pool = DevicePool(queue, config)
    job_id = enqueue(queue, recipient, "1")
    pool.claim(recipient)
    return queue, pool, config, job_id


def test_monitor_releases_a_finished_box_slot_only_once_runner_and_worker_are_gone(
    tmp_path: Path,
) -> None:
    queue, pool, config, job_id = monitor_setup(tmp_path, "claude", box=True)
    alive = {11: True, 22: True}
    pool.attach_runner(job_id, owner="claude", pid=11, start="s", alive=lambda *_: False)
    pool.attach_worker(job_id, owner="claude", pid=22, start="w")
    monitor = DeviceMonitor(pool, config, FakeTransport(), alive=lambda pid, _s: alive.get(pid, False))
    offline: list = []

    monitor.reconcile(1000.0, lambda *args: offline.append(args))
    assert pool.placement(job_id) is not None  # still running: never released

    queue.finish(job_id, owner="claude", handoff_comment_id="c1")
    monitor.reconcile(1000.0, lambda *args: offline.append(args))
    assert pool.placement(job_id) is not None  # runner alive

    alive[11] = False
    monitor.reconcile(1000.0, lambda *args: offline.append(args))
    assert pool.placement(job_id) is not None  # worker alive

    alive[22] = False
    result = monitor.reconcile(1000.0, lambda *args: offline.append(args))
    assert result.released == (job_id,)
    assert pool.placement(job_id) is None
    assert offline == []


def test_monitor_releases_a_remote_slot_only_when_the_device_says_the_worker_is_gone(
    tmp_path: Path,
) -> None:
    queue, pool, config, job_id = monitor_setup(tmp_path, "codex", box=False)
    pool.attach_worker(job_id, owner="codex", remote=True)
    queue.finish(job_id, owner="codex", handoff_comment_id=None)
    transport = FakeTransport(check=255)
    monitor = DeviceMonitor(pool, config, transport, alive=lambda *_: False)

    monitor.reconcile(1000.0, lambda *_: None)
    assert pool.placement(job_id) is not None  # unreachable: kept
    assert transport.calls == [("check", "codex-bot", "fakepc-codex-bot")]

    transport.check = 1
    result = monitor.reconcile(1060.0, lambda *_: None)
    assert result.stopped == (job_id,)  # an orphan with no runner is stopped
    assert pool.placement(job_id) is not None
    assert transport.calls[-1] == ("stop", "codex-bot", "fakepc-codex-bot")

    transport.check = 0
    result = monitor.reconcile(1120.0, lambda *_: None)
    assert result.released == (job_id,)
    assert pool.placement(job_id) is None


def test_monitor_releases_a_remote_slot_no_worker_was_started_for_without_ssh(
    tmp_path: Path,
) -> None:
    queue, pool, config, job_id = monitor_setup(tmp_path, "codex", box=False)
    queue.finish(job_id, owner="codex", handoff_comment_id=None)
    transport = FakeTransport(check=255)
    result = DeviceMonitor(pool, config, transport, alive=lambda *_: False).reconcile(
        1000.0, lambda *_: None
    )
    assert result.released == (job_id,)
    assert transport.calls == []


# --- 60 minutes offline: stuck, Grok woken once --------------------------------


def offline_setup(tmp_path: Path, check: int = 255):
    now = [0.0]
    path = tmp_path / "d.db"
    queue = SQLiteJobQueue(path, clock=lambda: now[0])
    machines = machines_file(tmp_path, box_enabled=False)
    config = config_of(machines)
    pool = DevicePool(queue, config)
    transport = FakeTransport(check=check)
    grok = FakeGrokDelivery()
    dispatcher = Dispatcher(
        path,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=FakeReviewActions(),
        grok_delivery=grok,
        devices=DeviceMonitor(pool, config, transport, alive=lambda *_: True),
    )
    snapshot = ticket(comment("go-1", GO.format(state="building"), author="grok-login"), timeout=60)
    dispatcher.process(snapshot)
    job, placement = pool.claim("claude")
    pool.attach_worker(job.id, owner="claude", remote=True)
    return now, queue, pool, transport, grok, dispatcher, snapshot, job


def stuck_wakes(grok: FakeGrokDelivery) -> list:
    return [job for job in grok.jobs if job.request.action == "stuck"]


def test_a_device_offline_60_minutes_marks_the_job_stuck_and_wakes_grok_once(
    tmp_path: Path,
) -> None:
    now, queue, pool, transport, grok, dispatcher, snapshot, job = offline_setup(tmp_path)
    # The runner on the box keeps beating (its ssh hangs), so T6 never fires.
    for minute in range(0, 60, 10):
        now[0] = minute * 60.0
        queue.heartbeat(job.id, owner="claude")
        dispatcher.reconcile_jobs()
        assert state(queue, job.id) == "running"
    now[0] = 3599.0
    queue.heartbeat(job.id, owner="claude")
    dispatcher.reconcile_jobs()
    assert state(queue, job.id) == "running"
    assert not dispatcher.is_stuck(snapshot)

    now[0] = 3600.0
    dispatcher.reconcile_jobs()

    assert state(queue, job.id) == "failed"
    assert dispatcher.is_stuck(snapshot)
    wakes = stuck_wakes(grok)
    assert len(wakes) == 1
    assert "device pc offline 60 minutes" in wakes[0].request.detail
    assert pool.placement(job.id) is not None  # the worker is not confirmed gone
    assert [item for item in queue.outstanding() if item.request.recipient == "claude"] == []

    for later in (3660.0, 7200.0, 10800.0):
        now[0] = later
        dispatcher.reconcile_jobs()
    assert len(stuck_wakes(grok)) == 1
    with queue.connect() as connection:
        done = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE recipient = 'claude' AND state = 'done'"
        ).fetchone()[0]
        reason = connection.execute("SELECT stuck_reason FROM ticket_state").fetchone()[0]
    assert done == 0
    assert reason == "device-offline"


def test_a_good_probe_restarts_the_offline_clock(tmp_path: Path) -> None:
    now, queue, pool, transport, grok, dispatcher, snapshot, job = offline_setup(tmp_path)
    dispatcher.reconcile_jobs()  # first failure at 0
    now[0] = 1800.0
    transport.check = 1  # the device answers: the worker runs
    queue.heartbeat(job.id, owner="claude")
    dispatcher.reconcile_jobs()
    transport.check = 255
    now[0] = 2400.0
    queue.heartbeat(job.id, owner="claude")
    dispatcher.reconcile_jobs()  # failing again from 2400
    now[0] = 3600.0
    queue.heartbeat(job.id, owner="claude")
    dispatcher.reconcile_jobs()
    assert state(queue, job.id) == "running"
    assert stuck_wakes(grok) == []
    now[0] = 2400.0 + 3600.0
    queue.heartbeat(job.id, owner="claude")
    dispatcher.reconcile_jobs()
    assert state(queue, job.id) == "failed"
    assert len(stuck_wakes(grok)) == 1


def test_the_offline_clock_is_forgotten_once_the_device_holds_no_slot(tmp_path: Path) -> None:
    queue, pool, config, job_id = monitor_setup(tmp_path, "codex", box=False)
    transport = FakeTransport(check=255)
    monitor = DeviceMonitor(pool, config, transport, alive=lambda *_: True)
    monitor.reconcile(0.0, lambda *_: None)
    assert pool.health("pc") == (None, 0.0)
    queue.finish(job_id, owner="codex", handoff_comment_id="c")
    monitor.alive = lambda *_: False
    monitor.reconcile(60.0, lambda *_: None)  # no worker started: released, no probe
    assert pool.used() == {}
    assert pool.health("pc") == (None, None)


def test_a_device_check_error_is_recorded_and_never_stops_the_poll(tmp_path: Path) -> None:
    path = tmp_path / "d.db"
    queue = SQLiteJobQueue(path, clock=lambda: 0.0)

    class Exploding:
        def reconcile(self, now, on_offline):
            raise sqlite3.OperationalError("boom")

    dispatcher = Dispatcher(
        path,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=FakeReviewActions(),
        grok_delivery=FakeGrokDelivery(),
        devices=Exploding(),
    )
    dispatcher.reconcile_jobs()
    with queue.connect() as connection:
        rows = connection.execute("SELECT repo, error FROM poll_errors").fetchall()
    assert [tuple(row) for row in rows] == [("devices", "OperationalError")]


def test_suite_guard_shadows_any_real_ssh() -> None:
    """tests/conftest.py puts a refusing ssh first on PATH for every test."""
    import shutil

    found = shutil.which("ssh")
    assert found is not None and "no-real-ssh" in found
    result = subprocess.run([found, "-o", "BatchMode=yes", "example.invalid", "true"],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 255
    assert "real ssh is disabled" in result.stderr
