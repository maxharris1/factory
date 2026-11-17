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


def machines_file(
    tmp_path: Path, *, box_enabled: bool = True, box_slots: int = 3, pc: bool = True
) -> Path:
    # Placement is lowest utilization with ties off-box (issue #223): an empty
    # pool places on the pc. pc=False disables the pc, so jobs land on the box.
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
    enabled: {str(pc).lower()}
    slot_max: 3
    harnesses: {{claude: claude-bot, codex: codex-bot, cursor: cursor-bot}}
    workdir_root: {tmp_path}/remote/{{bot}}/work
    ssh_alias: fakepc-{{bot}}
    factory_root: /srv/factory
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

    def fault(self, alias: str, kind: str, seconds: float) -> Path:
        """Set a link fault (tests/fake_ssh.py: delay, delay-detached, drop-after)."""
        path = self.hosts / f"{alias}.{kind}"
        path.write_text(str(seconds), encoding="utf-8")
        return path


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
        "factory_root": "/home/{bot}/factory",
        **fields,
    }
    lines = ["orch_host: pc", "devices:", "  - name: pc"]
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
    machines = machines_file(tmp_path, box_slots=3, pc=False)
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
    machines = machines_file(tmp_path, box_enabled=True, box_slots=1, pc=False)
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


def test_orch_wait_places_on_the_remote_device_first_and_the_box_on_a_tie(tmp_path: Path) -> None:
    machines = machines_file(tmp_path, box_slots=1)
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=time.time)
    pool = DevicePool(queue, config_of(machines))
    first = enqueue(queue, "codex", "1")
    second = enqueue(queue, "codex", "2")
    out = io.StringIO()
    wait_for_job(queue, "codex", out, pool=pool)
    line = json.loads(out.getvalue())
    assert (line["id"], line["device"], line["remote"]) == (first, "pc", True)
    assert line["workdir"] == f"{tmp_path}/remote/codex-bot/work/jobs/{first}"
    out = io.StringIO()
    wait_for_job(queue, "codex", out, pool=pool)
    line = json.loads(out.getvalue())
    assert (line["id"], line["device"], line["remote"]) == (second, "box", False)


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
    # the launch (#128), its polls, the check that the worker is gone, then
    # (exit 3) whether it ran the harness
    assert len(calls) >= 4
    for call in calls:
        assert (call["user"], call["alias"]) == (bot, f"fakepc-{bot}")
        assert "BatchMode=yes" in call["options"]
    assert sys.executable in calls[0]["script"]
    tag = calls[0]["script"].split()[2].removeprefix("ORCH_WORKER_TAG=")
    assert len(tag) == 32  # the start runs under a fresh worker tag ...
    assert f"t={tag};" in calls[-2]["script"]  # ... and the check looks for that tag
    assert f"= {tag} ]" in calls[-1]["script"]  # ... as does the launch record probe
    assert {call["words"] for call in calls} == {1}  # one remote command string each
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


def test_remote_worktree_preflight_failure_starts_nothing_and_requeues(tmp_path: Path) -> None:
    """#94: --worktree is no longer box-only; a device lacking tools requeues.

    (tests/test_remote_checkout.py covers the checkout itself.)
    """
    machines, queue, pool, job_id, ssh = remote_setup(tmp_path, "codex")
    home = tmp_path / "bot-home"
    home.mkdir()
    (home / ".bash_profile").write_text("PATH=/nonexistent\nexport PATH\n", encoding="utf-8")
    proc = orch(
        "job", "run", "codex-bot", job_id, "--db", str(tmp_path / "orch.db"),
        "--machines", str(machines), "--worktree",
        "--git-url-template", f"file://{tmp_path}/none/{{repo}}.git", "--", "fakeharness",
        env={**ssh.env(), "FAKE_SSH_HOME": str(home)},
    )
    _, err = proc.communicate(timeout=20)
    assert proc.returncode == 75, err
    assert "--worktree is box-only" not in err
    assert "missing under bash -lc on pc: git gh fakeharness" in err
    assert len(ssh.calls()) == 1 and "command -v" in ssh.calls()[0]["script"]
    assert pool.placement(job_id) is None
    assert state(queue, job_id) == "queued"


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
    worker: int | None = None
    try:
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
        assert wait_gone(worker)
        scripts = [call["script"] for call in ssh.calls()]
        assert any("kill -TERM" in item for item in scripts)
        assert pool.placement(job_id) is None
    finally:
        kill_quietly(worker)
        if proc.poll() is None:
            proc.kill()


def test_an_unreachable_device_after_the_job_keeps_the_slot(tmp_path: Path) -> None:
    machines, queue, pool, job_id, ssh = remote_setup(tmp_path, "codex")
    offline = ssh.offline("fakepc-codex-bot")
    proc = orch(
        "job", "run", "codex-bot", job_id, "--db", str(tmp_path / "orch.db"),
        "--machines", str(machines), "--follow-grace", "2", "--", "touch", str(offline),
        env=ssh.env(),
    )
    _, err = proc.communicate(timeout=20)
    assert proc.returncode == 255, err  # #128: no exit status came back
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
    machines = machines_file(tmp_path, pc=False)
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
    machines = machines_file(tmp_path, pc=False)
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=time.time)
    pool = DevicePool(queue, config_of(machines))
    job_id = enqueue(queue, "claude", "1")
    pool.claim("claude")
    started = tmp_path / "started"
    first = orch(
        "job", "run", "claude-bot", job_id, "--db", str(tmp_path / "orch.db"),
        "--machines", str(machines), "--", "sh", "-c", f"touch {started}; exec sleep 30",
    )
    try:
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
    finally:
        if first.poll() is None:
            first.kill()
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
    machines = machines_file(tmp_path, box_enabled=box, pc=not box)
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
    lost: list = []

    monitor.reconcile(1000.0, 600.0, lambda *args: lost.append(args))
    assert pool.placement(job_id) is not None  # still running: never released

    queue.finish(job_id, owner="claude", handoff_comment_id="c1")
    monitor.reconcile(1000.0, 600.0, lambda *args: lost.append(args))
    assert pool.placement(job_id) is not None  # runner alive

    alive[11] = False
    monitor.reconcile(1000.0, 600.0, lambda *args: lost.append(args))
    assert pool.placement(job_id) is not None  # worker alive

    alive[22] = False
    result = monitor.reconcile(1000.0, 600.0, lambda *args: lost.append(args))
    assert result.released == (job_id,)
    assert pool.placement(job_id) is None
    assert lost == []


def test_monitor_releases_a_remote_slot_only_when_the_device_says_the_worker_is_gone(
    tmp_path: Path,
) -> None:
    queue, pool, config, job_id = monitor_setup(tmp_path, "codex", box=False)
    pool.attach_worker(job_id, owner="codex", remote=True)
    queue.finish(job_id, owner="codex", handoff_comment_id=None)
    transport = FakeTransport(check=255)
    monitor = DeviceMonitor(pool, config, transport, alive=lambda *_: False)

    monitor.reconcile(1000.0, 600.0, lambda *_: None)
    assert pool.placement(job_id) is not None  # unreachable: kept
    assert transport.calls == [("check", "codex-bot", "fakepc-codex-bot")]

    transport.check = 1
    result = monitor.reconcile(1060.0, 600.0, lambda *_: None)
    assert result.stopped == (job_id,)  # an orphan with no runner is stopped
    assert pool.placement(job_id) is not None
    assert transport.calls[-1] == ("stop", "codex-bot", "fakepc-codex-bot")

    transport.check = 0
    result = monitor.reconcile(1120.0, 600.0, lambda *_: None)
    assert result.released == (job_id,)
    assert pool.placement(job_id) is None


def test_monitor_releases_a_remote_slot_no_worker_was_started_for_without_ssh(
    tmp_path: Path,
) -> None:
    queue, pool, config, job_id = monitor_setup(tmp_path, "codex", box=False)
    queue.finish(job_id, owner="codex", handoff_comment_id=None)
    transport = FakeTransport(check=255)
    result = DeviceMonitor(pool, config, transport, alive=lambda *_: False).reconcile(
        1000.0, 600.0, lambda *_: None
    )
    assert result.released == (job_id,)
    assert transport.calls == []


# --- unreachable past the limit: lost, its slot freed (#662) ------------------


TAG = "d" * 32


def lost_setup(tmp_path: Path, check: int = 255):
    now = [0.0]
    path = tmp_path / "d.db"
    queue = SQLiteJobQueue(path, clock=lambda: now[0])
    machines = machines_file(tmp_path, box_enabled=False)
    config = config_of(machines)
    pool = DevicePool(queue, config)
    transport = FakeTransport(check=check)
    dispatcher = Dispatcher(
        path,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=FakeReviewActions(),
        grok_delivery=FakeGrokDelivery(),
        devices=DeviceMonitor(pool, config, transport, alive=lambda *_: True),
    )
    snapshot = ticket(comment("go-1", GO.format(state="building"), author="grok-login"), timeout=60)
    dispatcher.process(snapshot)
    job, placement = pool.claim("claude")
    pool.attach_runner(job.id, owner="claude", pid=11, start="r", alive=lambda *_: False)
    pool.attach_worker(job.id, owner="claude", remote=True, tag=TAG)
    return now, queue, pool, transport, dispatcher, job


def poll_at(now: list[float], dispatcher: Dispatcher, at: float) -> None:
    now[0] = at
    dispatcher.reconcile_jobs()


def job_events(queue: SQLiteJobQueue, job_id: str) -> list[tuple[str, str]]:
    with queue.connect() as connection:
        return [
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT event, detail FROM job_events WHERE job_id = ? ORDER BY id", (job_id,)
            )
        ]


def orphans(queue: SQLiteJobQueue) -> list[tuple[str, str, str]]:
    with queue.connect() as connection:
        return [
            (str(row[0]), str(row[1]), str(row[2]))
            for row in connection.execute(
                "SELECT job_id, device, worker_tag FROM device_orphans ORDER BY job_id"
            )
        ]


def test_a_device_unreachable_10_minutes_loses_the_job_and_frees_its_slot(
    tmp_path: Path,
) -> None:
    now, queue, pool, transport, dispatcher, job = lost_setup(tmp_path)
    poll_at(now, dispatcher, 0.0)
    poll_at(now, dispatcher, 540.0)
    assert state(queue, job.id) == "running"
    assert pool.used() == {"pc": 1}

    poll_at(now, dispatcher, 600.0)

    with queue.connect() as connection:
        row = connection.execute(
            "SELECT state, failure_handled FROM jobs WHERE id = ?", (job.id,)
        ).fetchone()
    assert (row["state"], row["failure_handled"]) == ("failed", 0)
    assert ("device-lost", f"pc: unreachable 10 min; worker tag {TAG}") in job_events(
        queue, job.id
    )
    assert pool.placement(job.id) is None
    assert pool.used() == {}
    assert orphans(queue) == [(job.id, "pc", TAG)]
    with queue.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM ticket_state WHERE stuck = 1").fetchone()[0] == 0


def test_a_good_probe_restarts_the_lost_clock(tmp_path: Path) -> None:
    now, queue, pool, transport, dispatcher, job = lost_setup(tmp_path)
    poll_at(now, dispatcher, 0.0)
    transport.check = 1  # the device answers: the worker runs
    poll_at(now, dispatcher, 300.0)
    transport.check = 255
    poll_at(now, dispatcher, 400.0)
    poll_at(now, dispatcher, 900.0)
    assert state(queue, job.id) == "running"
    poll_at(now, dispatcher, 1000.0)
    assert state(queue, job.id) == "failed"
    assert pool.placement(job.id) is None


def test_the_offline_clock_is_forgotten_once_the_device_holds_no_slot(tmp_path: Path) -> None:
    queue, pool, config, job_id = monitor_setup(tmp_path, "codex", box=False)
    transport = FakeTransport(check=255)
    monitor = DeviceMonitor(pool, config, transport, alive=lambda *_: True)
    monitor.reconcile(0.0, 600.0, lambda *_: None)
    assert pool.health("pc") == (None, 0.0)
    queue.finish(job_id, owner="codex", handoff_comment_id="c")
    monitor.alive = lambda *_: False
    monitor.reconcile(60.0, 600.0, lambda *_: None)
    assert pool.used() == {}
    assert pool.health("pc") == (None, None)


def test_a_device_check_error_is_recorded_and_never_stops_the_poll(tmp_path: Path) -> None:
    path = tmp_path / "d.db"
    queue = SQLiteJobQueue(path, clock=lambda: 0.0)

    class Exploding:
        def reconcile(self, now, lost_after, on_lost):
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
    assert [tuple(row) for row in rows] == [("devices", "OperationalError: boom")]


def test_suite_guard_shadows_any_real_ssh() -> None:
    """tests/conftest.py puts a refusing ssh first on PATH for every test."""
    import shutil

    found = shutil.which("ssh")
    assert found is not None and "no-real-ssh" in found
    result = subprocess.run([found, "-o", "BatchMode=yes", "example.invalid", "true"],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 255
    assert "real ssh is disabled" in result.stderr


# --- Codex r1: worker identity, takeover, spawn gap, probes ------------------
#
# These run the real check/stop scripts (tests/fake_ssh.py runs them under
# `sh -c` in a new session, as sshd would), not scripted FakeTransport
# results, so they test what the scripts do to real processes.


BOOT_ID = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()


def gone(pid: int) -> bool:
    """True once pid has exited (a zombie waiting to be reaped counts as gone)."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return True
    return text.rpartition(")")[2].split()[0] == "Z"


def wait_gone(pid: int, seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + seconds
    while not gone(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def wait_file(path: Path, proc: subprocess.Popen | None = None, seconds: float = 20.0) -> str:
    deadline = time.monotonic() + seconds
    while not path.exists() or not path.read_text(encoding="utf-8").strip():
        if proc is not None and proc.poll() is not None:
            raise AssertionError(proc.stderr.read() if proc.stderr else "runner exited")
        assert time.monotonic() < deadline, f"{path} never written"
        time.sleep(0.05)
    return path.read_text(encoding="utf-8").strip()


def kill_quietly(pid: int | None, *, group: bool = False) -> None:
    if pid is None:
        return
    try:
        if group:
            os.killpg(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def use_fake_ssh(monkeypatch: pytest.MonkeyPatch, ssh: Ssh) -> None:
    """Make the in-process SshTransport find tests/fake_ssh.py first on PATH."""
    for name, value in ssh.env().items():
        monkeypatch.setenv(name, value)


def job_dir(tmp_path: Path, bot: str, job_id: str) -> Path:
    return tmp_path / "remote" / bot / "work" / "jobs" / job_id


def starts(ssh: Ssh) -> list[dict]:
    """The start calls (a check or stop on a Mac may make the job directory too)."""
    return [
        call for call in ssh.calls()
        if (call["script"] or "").startswith(f"exec env {remote.TAG_ENV}=")
    ]


# A remote command that leaves a child in its process group and exits. The
# child redirects its standard streams and ignores SIGHUP (and, for the
# stubborn variant, SIGTERM), then writes its pid; the command waits for
# that pid file so the child's handlers are in place before it exits.
CHILD = (
    "import os, signal, sys, time\n"
    "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
    "{extra}"
    "open(sys.argv[1] + '.tmp', 'w').write(str(os.getpid()))\n"
    "os.rename(sys.argv[1] + '.tmp', sys.argv[1])\n"
    "time.sleep(60)\n"
)
LEAVE_CHILD = (
    "import os, subprocess, sys, time\n"
    "path = sys.argv[1]\n"
    "subprocess.Popen([sys.executable, '-c', sys.argv[2], path],\n"
    "                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
    "                 stderr=subprocess.DEVNULL)\n"
    "while not os.path.exists(path):\n"
    "    time.sleep(0.02)\n"
)


def leave_child_command(child_pid: Path, *, stubborn: bool) -> list[str]:
    extra = "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if stubborn else ""
    return [sys.executable, "-c", LEAVE_CHILD, str(child_pid), CHILD.format(extra=extra)]


# F1.1: removing the pid file is not evidence the worker's group is empty.


def test_r1_f11_remote_child_left_in_the_group_is_stopped_before_the_slot_is_released(
    tmp_path: Path,
) -> None:
    machines, queue, pool, job_id, ssh = remote_setup(tmp_path, "codex")
    child_pid = tmp_path / "child.pid"
    child: int | None = None
    try:
        proc = orch(
            "job", "run", "codex-bot", job_id, "--db", str(tmp_path / "orch.db"),
            "--machines", str(machines), "--",
            *leave_child_command(child_pid, stubborn=False),
            env=ssh.env(),
        )
        _, err = proc.communicate(timeout=60)
        child = int(wait_file(child_pid))
        assert proc.returncode == 0, err
        assert "stopping it" in err, err  # the leftover child was found running
        assert wait_gone(child)
        assert pool.placement(job_id) is None
    finally:
        kill_quietly(child)


def test_r1_f11_remote_child_that_will_not_stop_keeps_the_slot_until_it_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    machines, queue, pool, job_id, ssh = remote_setup(tmp_path, "codex")
    child_pid = tmp_path / "child.pid"
    child: int | None = None
    try:
        proc = orch(
            "job", "run", "codex-bot", job_id, "--db", str(tmp_path / "orch.db"),
            "--machines", str(machines), "--",
            *leave_child_command(child_pid, stubborn=True),
            env=ssh.env(),
        )
        _, err = proc.communicate(timeout=90)
        child = int(wait_file(child_pid))
        assert proc.returncode == 0, err
        assert "not confirmed gone (running); slot held" in err, err
        assert not gone(child)
        assert pool.placement(job_id) is not None

        # The poller, with the runner gone, finds it still running too.
        use_fake_ssh(monkeypatch, ssh)
        queue.finish(job_id, owner="codex", handoff_comment_id="c1")
        monitor = DeviceMonitor(pool, config_of(machines), remote.SshTransport())
        result = monitor.reconcile(time.time(), 600.0, lambda *_: None)
        assert result.released == ()
        assert pool.placement(job_id) is not None

        kill_quietly(child)
        assert wait_gone(child)
        result = monitor.reconcile(time.time(), 600.0, lambda *_: None)
        assert result.released == (job_id,)
    finally:
        kill_quietly(child)


# F1.2: a stored group number must not stop (or count as) another group.


def _finished_remote_slot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    machines, queue, pool, job_id, ssh = remote_setup(tmp_path, "codex")
    use_fake_ssh(monkeypatch, ssh)
    pool.attach_worker(job_id, owner="codex", remote=True)
    queue.finish(job_id, owner="codex", handoff_comment_id="c1")
    directory = job_dir(tmp_path, "codex-bot", job_id)
    directory.mkdir(parents=True)
    return machines, queue, pool, job_id, ssh, directory


@pytest.mark.parametrize(
    "record",
    ["{pid}\n", "{pid} {boot}\n"],
    ids=["pid-only", "pid-and-this-boot"],
)
def test_r1_f12_stale_pid_file_naming_another_live_group_leaves_it_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record: str
) -> None:
    machines, queue, pool, job_id, ssh, directory = _finished_remote_slot(tmp_path, monkeypatch)
    other = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        (directory / remote.PID_FILE).write_text(
            record.format(pid=other.pid, boot=BOOT_ID), encoding="utf-8"
        )
        monitor = DeviceMonitor(pool, config_of(machines), remote.SshTransport())
        for _ in range(2):
            result = monitor.reconcile(time.time(), 600.0, lambda *_: None)
            assert (result.stopped, result.released) == ((), ())
        time.sleep(0.2)
        assert other.poll() is None  # the other group was never signalled
        assert pool.placement(job_id) is not None  # cannot tell: slot kept
        assert not any("kill -TERM" in (call["script"] or "") for call in ssh.calls())
    finally:
        other.kill()
        other.wait()


def test_r1_f12_pid_file_from_an_earlier_boot_is_gone_and_signals_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    machines, queue, pool, job_id, ssh, directory = _finished_remote_slot(tmp_path, monkeypatch)
    other = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        (directory / remote.PID_FILE).write_text(
            f"{other.pid} 00000000-0000-0000-0000-000000000000\n", encoding="utf-8"
        )
        monitor = DeviceMonitor(pool, config_of(machines), remote.SshTransport())
        result = monitor.reconcile(time.time(), 600.0, lambda *_: None)
        assert result.released == (job_id,)
        assert result.stopped == ()
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


class ChecksOnly:
    """Runs check scripts through the fake ssh; records stop scripts, never runs them.

    A stop built from a record of `1` would signal every process this user
    owns, so stops are never executed here.
    """

    def __init__(self) -> None:
        self.real = remote.SshTransport()
        self.stops: list[str] = []
        self.codes: list[int] = []

    def run(self, argv, *, timeout: float) -> int:
        if "kill -TERM" in argv[-1]:
            self.stops.append(argv[-1])
            return 0
        code = self.real.run(argv, timeout=timeout)
        self.codes.append(code)
        return code


@pytest.mark.parametrize(
    "record",
    ["0\n", "1\n", "007\n", "\n", "-5\n", "12abc\n", "0 {boot}\n", "1 {boot}\n", "x {boot}\n"],
    ids=["zero", "one", "leading-zero", "empty", "negative", "junk",
         "zero-boot", "one-boot", "junk-boot"],
)
def test_r1_f12_malformed_or_unsafe_pid_records_are_unknown_and_never_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record: str
) -> None:
    machines, queue, pool, job_id, ssh, directory = _finished_remote_slot(tmp_path, monkeypatch)
    (directory / remote.PID_FILE).write_text(record.format(boot=BOOT_ID), encoding="utf-8")
    transport = ChecksOnly()
    monitor = DeviceMonitor(pool, config_of(machines), transport)
    result = monitor.reconcile(time.time(), 600.0, lambda *_: None)
    assert transport.stops == []
    assert transport.codes == [3]  # UNKNOWN: the device answered, but cannot tell
    assert (result.stopped, result.released) == ((), ())
    assert pool.placement(job_id) is not None


# F1.3: a new runner must not start while an earlier runner's worker lives.


def test_r1_f13_box_takeover_is_refused_while_the_dead_runners_worker_lives(
    tmp_path: Path,
) -> None:
    machines = machines_file(tmp_path, pc=False)
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=time.time)
    pool = DevicePool(queue, config_of(machines))
    job_id = enqueue(queue, "claude", "1")
    assert pool.claim("claude")[1].device == "box"
    worker_pid = tmp_path / "worker.pid"
    marker = tmp_path / "second-ran"
    worker: int | None = None
    first = orch(
        "job", "run", "claude-bot", job_id, "--db", str(tmp_path / "orch.db"),
        "--machines", str(machines), "--", "sh", "-c", f"echo $$ > {worker_pid}; exec sleep 60",
    )
    try:
        worker = int(wait_file(worker_pid, first))
        first.kill()  # the runner dies; its worker keeps running
        first.wait(timeout=20)  # not communicate(): the worker holds its pipes
        assert not gone(worker)

        second = orch(
            "job", "run", "claude-bot", job_id, "--db", str(tmp_path / "orch.db"),
            "--machines", str(machines), "--", "touch", str(marker),
        )
        _, err = second.communicate(timeout=20)
        assert second.returncode == 2, err
        assert f"earlier worker pid {worker} still runs" in err
        assert not marker.exists()
        assert pool.placement(job_id) is not None
        assert pool.placement(job_id).worker_pid == worker  # identity kept

        queue.finish(job_id, owner="claude", handoff_comment_id="c1")
        monitor = DeviceMonitor(pool, config_of(machines), FakeTransport())
        assert monitor.reconcile(time.time(), 600.0, lambda *_: None).released == ()
        assert pool.placement(job_id) is not None
        kill_quietly(worker)
        assert wait_gone(worker)
        assert monitor.reconcile(time.time(), 600.0, lambda *_: None).released == (job_id,)
    finally:
        kill_quietly(worker)
        if first.poll() is None:
            first.kill()


def test_r1_f13_remote_takeover_is_refused_while_the_dead_runners_worker_lives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    machines, queue, pool, job_id, ssh = remote_setup(tmp_path, "codex")
    group_file = tmp_path / "group"
    marker = tmp_path / "second-ran"
    group: int | None = None
    script = (
        "import os, sys, time\n"
        f"open({str(group_file)!r} + '.tmp', 'w').write(str(os.getpgid(0)))\n"
        f"os.rename({str(group_file)!r} + '.tmp', {str(group_file)!r})\n"
        "time.sleep(60)\n"
    )
    first = orch(
        "job", "run", "codex-bot", job_id, "--db", str(tmp_path / "orch.db"),
        "--machines", str(machines), "--", sys.executable, "-c", script,
        env=ssh.env(),
    )
    try:
        group = int(wait_file(group_file, first))
        first.kill()  # the runner dies; the remote worker keeps running
        first.wait(timeout=20)  # not communicate(): the worker holds its pipes

        second = orch(
            "job", "run", "codex-bot", job_id, "--db", str(tmp_path / "orch.db"),
            "--machines", str(machines), "--", "touch", str(marker),
            env=ssh.env(),
        )
        _, err = second.communicate(timeout=30)
        assert second.returncode == 2, err
        assert "earlier worker on pc is not confirmed gone (running)" in err
        assert not marker.exists()
        assert len(starts(ssh)) == 1  # no second worker was started
        assert pool.placement(job_id) is not None

        # The orphan is stopped by the poller, and only then is the slot freed.
        use_fake_ssh(monkeypatch, ssh)
        queue.finish(job_id, owner="codex", handoff_comment_id="c1")
        monitor = DeviceMonitor(pool, config_of(machines), remote.SshTransport())
        result = monitor.reconcile(time.time(), 600.0, lambda *_: None)
        assert result.stopped == (job_id,)
        assert pool.placement(job_id) is not None
        deadline = time.monotonic() + 15
        while True:
            try:
                os.killpg(group, 0)
            except ProcessLookupError:
                break
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert monitor.reconcile(time.time(), 600.0, lambda *_: None).released == (job_id,)
    finally:
        kill_quietly(group, group=True)
        if first.poll() is None:
            first.kill()


# F1.4: no command may run before its worker identity is recorded.

SPAWN_THEN_DIE = """
import os, sys
sys.path.insert(0, {root!r})
from lib import job_run

def started(pid):
    open({pid_file!r}, "w").write(str(pid))
    {action}

code = job_run.run(
    ["sh", "-c", "touch " + {marker!r}],
    beat=lambda: None,
    interval=60,
    log=lambda message: print(message, file=sys.stderr, flush=True),
    started=started,
)
sys.exit(code)
"""


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("os._exit(9)", 9),  # the runner dies before it records the worker
        ("raise OSError('database is locked')", 127),  # recording fails
    ],
    ids=["runner-dies", "record-fails"],
)
def test_r1_f14_command_never_runs_unless_its_worker_was_recorded(
    tmp_path: Path, action: str, expected: int
) -> None:
    pid_file = tmp_path / "child.pid"
    marker = tmp_path / "ran"
    source = SPAWN_THEN_DIE.format(
        root=str(ROOT), pid_file=str(pid_file), marker=str(marker), action=action
    )
    child: int | None = None
    try:
        result = subprocess.run(
            [sys.executable, "-c", source], capture_output=True, text=True, timeout=30
        )
        child = int(wait_file(pid_file))
        assert result.returncode == expected, result.stderr
        assert wait_gone(child)
        time.sleep(0.3)
        assert not marker.exists()  # the command never ran
    finally:
        kill_quietly(child)


def test_r1_f14_recorded_worker_runs_the_command_with_the_recorded_pid(tmp_path: Path) -> None:
    from lib import job_run

    seen: list[int] = []
    report = tmp_path / "pid"
    code = job_run.run(
        ["sh", "-c", f"echo $$ > {report}"],
        beat=lambda: None,
        interval=60,
        log=lambda _m: None,
        started=seen.append,
    )
    assert code == 0
    assert seen == [int(report.read_text(encoding="utf-8"))]


# F1.5: every remote device holding a slot is probed every poll.


def test_r1_f15_device_holding_only_finished_jobs_with_live_runners_is_probed_each_poll(
    tmp_path: Path,
) -> None:
    queue, pool, config, job_id = monitor_setup(tmp_path, "codex", box=False)
    pool.attach_runner(job_id, owner="codex", pid=11, start="s", alive=lambda *_: False)
    pool.attach_worker(job_id, owner="codex", remote=True)
    queue.finish(job_id, owner="codex", handoff_comment_id="c1")
    transport = FakeTransport(check=255)
    monitor = DeviceMonitor(pool, config, transport, alive=lambda *_: True)  # runner lives
    for poll, now in enumerate((0.0, 60.0, 120.0), start=1):
        result = monitor.reconcile(now, 600.0, lambda *_: None)
        assert result == type(result)()
        assert len(transport.calls) == poll
        assert transport.calls[-1] == ("check", "codex-bot", "fakepc-codex-bot")
    assert pool.health("pc") == (None, 0.0)
    transport.check = 1
    monitor.reconcile(180.0, 600.0, lambda *_: None)
    assert pool.health("pc") == (180.0, None)
    assert pool.placement(job_id) is not None  # the runner still owns its release


def test_r1_f15_probe_is_skipped_once_cleanup_frees_every_slot(tmp_path: Path) -> None:
    queue, pool, config, job_id = monitor_setup(tmp_path, "codex", box=False)
    queue.finish(job_id, owner="codex", handoff_comment_id="c1")
    transport = FakeTransport(check=255)
    result = DeviceMonitor(pool, config, transport, alive=lambda *_: False).reconcile(
        0.0, 600.0, lambda *_: None
    )
    assert result.released == (job_id,)
    assert transport.calls == []


# Test gaps named in the r1 audit.


def _tamper_slot(queue: SQLiteJobQueue, job_id: str, **values: str) -> None:
    sets = ", ".join(f"{name} = ?" for name in values)
    with queue.connect() as connection:
        connection.execute(
            f"UPDATE device_slots SET {sets} WHERE job_id = ?", (*values.values(), job_id)
        )


def test_r1_gap_a_slot_for_another_bot_user_is_refused_and_kept(tmp_path: Path) -> None:
    machines, queue, pool, job_id, ssh = remote_setup(tmp_path, "codex")
    _tamper_slot(queue, job_id, os_user="cursor-bot")
    marker = tmp_path / "ran"
    proc = orch(
        "job", "run", "codex-bot", job_id, "--db", str(tmp_path / "orch.db"),
        "--machines", str(machines), "--", "touch", str(marker),
        env=ssh.env(),
    )
    _, err = proc.communicate(timeout=20)
    assert proc.returncode == 2
    assert f"job {job_id}'s slot is for cursor-bot, not codex-bot" in err
    assert ssh.calls() == []
    assert not marker.exists()
    assert pool.placement(job_id).os_user == "cursor-bot"


def test_r1_gap_a_slot_on_a_device_machines_yml_no_longer_lists_is_refused_and_kept(
    tmp_path: Path,
) -> None:
    machines, queue, pool, job_id, ssh = remote_setup(tmp_path, "codex")
    _tamper_slot(queue, job_id, device="retired")
    marker = tmp_path / "ran"
    proc = orch(
        "job", "run", "codex-bot", job_id, "--db", str(tmp_path / "orch.db"),
        "--machines", str(machines), "--", "touch", str(marker),
        env=ssh.env(),
    )
    _, err = proc.communicate(timeout=20)
    assert proc.returncode == 2
    assert "placed on 'retired', which machines.yml no longer lists" in err
    assert ssh.calls() == []
    assert not marker.exists()
    assert pool.placement(job_id).device == "retired"


def test_r1_gap_an_unknown_check_keeps_the_slot_stops_nothing_and_counts_as_contact(
    tmp_path: Path,
) -> None:
    queue, pool, config, job_id = monitor_setup(tmp_path, "codex", box=False)
    pool.attach_worker(job_id, owner="codex", remote=True)
    queue.finish(job_id, owner="codex", handoff_comment_id=None)
    transport = FakeTransport(check=3)
    monitor = DeviceMonitor(pool, config, transport, alive=lambda *_: False)
    result = monitor.reconcile(500.0, 600.0, lambda *_: None)
    assert (result.released, result.stopped) == ((), ())
    assert [kind for kind, *_ in transport.calls] == ["check"]
    assert pool.placement(job_id) is not None
    assert pool.health("pc") == (500.0, None)


def test_r1_gap_lost_leaves_a_job_that_is_no_longer_running_alone(tmp_path: Path) -> None:
    now, queue, pool, transport, dispatcher, job = lost_setup(tmp_path)
    queue.finish(job.id, owner="claude", handoff_comment_id="c1")
    dispatcher._device_lost(job.id, "pc", "unreachable 10 min")
    assert state(queue, job.id) == "done"
    assert pool.placement(job.id) is not None
    assert job_events(queue, job.id) == []


def test_r1_gap_lost_rolls_back_when_the_back_off_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lib.dispatch.jobs

    now, queue, pool, transport, dispatcher, job = lost_setup(tmp_path)
    poll_at(now, dispatcher, 0.0)

    def broken(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(lib.dispatch.jobs, "back_off", broken)
    poll_at(now, dispatcher, 600.0)
    assert state(queue, job.id) == "running"
    assert pool.placement(job.id) is not None
    assert job_events(queue, job.id) == []
    with queue.connect() as connection:
        errors = connection.execute("SELECT repo, error FROM poll_errors").fetchall()
    assert ("devices", "OperationalError: disk I/O error") in [tuple(row) for row in errors]


def test_r1_gap_lost_fails_every_running_job_on_the_device(tmp_path: Path) -> None:
    now = [0.0]
    path = tmp_path / "d.db"
    queue = SQLiteJobQueue(path, clock=lambda: now[0])
    config = config_of(machines_file(tmp_path, box_enabled=False))
    pool = DevicePool(queue, config)
    dispatcher = Dispatcher(
        path,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=FakeReviewActions(),
        grok_delivery=FakeGrokDelivery(),
        devices=DeviceMonitor(pool, config, FakeTransport(check=255), alive=lambda *_: True),
    )
    for number in (41, 42):
        dispatcher.process(
            ticket(
                comment(f"go-{number}", GO.format(state="building"), author="grok-login"),
                timeout=600,
                ticket_id=str(number),
            )
        )
    jobs = [pool.claim("claude")[0] for _ in range(2)]
    for job in jobs:
        pool.attach_runner(job.id, owner="claude", pid=11, start="r", alive=lambda *_: False)
        pool.attach_worker(job.id, owner="claude", remote=True)
    poll_at(now, dispatcher, 0.0)
    poll_at(now, dispatcher, 600.0)
    assert [state(queue, job.id) for job in jobs] == ["failed", "failed"]
    assert pool.used() == {}
    assert sorted(job_id for job_id, _, _ in orphans(queue)) == sorted(job.id for job in jobs)


def test_r1_gap_hostile_workdir_and_environment_values_reach_the_command_verbatim(
    tmp_path: Path,
) -> None:
    root = tmp_path / "w o'r\"k $(touch pwn1) `touch pwn2`"
    target = remote.RemoteTarget(
        device="pc",
        alias="fakepc-codex-bot",
        user="codex-bot",
        workdir_root=str(root),
        job_id="abc123",
        tag="0123456789abcdef0123456789abcdef",
    )
    values = {
        "ORCH_JOB_ID": "abc123",
        "ORCH_JOB_BOT": "x'; touch pwn3 #",
        "ORCH_JOB_DEVICE": "$(touch pwn4)\n`touch pwn5`\"",
    }
    report = tmp_path / "report.json"
    dump = (
        "import json, os, sys\n"
        "json.dump({'cwd': os.getcwd(), 'argv': sys.argv[1:],"
        " 'env': {k: os.environ.get(k) for k in"
        " ('ORCH_JOB_ID', 'ORCH_JOB_BOT', 'ORCH_JOB_DEVICE', 'ORCH_JOB_WORKDIR')}},"
        f" open({str(report)!r}, 'w'))\n"
    )
    script = remote.start_script(
        target, [sys.executable, "-c", dump, "a b", "$(touch pwn6)", "'"], values
    )
    result = subprocess.run(
        ["sh", "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=30,
        env={"PATH": os.environ["PATH"]},
    )
    assert result.returncode == 0, result.stderr
    seen = json.loads(report.read_text(encoding="utf-8"))
    assert seen["cwd"] == target.job_dir
    assert seen["argv"] == ["a b", "$(touch pwn6)", "'"]
    assert seen["env"] == {**values, "ORCH_JOB_WORKDIR": target.job_dir}
    assert not any(tmp_path.rglob("pwn*"))


def _f2_01_database(path: Path) -> SQLiteJobQueue:
    queue = SQLiteJobQueue(path, clock=time.time)
    with queue.connect() as connection:
        connection.execute(
            "CREATE TABLE device_slots (job_id TEXT PRIMARY KEY, device TEXT NOT NULL,"
            " harness TEXT NOT NULL, os_user TEXT NOT NULL, placed_at REAL NOT NULL)"
        )
    return queue


def _columns(queue: SQLiteJobQueue) -> set[str]:
    with queue.connect() as connection:
        return {str(row["name"]) for row in connection.execute("PRAGMA table_info(device_slots)")}


def test_r1_gap_f2_01_migration_survives_concurrent_startup(tmp_path: Path) -> None:
    import threading

    queue = _f2_01_database(tmp_path / "old.db")
    config = config_of(machines_file(tmp_path))
    errors: list[BaseException] = []
    gate = threading.Barrier(6)

    def start() -> None:
        try:
            gate.wait()
            DevicePool(SQLiteJobQueue(tmp_path / "old.db", clock=time.time), config)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=start) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert errors == []
    from lib import device_pool

    assert {name for name, _ in device_pool._SLOT_COLUMNS} <= _columns(queue)
    assert DevicePool(queue, config).health("pc") == (None, None)


def test_r1_gap_f2_01_migration_rolls_back_as_a_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lib import device_pool

    queue = _f2_01_database(tmp_path / "old.db")
    before = _columns(queue)
    monkeypatch.setattr(
        device_pool,
        "_SLOT_COLUMNS",
        (*device_pool._SLOT_COLUMNS, ("broken", "INTEGER UNIQUE")),  # ALTER refuses UNIQUE
    )
    with pytest.raises(sqlite3.OperationalError):
        DevicePool(queue, config_of(machines_file(tmp_path)))
    assert _columns(queue) == before
    with queue.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'device_health'"
        ).fetchone() is None


def test_r1_gap_fake_ssh_joins_remote_arguments_like_ssh(tmp_path: Path) -> None:
    ssh = Ssh(tmp_path)
    result = subprocess.run(
        ["ssh", "-l", "codex-bot", "--", "fakepc-codex-bot", "echo", "a", "b  c"],
        env={**os.environ, **ssh.env()}, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "a b c\n"  # one string, re-split by the remote shell


SCRUBBED = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")


@pytest.mark.parametrize(
    ("job_env", "expected"),
    [
        ({}, dict.fromkeys(SCRUBBED)),
        ({"GH_TOKEN": "job-token"}, {**dict.fromkeys(SCRUBBED), "GH_TOKEN": "job-token"}),
    ],
)
def test_remote_start_drops_profile_tokens_and_keeps_only_the_jobs_own(
    tmp_path: Path, job_env: dict[str, str], expected: dict[str, str | None]
) -> None:
    ssh = Ssh(tmp_path)
    home = tmp_path / "bot-home"
    home.mkdir()
    (home / ".bash_profile").write_text(
        "".join(f"export {name}=profile-{name}\n" for name in SCRUBBED), encoding="utf-8"
    )
    target = remote.RemoteTarget(
        device="pc",
        alias="fakepc-codex-bot",
        user="codex-bot",
        workdir_root=str(tmp_path / "work"),
        job_id="abc123",
        tag="0123456789abcdef0123456789abcdef",
    )
    report = tmp_path / "report.json"
    dump = (
        "import json, os\n"
        f"json.dump({{k: os.environ.get(k) for k in {SCRUBBED!r}}}, open({str(report)!r}, 'w'))\n"
    )
    script = remote.start_script(target, [sys.executable, "-c", dump], job_env)
    result = subprocess.run(
        remote.ssh_argv(target, script), capture_output=True, text=True, timeout=30,
        env={**ssh.env(), "FAKE_SSH_HOME": str(home)},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(report.read_text(encoding="utf-8")) == expected


PROFILE_SENTINEL = "ghp_SYNTHETIC_PROFILE_TOKEN"
_BASH_HAS_NAMEREFS = subprocess.run(["bash", "-c", "declare -n r=x"], capture_output=True).returncode == 0


@pytest.mark.parametrize(
    "declare",
    [
        pytest.param("export {name}=" + PROFILE_SENTINEL, id="export"),
        pytest.param("unset() {{ :; }}; export {name}=" + PROFILE_SENTINEL, id="unset-shadowed"),
        pytest.param(
            "declare -nrx {name}=" + PROFILE_SENTINEL,
            id="nameref",
            marks=pytest.mark.skipif(not _BASH_HAS_NAMEREFS, reason="this bash has no name references"),
        ),
    ],
)
@pytest.mark.parametrize("job_env", [{}, {"GH_TOKEN": "job-token"}], ids=["no-job-token", "job-token"])
def test_remote_harness_never_sees_a_profile_token_however_declared(
    tmp_path: Path, declare: str, job_env: dict[str, str]
) -> None:
    ssh = Ssh(tmp_path)
    home = tmp_path / "bot-home"
    home.mkdir()
    (home / ".bash_profile").write_text(
        "".join(declare.format(name=name) + "\n" for name in SCRUBBED), encoding="utf-8"
    )
    target = remote.RemoteTarget(
        device="pc",
        alias="fakepc-codex-bot",
        user="codex-bot",
        workdir_root=str(tmp_path / "work"),
        job_id="abc123",
        tag="0123456789abcdef0123456789abcdef",
    )
    report = tmp_path / "report.json"
    dump = f"import json, os\njson.dump(dict(os.environ), open({str(report)!r}, 'w'))\n"
    script = remote.start_script(target, [sys.executable, "-c", dump], job_env)
    result = subprocess.run(
        remote.ssh_argv(target, script), capture_output=True, text=True, timeout=30,
        env={**ssh.env(), "FAKE_SSH_HOME": str(home)},
    )
    assert result.returncode == 0, result.stderr
    seen = json.loads(report.read_text(encoding="utf-8"))
    assert {name: seen.get(name) for name in SCRUBBED} == {**dict.fromkeys(SCRUBBED), **job_env}
    assert PROFILE_SENTINEL not in seen.values()


@pytest.mark.parametrize("name", SCRUBBED)
def test_remote_start_refuses_when_a_profile_token_cannot_be_dropped(tmp_path: Path, name: str) -> None:
    ssh = Ssh(tmp_path)
    home = tmp_path / "bot-home"
    home.mkdir()
    (home / ".bash_profile").write_text(f"export {name}=profile-sentinel; readonly {name}\n", encoding="utf-8")
    target = remote.RemoteTarget(
        device="pc",
        alias="fakepc-codex-bot",
        user="codex-bot",
        workdir_root=str(tmp_path / "work"),
        job_id="abc123",
        tag="0123456789abcdef0123456789abcdef",
    )
    ran = tmp_path / "ran"
    script = remote.start_script(target, [sys.executable, "-c", f"open({str(ran)!r}, 'w')"], {})
    result = subprocess.run(
        remote.ssh_argv(target, script), capture_output=True, text=True, timeout=30,
        env={**ssh.env(), "FAKE_SSH_HOME": str(home)},
    )
    assert result.returncode == remote.START_REFUSED, result.stderr
    assert not ran.exists()


JOB_SECRET = "ghs_SYNTHETIC_JOB_TOKEN"


def _token_target(tmp_path: Path) -> remote.RemoteTarget:
    return remote.RemoteTarget(
        device="pc",
        alias="fakepc-codex-bot",
        user="codex-bot",
        workdir_root=str(tmp_path / "work"),
        job_id="abc123",
        tag="0123456789abcdef0123456789abcdef",
    )


def test_remote_start_runs_a_harness_whose_path_holds_an_equals_sign(tmp_path: Path) -> None:
    ssh = Ssh(tmp_path)
    home = tmp_path / "bot-home"
    home.mkdir()
    ran = tmp_path / "ran.json"
    tool = tmp_path / "bin" / "tool=beta"
    tool.parent.mkdir()
    tool.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        f"json.dump({{'argv': sys.argv[1:], 'token': os.environ.get('GH_TOKEN')}}, open({str(ran)!r}, 'w'))\n",
        encoding="utf-8",
    )
    tool.chmod(0o755)
    script = remote.start_script(_token_target(tmp_path), [str(tool), "x=1"], {"GH_TOKEN": JOB_SECRET})
    result = subprocess.run(
        remote.ssh_argv(_token_target(tmp_path), script), capture_output=True, text=True, timeout=30,
        env={**ssh.env(), "FAKE_SSH_HOME": str(home)},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(ran.read_text(encoding="utf-8")) == {"argv": ["x=1"], "token": JOB_SECRET}
    assert JOB_SECRET not in result.stdout + result.stderr


@pytest.mark.skipif(not _BASH_HAS_NAMEREFS, reason="this bash has no name references")
@pytest.mark.parametrize("report_env", [False, True], ids=["plain", "report-env"])
@pytest.mark.parametrize("variable", ["e", "r", "k", "n", "a", "c", "names"])
def test_a_profile_name_reference_never_puts_the_job_token_on_stderr(
    tmp_path: Path, variable: str, report_env: bool
) -> None:
    ssh = Ssh(tmp_path)
    home = tmp_path / "bot-home"
    home.mkdir()
    (home / ".bash_profile").write_text(
        f"declare -n {variable}=GH_TOKEN\nexport GH_TOKEN=profile-token\n", encoding="utf-8"
    )
    report = tmp_path / "report.json"
    dump = f"import json, os, sys\njson.dump([sys.argv[1:], os.environ.get('GH_TOKEN')], open({str(report)!r}, 'w'))\n"
    target = _token_target(tmp_path)
    script = remote.start_script(
        target, [sys.executable, "-c", dump], {"GH_TOKEN": JOB_SECRET},
        guarded_dirs=[str(tmp_path)], insert_at=3, report_env=report_env,
    )
    result = subprocess.run(
        remote.ssh_argv(target, script), capture_output=True, text=True, timeout=30,
        env={**ssh.env(), "FAKE_SSH_HOME": str(home)},
    )
    assert JOB_SECRET not in result.stderr
    assert result.returncode == 0, result.stderr
    assert json.loads(report.read_text(encoding="utf-8")) == [["--add-dir", str(tmp_path)], JOB_SECRET]
