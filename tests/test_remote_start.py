"""F2-02: remote job start over the device pool (docs/SPEC.md F2-02).

Every test here uses a fake SSH transport: tests/fake_ssh.py on PATH for
the CLI, or FakeTransport in process. No test reaches a real host.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import functools
import io
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from lib import device_monitor, device_pool, exposure, job_run, live_settings, process_tree, remote, telemetry
from lib.device_monitor import DeviceMonitor, remote_target, stranded
from lib.device_pool import DevicePool, hold_memory
from lib.dispatcher import Dispatcher
from lib.harness_env import CPU_CAP, account_tokens, cpu_cap_env, cpu_window, harness_env
from lib.machines_yml import MachinesConfig, load_machines_yml
from lib.queue import JobRequest, SQLiteJobQueue
from lib.suite_slot import LIMIT_ENV
from lib.wait import WaitError, deliver, wait_for_job
from lib.worktrees import pid_alive, process_start
from tests.pool_helpers import TARGET_MACHINES_YML, poller_adopts, write_target_machines
from tests.test_dispatcher import (
    GO,
    TRUSTED_LOGINS,
    FakeGrokDelivery,
    FakeReviewActions,
    comment,
    ticket,
)
from tests.user_manager import scope_refusal

ROOT = Path(__file__).resolve().parents[1]
ORCH = ROOT / "bin" / "orch"
FAKE_SSH = ROOT / "tests" / "fake_ssh.py"
BOTS = ("claude-bot", "codex-bot", "cursor-bot")


# --- fixtures ---------------------------------------------------------------


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
    config, findings = load_machines_yml(path)
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
        """Every call but the start's exposure reads (lib/exposure.py)."""
        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        return [call for call in calls if exposure.SHELL_NAME not in call["script"].split()]

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
    config, findings = load_machines_yml(path)
    assert config is not None
    assert [(item.severity, item.message) for item in findings] == [
        (
            "warning",
            "skipping device 'pc': devices[0].ssh_alias is an install placeholder; "
            "it will not be scheduled",
        )
    ]
    pc = config.device("pc")
    assert pc is not None and not pc.eligible("codex") and pc.ssh_alias_for("codex") is None


def test_shipped_pc_and_box_resolve_each_bot_to_its_own_alias_and_login() -> None:
    config = config_of(ROOT / "machines.yml")
    pc, mac, box = config.device("pc"), config.device("mac"), config.device("box")
    assert pc is not None and mac is not None and box is not None
    assert {harness: pc.ssh_alias_for(harness) for harness in pc.harnesses} == {
        "claude": "pc-wsl-claude-bot",
        "codex": "pc-wsl-codex-bot",
        "cursor": "pc-wsl-cursor-bot",
    }
    assert {harness: box.ssh_alias_for(harness) for harness in box.harnesses} == {
        "claude": "box-claude-bot",
        "codex": "box-codex-bot",
        "cursor": "box-cursor-bot",
    }
    assert (pc.enabled, box.enabled, mac.enabled) == (True, True, False)
    for harness, bot in (("claude", "claude-bot"), ("codex", "codex-bot"), ("cursor", "cursor-bot")):
        assert remote.ssh_argv(remote_target(box, harness, "job1"), "true")[-5:] == [
            "-l", bot, "--", f"box-{bot}", "true",
        ]
        assert remote.ssh_argv(remote_target(pc, harness, "job1"), "true")[-5:] == [
            "-l", bot, "--", f"pc-wsl-{bot}", "true",
        ]
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


def test_target_shape_box_logs_in_as_box_and_pc_as_its_bot(tmp_path: Path) -> None:
    config = config_of(write_target_machines(tmp_path))
    box = remote_target(config.device("box"), "claude", "job1")
    pc = remote_target(config.device("pc"), "claude", "job1")

    assert remote.ssh_argv(box, "true")[-5:] == ["-l", "box", "--", "box-claude-bot", "true"]
    assert remote.ssh_argv(pc, "true")[-5:] == [
        "-l", "claude-bot", "--", "pc-wsl-claude-bot", "true",
    ]


def test_a_box_job_for_claude_bot_starts_over_the_box_login(tmp_path: Path) -> None:
    machines = tmp_path / "machines.yml"
    machines.write_text(
        TARGET_MACHINES_YML.replace("box-{bot}", "fakebox-{bot}").replace(
            "/home/box/factory-work", f"{tmp_path}/boxwork"
        ),
        encoding="utf-8",
    )
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=time.time)
    pool = DevicePool(queue, config_of(machines))
    with queue.connect() as connection:
        connection.executemany(
            "INSERT INTO device_slots (job_id, device, harness, os_user, placed_at) "
            "VALUES (?, 'pc', 'claude', 'claude-bot', 1)",
            [(f"pc-{n}",) for n in range(10)],
        )
    job_id = enqueue(queue, "claude", "1")
    claimed = pool.claim("claude")
    assert claimed is not None
    assert (claimed[1].device, claimed[1].os_user) == ("box", "claude-bot")
    ssh = Ssh(tmp_path)
    (ssh.hosts / "fakebox-claude-bot").write_text("box", encoding="utf-8")

    proc = orch(
        "job", "run", "claude-bot", job_id, "--db", str(tmp_path / "orch.db"),
        "--machines", str(machines), "--", "true",
        env=ssh.env(),
    )
    _, err = proc.communicate(timeout=30)

    assert proc.returncode == 0, err
    calls = ssh.calls()
    assert calls
    assert {(call["user"], call["alias"]) for call in calls} == {("box", "fakebox-claude-bot")}


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
    poller_adopts(queue, machines)
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


def test_remote_job_gets_the_placed_devices_test_suite_max(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ORCH_NO_LOCAL_OVERLAY", raising=False)
    machines, _, _, job_id, ssh = remote_setup(tmp_path, "codex")
    (tmp_path / "machines.local.yml").write_text(
        "devices:\n  - name: pc\n    test_suite_max: 2\n", encoding="utf-8")
    report = tmp_path / "report.json"
    script = f"import json, os; json.dump(os.environ.get({LIMIT_ENV!r}), open({str(report)!r}, 'w'))"
    proc = orch(
        "job", "run", "codex-bot", job_id, "--db", str(tmp_path / "orch.db"), "--machines", str(machines),
        "--", sys.executable, "-c", script,
        env=ssh.env(),
    )
    _, err = proc.communicate(timeout=30)

    assert proc.returncode == 0, err
    assert json.loads(report.read_text(encoding="utf-8")) == "2"


@pytest.mark.parametrize("account", ["placed", "default"])
@pytest.mark.parametrize("recipient", ["claude", "codex"])
def test_a_remote_job_runs_as_its_slots_account(tmp_path: Path, recipient: str, account: str) -> None:
    bot = f"{recipient}-bot"
    placed = f"{recipient}-max-2"
    machines = machines_file(tmp_path, box_enabled=False)
    machines.write_text(
        "accounts:\n"
        "  - {id: claude-max-2, harness: claude, signed_in_on: [pc]}\n"
        "  - {id: codex-max-2, harness: codex, signed_in_on: [pc]}\n"
        + machines.read_text(encoding="utf-8").replace(
            "    ssh_alias: fakepc-{bot}\n", f"    ssh_alias: fakepc-{{bot}}\n    home: {tmp_path}/homes/{{bot}}\n"
        ),
        encoding="utf-8",
    )
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=time.time)
    job_id = enqueue(queue, recipient, "1")
    assert DevicePool(queue, config_of(machines)).claim(recipient) is not None
    if account == "placed":
        with queue.connect() as connection:
            connection.execute("UPDATE device_slots SET account = ? WHERE job_id = ?", (placed, job_id))
    ssh = Ssh(tmp_path)
    home = tmp_path / "homes" / bot
    home.mkdir(parents=True)
    sentinel = f"sk_SYNTHETIC_{uuid.uuid4().hex}"
    (home / ".bash_profile").write_text(
        "".join(f"export {name}={sentinel}\n" for name in ACCOUNT_TOKENS[recipient]), encoding="utf-8"
    )
    report = tmp_path / "report.json"
    dump = f"import json, os\njson.dump(dict(os.environ), open({str(report)!r}, 'w'))\n"

    proc = orch(
        "job", "run", bot, job_id, "--db", str(tmp_path / "orch.db"), "--machines", str(machines),
        "--", sys.executable, "-c", dump,
        env={**ssh.env(), "FAKE_SSH_HOME": str(home)},
    )
    _, err = proc.communicate(timeout=30)

    assert proc.returncode == 0, err
    seen = json.loads(report.read_text(encoding="utf-8"))
    if account == "placed":
        assert seen[ACCOUNT_HOME_VARIABLE[recipient]] == f"{home}/.factory-accounts/{placed}"
        assert not set(ACCOUNT_TOKENS[recipient]) & set(seen)
    else:
        assert ACCOUNT_HOME_VARIABLE[recipient] not in seen
        assert {seen[name] for name in ACCOUNT_TOKENS[recipient]} == {sentinel}
    assert sentinel not in err


def test_a_remote_claude_runs_reported_model_lands_in_the_boxs_job_exposure(tmp_path: Path) -> None:
    machines, queue, _pool, job_id, ssh = remote_setup(tmp_path, "claude")
    init = '{"type":"system","subtype":"init","model":"claude-opus-5-5"}'
    proc = orch(
        "job", "run", "claude-bot", job_id, "--db", str(tmp_path / "orch.db"), "--machines", str(machines),
        "--", sys.executable, "-c", f"print({init!r})",
        env={**ssh.env(), "ORCH_TELEMETRY": "on"},
    )
    _, err = proc.communicate(timeout=30)

    assert proc.returncode == 0, err
    with queue.connect() as connection:
        recorded = exposure.read(connection, job_id)
    assert recorded is not None and recorded.model == "claude-opus-5-5", err
    assert "exposure: model claude-opus-5-5 from the harness's report (was unknown)" in err


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
        self.signals: list[str] = []

    def run(self, argv, *, timeout: float) -> int:
        assert argv[0] == "ssh" and timeout > 0
        user, alias, script = argv[-4], argv[-2], argv[-1]
        kind = "stop" if "kill -TERM" in script or "kill -KILL" in script else "check"
        self.calls.append((kind, user, alias))
        if kind == "stop":
            self.signals.append("KILL" if "kill -KILL" in script else "TERM")
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


def lost_setup(tmp_path: Path, check: int = 255, *, reload: bool = False, tag: str = TAG):
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
        devices=DeviceMonitor(
            pool,
            config,
            transport,
            alive=lambda *_: True,
            machines_path=machines if reload else None,
        ),
    )
    snapshot = ticket(comment("go-1", GO.format(state="building"), author="grok-login"), timeout=60)
    dispatcher.process(snapshot)
    job, placement = pool.claim("claude")
    pool.attach_runner(job.id, owner="claude", pid=11, start="r", alive=lambda *_: False)
    pool.attach_worker(job.id, owner="claude", remote=True, tag=tag)
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


# --- disabled in machines.yml: lost within one poll (#679) --------------------


def stamp(path: Path, ns: int) -> None:
    os.utime(path, ns=(ns, ns))


def disable_pc(tmp_path: Path, ns: int = 10**18) -> None:
    stamp(machines_file(tmp_path, box_enabled=False, pc=False), ns)


def assert_lost(queue: SQLiteJobQueue, pool: DevicePool, job_id: str, reason: str) -> None:
    with queue.connect() as connection:
        row = connection.execute(
            "SELECT state, failure_handled FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    assert (row["state"], row["failure_handled"]) == ("failed", 0)
    assert ("device-lost", f"pc: {reason}; worker tag {TAG}") in job_events(queue, job_id)
    assert pool.placement(job_id) is None
    assert pool.used() == {}
    assert orphans(queue) == [(job_id, "pc", TAG)]


def test_disabling_an_answering_device_loses_its_running_job_within_one_poll(
    tmp_path: Path,
) -> None:
    now, queue, pool, transport, dispatcher, job = lost_setup(tmp_path, check=1, reload=True)
    poll_at(now, dispatcher, 0.0)
    assert state(queue, job.id) == "running"

    disable_pc(tmp_path)
    poll_at(now, dispatcher, 60.0)

    assert_lost(queue, pool, job.id, "disabled")


def test_disabling_an_unreachable_device_loses_its_job_long_before_the_offline_limit(
    tmp_path: Path,
) -> None:
    now, queue, pool, transport, dispatcher, job = lost_setup(tmp_path, reload=True)
    poll_at(now, dispatcher, 0.0)
    assert pool.health("pc") == (None, 0.0)
    assert state(queue, job.id) == "running"

    disable_pc(tmp_path)
    poll_at(now, dispatcher, 60.0)

    assert_lost(queue, pool, job.id, "disabled")


def box_job_setup(tmp_path: Path, *, alive: dict[int, bool]):
    now = [0.0]
    queue = SQLiteJobQueue(tmp_path / "d.db", clock=lambda: now[0])
    machines = machines_file(tmp_path, pc=False)
    config = config_of(machines)
    pool = DevicePool(queue, config)
    kills: list[tuple[int, int]] = []
    monitor = DeviceMonitor(
        pool,
        config,
        FakeTransport(),
        alive=lambda pid, _start: alive.get(pid, False),
        kill=lambda pid, sig: kills.append((pid, sig)),
        machines_path=machines,
    )
    monitor.scan = lambda _tag: {
        pid: process_tree.Process(pid, 1, start, False)
        for pid, start in ((11, "r"), (12, "w"))
        if alive.get(pid, False)
    }
    dispatcher = Dispatcher(
        queue.path,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=FakeReviewActions(),
        grok_delivery=FakeGrokDelivery(),
        devices=monitor,
    )
    dispatcher.process(
        ticket(comment("go-1", GO.format(state="building"), author="grok-login"), timeout=60)
    )
    job, placement = pool.claim("claude")
    assert placement.device == "box"
    pool.attach_runner(job.id, owner="claude", pid=11, start="r", alive=lambda *_: False)
    pool.attach_worker(job.id, owner="claude", pid=12, start="w")
    return now, queue, pool, dispatcher, job, kills


def disable_box(tmp_path: Path) -> None:
    stamp(machines_file(tmp_path, box_enabled=False, pc=False), 10**18)


@pytest.mark.parametrize(
    ("alive", "signalled"),
    [({11: True, 12: True}, 11), ({12: True}, 12)],
    ids=["runner-alive", "runner-gone"],
)
def test_disabling_the_box_loses_its_running_job_and_stops_it_within_one_poll(
    tmp_path: Path, alive: dict[int, bool], signalled: int
) -> None:
    live = dict(alive)
    now, queue, pool, dispatcher, job, kills = box_job_setup(tmp_path, alive=live)
    poll_at(now, dispatcher, 0.0)
    assert state(queue, job.id) == "running"
    assert pool.used() == {"box": 1}

    disable_box(tmp_path)
    poll_at(now, dispatcher, 60.0)

    assert state(queue, job.id) == "failed"
    assert ("device-lost", "box: disabled") in job_events(queue, job.id)
    assert pool.used() == {}
    assert orphans(queue) == [(job.id, "box", "None")]
    assert kills == [(signalled, signal.SIGTERM)]

    poll_at(now, dispatcher, 120.0)
    assert orphans(queue) == [(job.id, "box", "None")]
    round_ = kills[1:]
    stops = [pid for pid, sig in round_[: len(live)] if sig == signal.SIGSTOP]
    killed = [pid for pid, sig in round_[len(live):] if sig == signal.SIGKILL]
    assert len(round_) == 2 * len(live)
    assert sorted(stops) == sorted(killed) == sorted(live)

    live.clear()
    poll_at(now, dispatcher, 180.0)
    assert orphans(queue) == []
    assert len(kills) == 1 + 2 * len(alive)


def test_a_lost_box_worker_that_ignores_sigterm_keeps_its_orphan_until_it_is_killed(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "ready"
    worker = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, sys, time; from pathlib import Path; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "Path(sys.argv[1]).touch(); time.sleep(120)",
            str(ready),
        ],
        env={key: value for key, value in os.environ.items() if key != process_tree.RUN_TAG_ENV},
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        now, queue, pool, dispatcher, job, kills = box_job_setup(tmp_path, alive={})
        monitor = dispatcher.devices
        monitor.alive = pid_alive
        monitor.kill = os.kill
        monitor.scan = process_tree.processes
        runner = subprocess.Popen([sys.executable, "-c", "pass"])
        runner_pid, runner_start = runner.pid, process_start(runner.pid)
        runner.wait()
        with queue.connect() as connection:
            connection.execute(
                "UPDATE device_slots SET runner_pid = ?, runner_start = ?, "
                "worker_pid = ?, worker_start = ? WHERE job_id = ?",
                (runner_pid, runner_start, worker.pid, process_start(worker.pid), job.id),
            )
            connection.commit()

        disable_box(tmp_path)
        poll_at(now, dispatcher, 0.0)
        assert state(queue, job.id) == "failed"
        assert pool.used() == {}
        time.sleep(0.5)
        assert worker.poll() is None
        assert orphans(queue) == [(job.id, "box", "None")]

        poll_at(now, dispatcher, 60.0)
        assert worker.wait(timeout=10) == -signal.SIGKILL
        assert orphans(queue) == [(job.id, "box", "None")]

        poll_at(now, dispatcher, 120.0)
        assert orphans(queue) == []
    finally:
        worker.kill()
        worker.wait()


def test_a_lost_box_job_stops_its_descendants_and_never_a_reused_pid(tmp_path: Path) -> None:
    listing = {
        11: process_tree.Process(11, 1, "r", False),
        12: process_tree.Process(12, 11, "w", False),
        13: process_tree.Process(13, 12, "g", False),
        14: process_tree.Process(14, 1, "x", False),
    }
    now, queue, pool, dispatcher, job, kills = box_job_setup(tmp_path, alive={11: True, 12: True})
    dispatcher.devices.scan = lambda _tag: dict(listing)
    disable_box(tmp_path)
    poll_at(now, dispatcher, 0.0)
    assert kills == [(11, signal.SIGTERM)]

    del listing[11], listing[12]
    listing[13] = process_tree.Process(13, 1, "g", False)
    listing[12] = process_tree.Process(12, 14, "reused", False)
    kills.clear()
    poll_at(now, dispatcher, 60.0)
    assert kills == [(13, signal.SIGSTOP), (13, signal.SIGKILL)]
    assert orphans(queue) == [(job.id, "box", "None")]

    del listing[13]
    poll_at(now, dispatcher, 120.0)
    assert orphans(queue) == []
    assert kills == [(13, signal.SIGSTOP), (13, signal.SIGKILL)]


def test_an_uncontained_lost_box_job_never_signals_a_process_in_its_reused_session(
    tmp_path: Path,
) -> None:
    listing = {
        11: process_tree.Process(11, 1, "r", False, 11),
        12: process_tree.Process(12, 11, "w", False, 11),
        13: process_tree.Process(13, 12, "leaf", False, 13),
    }
    now, queue, pool, dispatcher, job, kills = box_job_setup(tmp_path, alive={11: True, 12: True})
    dispatcher.devices.scan = lambda _tag: dict(listing)
    disable_box(tmp_path)
    poll_at(now, dispatcher, 0.0)
    assert kills == [(11, signal.SIGTERM)]

    del listing[11], listing[12]
    listing[13] = process_tree.Process(13, 1, "leaf", False, 13)
    listing[20] = process_tree.Process(20, 1, "reuser", False, 11)
    kills.clear()
    poll_at(now, dispatcher, 60.0)
    assert kills == [(13, signal.SIGSTOP), (13, signal.SIGKILL)]
    assert orphans(queue) == [(job.id, "box", "None")]


def test_a_contained_lost_box_job_kills_its_scope_and_spares_its_session(
    tmp_path: Path,
) -> None:
    scope = process_tree.scope_unit(TAG)
    listing = {
        11: process_tree.Process(11, 1, "r", False, 11),
        12: process_tree.Process(12, 11, "w", False, 11, scope),
        20: process_tree.Process(20, 1, "neighbour", False, 11),
    }
    now, queue, pool, dispatcher, job, kills = box_job_setup(tmp_path, alive={11: True, 12: True})
    with queue.connect() as connection:
        connection.execute(
            "UPDATE device_slots SET run_scope = ? WHERE job_id = ?", (scope, job.id)
        )
        connection.commit()
    dispatcher.devices.scan = lambda _tag: dict(listing)
    disable_box(tmp_path)
    poll_at(now, dispatcher, 0.0)
    assert kills == [(11, signal.SIGTERM)]

    listing[14] = process_tree.Process(14, 1, "setsid-leaf", False, 14, scope)
    kills.clear()
    poll_at(now, dispatcher, 60.0)
    assert sorted(pid for pid, sig in kills if sig == signal.SIGKILL) == [11, 12, 14]
    assert all(pid != 20 for pid, _ in kills), "a same-session process outside the scope"

    listing = {20: listing[20]}
    poll_at(now, dispatcher, 120.0)
    assert orphans(queue) == []


REUSED_SESSION_CASES = {
    "reused-leader-gone": (
        {12: process_tree.Process(12, 11, "w", False, 11)},
        {44: process_tree.Process(44, 1, "z", False, 11)},
        [],
    ),
    "different-session": (
        {
            12: process_tree.Process(12, 11, "w", False, 12),
            45: process_tree.Process(45, 12, "m", False, 12),
        },
        {
            45: process_tree.Process(45, 1, "m", False, 12),
            44: process_tree.Process(44, 1, "z", False, 11),
        },
        [45],
    ),
    "recorded-member-keeps-session": (
        {
            12: process_tree.Process(12, 11, "w", False, 11),
            45: process_tree.Process(45, 12, "m", False, 11),
        },
        {
            45: process_tree.Process(45, 1, "m", False, 11),
            44: process_tree.Process(44, 1, "z", False, 11),
        },
        [44, 45],
    ),
}


@pytest.mark.parametrize(
    ("first", "later", "killed"),
    list(REUSED_SESSION_CASES.values()),
    ids=list(REUSED_SESSION_CASES),
)
def test_a_lost_box_jobs_session_counts_only_while_a_proven_member_is_in_it(
    tmp_path: Path,
    first: dict[int, process_tree.Process],
    later: dict[int, process_tree.Process],
    killed: list[int],
) -> None:
    """#827 f-5: the dead runner's pid names its session only while a process
    of the run is still in it; after that the number may be another's."""
    listing = {11: process_tree.Process(11, 1, "r", False, 11), **first}
    now, queue, pool, dispatcher, job, kills = box_job_setup(tmp_path, alive={11: True, 12: True})
    dispatcher.devices.scan = lambda _tag: dict(listing)
    disable_box(tmp_path)
    poll_at(now, dispatcher, 0.0)
    assert kills == [(11, signal.SIGTERM)], "the lost job's live runner is asked to stop"

    listing.clear()
    listing.update(later)
    kills.clear()
    poll_at(now, dispatcher, 60.0)
    stops = sorted(pid for pid, sig in kills if sig == signal.SIGSTOP)
    ends = sorted(pid for pid, sig in kills if sig == signal.SIGKILL)
    assert stops == ends == killed, f"the kill poll stops and kills only the run's: {kills}"
    assert len(kills) == 2 * len(killed)

    for pid in killed:
        del listing[pid]
    poll_at(now, dispatcher, 120.0)
    assert orphans(queue) == [], "the row is forgotten once no process of the run is left"
    assert len(kills) == 2 * len(killed), "a process outside the run is never signalled"


LOST_TREE = """\
import os, signal, subprocess, sys, time
from pathlib import Path

here = Path(sys.argv[1])
if sys.argv[2] == "grandchild":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    (here / "grandchild.pid").write_text(str(os.getpid()))
    while True:
        (here / "tick").write_text(str(time.monotonic_ns()))
        time.sleep(0.02)
if sys.argv[2] == "worker":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    subprocess.Popen([sys.executable, __file__, str(here), "grandchild"])
    time.sleep(120)
from lib.job_run import run
sys.exit(run([sys.executable, __file__, str(here), "worker"], beat=lambda: None,
    interval=60, log=print, started=lambda pid: (here / "worker.pid").write_text(str(pid))))
"""


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="reads /proc")
def test_a_lost_box_job_keeps_its_orphan_until_its_whole_tree_is_gone(tmp_path: Path) -> None:
    script = tmp_path / "lost_tree.py"
    script.write_text(LOST_TREE)
    runner = subprocess.Popen(
        [sys.executable, str(script), str(tmp_path), "runner"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    started: list[int] = []
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / "tick").exists():
            assert time.monotonic() < deadline and runner.poll() is None
            time.sleep(0.02)
        worker = int((tmp_path / "worker.pid").read_text())
        grandchild = int((tmp_path / "grandchild.pid").read_text())
        started = [worker, grandchild]
        now, queue, pool, dispatcher, job, kills = box_job_setup(tmp_path, alive={})
        monitor = dispatcher.devices
        monitor.alive = pid_alive
        monitor.kill = os.kill
        monitor.scan = process_tree.processes
        with queue.connect() as connection:
            connection.execute(
                "UPDATE device_slots SET runner_pid = ?, runner_start = ?, "
                "worker_pid = ?, worker_start = ? WHERE job_id = ?",
                (runner.pid, process_start(runner.pid), worker, process_start(worker), job.id),
            )
            connection.commit()

        disable_box(tmp_path)
        poll_at(now, dispatcher, 0.0)
        assert state(queue, job.id) == "failed"
        assert pool.used() == {}
        assert orphans(queue) == [(job.id, "box", "None")]

        poll_at(now, dispatcher, 60.0)
        runner.wait(timeout=10)
        deadline = time.monotonic() + 10
        while pid_alive(worker, None) or pid_alive(grandchild, None):
            assert orphans(queue) == [(job.id, "box", "None")]
            assert time.monotonic() < deadline, "the lost job's tree outlived its SIGKILL"
            time.sleep(0.02)
        tick = (tmp_path / "tick").read_text()

        poll_at(now, dispatcher, 120.0)
        time.sleep(0.1)
        assert (tmp_path / "tick").read_text() == tick, "lost job still executes"
        assert orphans(queue) == []
    finally:
        for pid in (runner.pid, *started):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        runner.wait()


CRASHED_RUN = """\
import os, signal, subprocess, sys, time
from pathlib import Path

here = Path(sys.argv[1])
if sys.argv[2] == "leaf":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    (here / "leaf.pid").write_text(str(os.getpid()))
    while True:
        (here / "tick").write_text(str(time.monotonic_ns()))
        time.sleep(0.02)
if sys.argv[2] == "worker":
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    subprocess.Popen([sys.executable, __file__, str(here), "leaf"])
    time.sleep(120)
from lib.job_run import run
from lib.process_tree import run_tag

def started(pid):
    (here / "worker.pid").write_text(str(pid))
    (here / "run.tag").write_text(run_tag(pid) or "")

sys.exit(run([sys.executable, __file__, str(here), "worker"], beat=lambda: None,
    interval=60, log=print, started=started))
"""


def start_crashed_run(tmp_path: Path) -> tuple[subprocess.Popen[bytes], int, int]:
    """A real `orch job run` tree (runner, worker, TERM-ignoring leaf), its runner then
    killed as a crash would."""
    script = tmp_path / "crashed_run.py"
    script.write_text(CRASHED_RUN)
    runner = subprocess.Popen(
        [sys.executable, str(script), str(tmp_path), "runner"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    deadline = time.monotonic() + 10
    while not (tmp_path / "tick").exists():
        assert time.monotonic() < deadline and runner.poll() is None
        time.sleep(0.02)
    return runner, int((tmp_path / "worker.pid").read_text()), int((tmp_path / "leaf.pid").read_text())


def real_box_job(
    tmp_path: Path,
    runner: subprocess.Popen[bytes],
    worker: int,
    tag: str | None,
    scope: str | None = None,
):
    now, queue, pool, dispatcher, job, _ = box_job_setup(tmp_path, alive={})
    monitor = dispatcher.devices
    monitor.alive = pid_alive
    monitor.kill = os.kill
    monitor.scan = process_tree.processes
    with queue.connect() as connection:
        connection.execute(
            "UPDATE device_slots SET runner_pid = ?, runner_start = ?, "
            "worker_pid = ?, worker_start = ?, run_tag = ?, run_scope = ? WHERE job_id = ?",
            (
                runner.pid, process_start(runner.pid), worker, process_start(worker), tag,
                scope, job.id,
            ),
        )
        connection.commit()
    return now, queue, pool, dispatcher, job


def restart_monitor(dispatcher: Dispatcher, pool: DevicePool) -> None:
    monitor = dispatcher.devices
    dispatcher.devices = DeviceMonitor(
        pool, monitor.config, monitor.transport, alive=pid_alive, kill=os.kill,
        scan=process_tree.processes, machines_path=monitor.machines_path,
    )


def kill_all(*pids: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="reads /proc")
@pytest.mark.parametrize("restart", [False, True], ids=["same-monitor", "restarted-monitor"])
def test_a_lost_box_jobs_survivor_keeps_its_orphan_across_a_monitor_restart(
    tmp_path: Path, restart: bool
) -> None:
    runner, worker, leaf = start_crashed_run(tmp_path)
    try:
        now, queue, pool, dispatcher, job = real_box_job(tmp_path, runner, worker, None)
        runner.kill()
        runner.wait(timeout=10)
        disable_box(tmp_path)
        poll_at(now, dispatcher, 0.0)
        assert orphans(queue) == [(job.id, "box", "None")]
        deadline = time.monotonic() + 10
        while pid_alive(worker, None):
            assert time.monotonic() < deadline, "the worker ignored its SIGTERM"
            time.sleep(0.01)
        assert pid_alive(leaf, None)
        if restart:
            restart_monitor(dispatcher, pool)

        poll_at(now, dispatcher, 60.0)
        assert orphans(queue) == [(job.id, "box", "None")], "live leaf lost its cleanup row"
        deadline = time.monotonic() + 10
        while pid_alive(leaf, None):
            assert time.monotonic() < deadline, "the leaf outlived its SIGKILL"
            time.sleep(0.02)
        tick = (tmp_path / "tick").read_text()
        if restart:
            restart_monitor(dispatcher, pool)

        poll_at(now, dispatcher, 120.0)
        time.sleep(0.1)
        assert (tmp_path / "tick").read_text() == tick, "lost job still executes"
        assert orphans(queue) == []
    finally:
        kill_all(runner.pid, worker, leaf)
        runner.wait()


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="reads /proc")
def test_the_recorded_run_tag_finds_a_survivor_whose_parents_died_before_any_poll(
    tmp_path: Path,
) -> None:
    runner, worker, leaf = start_crashed_run(tmp_path)
    try:
        tag = (tmp_path / "run.tag").read_text()
        assert len(tag) == 32
        now, queue, pool, dispatcher, job = real_box_job(tmp_path, runner, worker, tag)
        runner.kill()
        os.kill(worker, signal.SIGKILL)
        runner.wait(timeout=10)
        deadline = time.monotonic() + 10
        while pid_alive(worker, None):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert pid_alive(leaf, None)

        disable_box(tmp_path)
        poll_at(now, dispatcher, 0.0)
        assert orphans(queue) == [(job.id, "box", "None")], "the tagged leaf was not found"
        restart_monitor(dispatcher, pool)
        poll_at(now, dispatcher, 60.0)
        assert orphans(queue) == [(job.id, "box", "None")]
        deadline = time.monotonic() + 10
        while pid_alive(leaf, None):
            assert time.monotonic() < deadline, "the leaf outlived its SIGKILL"
            time.sleep(0.02)

        poll_at(now, dispatcher, 120.0)
        assert orphans(queue) == []
    finally:
        kill_all(runner.pid, worker, leaf)
        runner.wait()


UNRECORDED_ORPHAN = """\
import os, signal, subprocess, sys, time
from pathlib import Path

here = Path(sys.argv[1])
if sys.argv[2] == "leaf":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    (here / "leaf.pid").write_text(str(os.getpid()))
    while True:
        (here / "tick").write_text(str(time.monotonic_ns()))
        time.sleep(0.02)
if sys.argv[2] == "worker":
    def end(signum, frame):
        subprocess.Popen([sys.executable, __file__, str(here), "leaf"],
            env={"PATH": os.environ["PATH"]}, start_new_session=sys.argv[3] == "setsid")
        os._exit(0)
    signal.signal(signal.SIGTERM, end)
    (here / "command.pid").write_text(str(os.getpid()))
    (here / "ready").touch()
    time.sleep(120)
from lib.job_run import run
from lib.process_tree import run_tag

def started(pid):
    tag = run_tag(pid) or ""
    unit = f"orch-run-{tag}.scope"
    cgroup_file = Path(f"/proc/{pid}/cgroup")
    cgroup = cgroup_file.read_text().splitlines() if cgroup_file.exists() else []
    (here / "worker.pid").write_text(str(pid))
    (here / "run.tag").write_text(tag)
    scopes = [line.rsplit("/", 1)[-1] for line in cgroup if line.startswith("0::")]
    (here / "run.scope").write_text(unit if unit in scopes else "")

sys.exit(run([sys.executable, __file__, str(here), "worker", sys.argv[3]], beat=lambda: None,
    interval=60, log=print, started=started))
"""


FAKE_SYSTEMD_RUN = f"""#!{sys.executable}
import json, os, re, sys

args = sys.argv[1:]
options = []
while args and args[0].startswith("-"):
    word = args.pop(0)
    if word == "--":
        break
    options.append(word)
old = os.environ.get("FAKE_SYSTEMD_OLD") == "1"
if "--help" in options:
    if os.environ.get("FAKE_SYSTEMD_HELP_FAILS") == "1":
        sys.exit(1)
    print("  --scope    Run this as scope rather than service")
    if not old:
        print("  --expand-environment=BOOL    Control expansion of environment variables")
    sys.exit(0)
bus_blip = os.environ.get("FAKE_SYSTEMD_BUS_BLIP", "")
if bus_blip and os.path.exists(bus_blip):
    os.remove(bus_blip)
    sys.exit("Failed to connect to bus: No such file or directory")
if old and any(
    o.startswith("--expand-environment") for o in options
):
    sys.exit("systemd-run: unrecognized option '--expand-environment=no'")
if not any(o.startswith("--unit=") for o in options):
    sys.exit(0)
with open(os.environ["FAKE_SYSTEMD_LOG"], "a") as log:
    log.write(json.dumps(options) + "\\n")
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

def value(name):
    if NAME.fullmatch(name):
        return os.environ.get(name, "")
    print(f"Invalid environment variable name evaluates to an empty string: {{name}}",
          file=sys.stderr)
    return ""

def expand(word):
    if word.startswith("$") and not word.startswith("${{"):
        return value(word[1:]).split()
    return [re.sub(r"\\$\\$|\\$\\{{([^}}]*)\\}}",
                   lambda m: "$" if m[0] == "$$" else value(m[1]), word)]

if not old and "--expand-environment=no" not in options:
    args = args[:1] + [w for a in args[1:] for w in expand(a)]
os.execvp(args[0], args)
"""


def fake_linger(
    tmp_path: Path, linger: str, *, systemd_run_fails: bool = False, systemd_run: bool = False
) -> str:
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    fakes = {"loginctl": f"#!/bin/sh\necho {linger}\n"}
    if systemd_run_fails:
        fakes["systemd-run"] = (
            "#!/bin/sh\necho 'Failed to connect to bus: No such file or directory' >&2\nexit 1\n"
        )
    if systemd_run:
        fakes["systemd-run"] = FAKE_SYSTEMD_RUN
    for name, body in fakes.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    return f"{bin_dir}{os.pathsep}{os.environ['PATH']}"


@pytest.mark.parametrize("leaf_session", ["same", "setsid"])
def test_a_lost_box_jobs_untagged_orphan_born_after_the_last_look_is_killed(
    tmp_path: Path, leaf_session: str
) -> None:
    if sys.platform != "darwin" and (refusal := scope_refusal()):
        pytest.skip(refusal)
    script = tmp_path / "unrecorded_orphan.py"
    script.write_text(UNRECORDED_ORPHAN)
    runner = subprocess.Popen(
        [sys.executable, str(script), str(tmp_path), "runner", leaf_session],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT), "PATH": fake_linger(tmp_path, "yes")},
        start_new_session=True,
    )
    worker = leaf = None
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / "ready").exists():
            assert time.monotonic() < deadline and runner.poll() is None
            time.sleep(0.02)
        worker = int((tmp_path / "worker.pid").read_text())
        tag = (tmp_path / "run.tag").read_text() or None
        scope = (tmp_path / "run.scope").read_text() or None
        now, queue, pool, dispatcher, job = real_box_job(tmp_path, runner, worker, tag, scope)
        runner.kill()
        runner.wait(timeout=10)
        disable_box(tmp_path)

        poll_at(now, dispatcher, 0.0)
        deadline = time.monotonic() + 10
        command = int((tmp_path / "command.pid").read_text())
        while not (tmp_path / "tick").exists() or pid_alive(command, None):
            assert time.monotonic() < deadline, "the worker did not hand off to its leaf"
            time.sleep(0.02)
        leaf = int((tmp_path / "leaf.pid").read_text())
        assert process_tree.run_tag(leaf) is None
        restart_monitor(dispatcher, pool)

        poll_at(now, dispatcher, 60.0)
        assert orphans(queue) == [(job.id, "box", "None")], "live orphan lost its cleanup row"
        deadline = time.monotonic() + 10
        while pid_alive(leaf, None):
            assert time.monotonic() < deadline, "the orphan outlived its SIGKILL"
            time.sleep(0.02)
        tick = (tmp_path / "tick").read_text()

        poll_at(now, dispatcher, 120.0)
        time.sleep(0.1)
        assert (tmp_path / "tick").read_text() == tick, "lost job still executes"
        assert orphans(queue) == []
    finally:
        kill_all(*[pid for pid in (runner.pid, worker, leaf) if pid])
        runner.wait()


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="reads /proc")
def test_a_lost_box_job_never_kills_through_a_dead_runners_reused_session(
    tmp_path: Path,
) -> None:
    """#827 f-5: a session whose leader's pid the dead runner once had holds
    only an unrelated process; the lost job's cleanup never signals it."""
    leader = subprocess.Popen(
        ["sh", "-c", "sleep 120 < /dev/null > /dev/null 2>&1 & echo $!; read -r _ || :"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    stray = int(leader.stdout.readline())
    leader_start = process_start(leader.pid)
    leader.stdin.close()
    leader.stdout.close()
    assert leader.wait(timeout=10) == 0
    worker = subprocess.Popen(["sleep", "60"])
    worker_start = process_start(worker.pid)
    worker.kill()
    worker.wait()
    signals: list[tuple[int, int]] = []

    def kill(pid: int, signum: int) -> None:
        signals.append((pid, signum))
        if pid != stray:
            raise PermissionError(f"pid {pid} is outside this test")
        os.kill(pid, signum)

    try:
        now, queue, pool, dispatcher, job, _ = box_job_setup(tmp_path, alive={})
        monitor = dispatcher.devices
        monitor.alive = pid_alive
        monitor.kill = kill
        monitor.scan = process_tree.processes
        with queue.connect() as connection:
            connection.execute(
                "UPDATE device_slots SET runner_pid = ?, runner_start = ?, "
                "worker_pid = ?, worker_start = ?, run_tag = NULL WHERE job_id = ?",
                (leader.pid, leader_start, worker.pid, worker_start, job.id),
            )
            connection.commit()

        disable_box(tmp_path)
        for at in (0.0, 60.0, 120.0):
            poll_at(now, dispatcher, at)
        assert signals == [], "no process is signalled through the dead runner's session"
        assert pid_alive(stray, None), "the unrelated process in that session still runs"
        assert orphans(queue) == [], "the row is forgotten: nothing of the run is left"
    finally:
        kill_all(stray)


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="reads /proc")
def test_a_box_jobs_slot_records_the_run_tag_its_command_carries(tmp_path: Path) -> None:
    from lib import placed_run

    _, queue, pool, dispatcher, job, _ = box_job_setup(tmp_path, alive={})
    with queue.connect() as connection:
        connection.execute(
            "UPDATE device_slots SET worker_pid = NULL, worker_start = NULL WHERE job_id = ?",
            (job.id,),
        )
        connection.commit()
    placed = placed_run.PlacedRun(
        pool=pool,
        placement=pool.placement(job.id),
        device=dispatcher.devices.config.device("box"),
        owner="claude",
        log=lambda _m: None,
        transport=FakeTransport(),
        account="claude-default",
        account_home=None,
        runner=11,
    )
    seen = tmp_path / "tag"
    code = job_run.run(
        ["sh", "-c", f'printf %s "${process_tree.RUN_TAG_ENV}" > {seen}'],
        beat=lambda: None,
        interval=60,
        log=lambda _m: None,
        started=placed.started,
    )
    assert code == 0
    with queue.connect() as connection:
        recorded = connection.execute(
            "SELECT run_tag FROM device_slots WHERE job_id = ?", (job.id,)
        ).fetchone()[0]
    assert recorded == seen.read_text() and len(recorded) == 32


@pytest.mark.parametrize(
    ("linger", "systemd_run_fails"),
    [("yes", False), ("no", False), ("yes", True)],
    ids=["contained", "linger-off", "no-user-bus"],
)
def test_a_box_jobs_slot_records_its_scope_or_says_the_run_is_uncontained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linger: str, systemd_run_fails: bool
) -> None:
    from lib import placed_run

    contained = linger == "yes" and not systemd_run_fails
    if contained and (refusal := scope_refusal()):
        pytest.skip(refusal)
    if not sys.platform.startswith("linux"):
        pytest.skip("systemd scopes are Linux only")
    monkeypatch.setenv("PATH", fake_linger(tmp_path, linger, systemd_run_fails=systemd_run_fails))
    _, queue, pool, dispatcher, job, _ = box_job_setup(tmp_path, alive={})
    with queue.connect() as connection:
        connection.execute(
            "UPDATE device_slots SET worker_pid = NULL, worker_start = NULL WHERE job_id = ?",
            (job.id,),
        )
        connection.commit()
    placed = placed_run.PlacedRun(
        pool=pool,
        placement=pool.placement(job.id),
        device=dispatcher.devices.config.device("box"),
        owner="claude",
        log=lambda _m: None,
        transport=FakeTransport(),
        account="claude-default",
        account_home=None,
        runner=11,
    )
    ran = tmp_path / "ran"
    lines: list[str] = []
    code = job_run.run(
        ["sh", "-c", f"touch {ran}"],
        beat=lambda: None,
        interval=60,
        log=lines.append,
        started=placed.started,
    )
    assert code == 0 and ran.exists(), "the command runs either way"
    with queue.connect() as connection:
        tag, scope = connection.execute(
            "SELECT run_tag, run_scope FROM device_slots WHERE job_id = ?", (job.id,)
        ).fetchone()
    uncontained = [line for line in lines if "the run is uncontained" in line]
    if contained:
        assert scope == process_tree.scope_unit(tag), lines
        assert uncontained == []
    else:
        assert scope is None
        assert len(uncontained) == 1, lines


NAMESPACED_RUN = """\
import json, os, sys
from pathlib import Path
from lib.job_run import run

here = Path(sys.argv[1])

def started(pid):
    (here / "worker.pid").write_text(str(pid))

code = run(["sh", "-c", f"echo $$ > {here / 'command.pid'}"], beat=lambda: None, interval=60,
    log=lambda line: print(line, file=sys.stderr), started=started)
(here / "code").write_text(str(code))
"""

LOGGING_CALL = f"""#!{sys.executable}
import json, os, sys
with open(os.environ["FAKE_CALLS"], "a") as log:
    log.write(json.dumps([os.path.basename(sys.argv[0]), os.getpid(), *sys.argv[1:]]) + "\\n")
args = sys.argv[1:]
if "--help" in args:
    print("  --expand-environment=BOOL    Control expansion of environment variables")
if os.path.basename(sys.argv[0]) == "systemd-run" and "--" in args:
    command = args[args.index("--") + 1:]
    os.execvp(command[0], command)
"""


def test_a_run_inside_a_pid_namespace_never_hands_the_user_manager_its_namespace_pid(
    tmp_path: Path,
) -> None:
    """#1140: a pid read inside a PID namespace names another process to the
    user manager, so the run is contained by the worker's own systemd-run."""
    if not sys.platform.startswith("linux"):
        pytest.skip("systemd scopes are Linux only")
    unshare = ["unshare", "--user", "--map-current-user", "--pid", "--fork", "--mount-proc"]
    try:
        usable = subprocess.run([*unshare, "true"], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"cannot make a PID namespace: {exc}")
    if usable.returncode != 0:
        pytest.skip(f"cannot make a PID namespace: {usable.stderr.decode().strip()}")
    path = fake_linger(tmp_path, "yes")
    for name in ("busctl", "systemd-run"):
        (tmp_path / "fake-bin" / name).write_text(LOGGING_CALL)
        (tmp_path / "fake-bin" / name).chmod(0o755)
    calls = tmp_path / "calls"
    script = tmp_path / "namespaced_run.py"
    script.write_text(NAMESPACED_RUN)
    result = subprocess.run(
        [*unshare, sys.executable, str(script), str(tmp_path)],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
        env={**os.environ, "PYTHONPATH": str(ROOT), "PATH": path, "FAKE_CALLS": str(calls)},
    )
    assert (tmp_path / "code").read_text() == "0", result.stderr
    worker = int((tmp_path / "worker.pid").read_text())
    assert int((tmp_path / "command.pid").read_text()) == worker, "the command runs as the worker"
    logged = [json.loads(line) for line in calls.read_text().splitlines()]
    assert [call for call in logged if call[0] == "busctl"] == [], (
        "no pid from inside the namespace reaches the user manager"
    )
    scoped = [call for call in logged if call[0] == "systemd-run" and "--" in call]
    assert [call[1] for call in scoped] == [worker], "the worker itself asks for its scope"


def test_an_enabled_box_keeps_its_running_job(tmp_path: Path) -> None:
    now, queue, pool, dispatcher, job, kills = box_job_setup(tmp_path, alive={11: True})
    for at in (0.0, 60.0, 120.0):
        poll_at(now, dispatcher, at)
    assert state(queue, job.id) == "running"
    assert pool.used() == {"box": 1}
    assert kills == []


def test_a_disabled_device_is_stranded_with_no_offline_clock(tmp_path: Path) -> None:
    disabled = config_of(machines_file(tmp_path, pc=False)).device("pc")
    enabled = config_of(machines_file(tmp_path, pc=True)).device("pc")
    assert stranded(disabled, None, 0.0, 600.0) == "disabled"
    assert stranded(enabled, None, 0.0, 600.0) is None


# --- machines.yml reloaded on change (#679) ---------------------------------


def poll_errors(queue: SQLiteJobQueue) -> list[tuple[str, str]]:
    with queue.connect() as connection:
        return [
            (str(row[0]), str(row[1]))
            for row in connection.execute("SELECT repo, error FROM poll_errors ORDER BY rowid")
        ]


def test_the_monitor_reloads_machines_yml_when_its_mtime_changes(tmp_path: Path) -> None:
    now, queue, pool, transport, dispatcher, job = lost_setup(tmp_path, check=1, reload=True)
    monitor = dispatcher.devices
    poll_at(now, dispatcher, 0.0)
    assert monitor.config.device("pc").enabled

    disable_pc(tmp_path)
    poll_at(now, dispatcher, 60.0)

    assert monitor.config.device("pc").enabled is False
    assert pool.config is monitor.config


def test_an_unchanged_machines_yml_is_not_loaded_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now, queue, pool, transport, dispatcher, job = lost_setup(tmp_path, check=1, reload=True)
    loads: list[Path] = []
    real = live_settings.load_machines_yml

    def counted(path: Path, **kwargs):
        loads.append(path)
        return real(path, **kwargs)

    monkeypatch.setattr(live_settings, "load_machines_yml", counted)
    for at in (0.0, 60.0, 120.0):
        poll_at(now, dispatcher, at)
    assert loads == [tmp_path / "machines.yml"]

    disable_pc(tmp_path)
    for at in (180.0, 240.0, 300.0):
        poll_at(now, dispatcher, at)
    assert loads == [tmp_path / "machines.yml"] * 2


def test_a_broken_machines_yml_keeps_the_last_good_config_and_is_reported_until_fixed(
    tmp_path: Path,
) -> None:
    now, queue, pool, transport, dispatcher, job = lost_setup(tmp_path, check=0, reload=True)
    monitor = dispatcher.devices
    poll_at(now, dispatcher, 0.0)
    good = monitor.config
    queue.finish(job.id, owner="claude", handoff_comment_id="c1")
    monitor.alive = lambda *_: False
    machines = tmp_path / "machines.yml"
    machines.write_text("devices: [\n", encoding="utf-8")
    stamp(machines, 10**18)

    poll_at(now, dispatcher, 60.0)

    errors = poll_errors(queue)
    assert [repo for repo, _ in errors] == ["devices"]
    assert errors[0][1].startswith("machines.yml:")
    assert monitor.config is good and pool.config is good
    assert pool.placement(job.id) is None

    poll_at(now, dispatcher, 120.0)
    assert len(poll_errors(queue)) == 2

    disable_pc(tmp_path, ns=2 * 10**18)
    poll_at(now, dispatcher, 180.0)
    assert monitor.config.device("pc").enabled is False
    assert len(poll_errors(queue)) == 2


def test_a_machines_local_yml_edit_reloads_and_disables_the_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ORCH_NO_LOCAL_OVERLAY", raising=False)
    now, queue, pool, transport, dispatcher, job = lost_setup(tmp_path, check=1, reload=True)
    poll_at(now, dispatcher, 0.0)
    assert state(queue, job.id) == "running"

    (tmp_path / "machines.local.yml").write_text(
        "devices:\n  - name: pc\n    enabled: false\n", encoding="utf-8"
    )
    poll_at(now, dispatcher, 60.0)

    assert dispatcher.devices.config.device("pc").enabled is False
    assert_lost(queue, pool, job.id, "disabled")


# --- orphans: stopped once their device answers (#679) -----------------------


STOP = ("stop", "claude-bot", "fakepc-claude-bot")


def test_an_orphan_is_stopped_when_its_device_answers_and_forgotten_once_gone(
    tmp_path: Path,
) -> None:
    now, queue, pool, transport, dispatcher, job = lost_setup(tmp_path)
    poll_at(now, dispatcher, 0.0)
    poll_at(now, dispatcher, 600.0)
    assert orphans(queue) == [(job.id, "pc", TAG)]
    assert STOP not in transport.calls

    transport.check = 1
    poll_at(now, dispatcher, 660.0)
    assert transport.calls[-1] == STOP
    assert transport.signals == ["KILL"], "a worker lost on an earlier poll is killed"
    assert orphans(queue) == [(job.id, "pc", TAG)]

    transport.check = 0
    poll_at(now, dispatcher, 720.0)
    assert orphans(queue) == []


def test_disabling_an_answering_device_stops_the_lost_worker_in_the_same_poll(
    tmp_path: Path,
) -> None:
    now, queue, pool, transport, dispatcher, job = lost_setup(tmp_path, check=1, reload=True)
    poll_at(now, dispatcher, 0.0)
    assert STOP not in transport.calls

    disable_pc(tmp_path)
    poll_at(now, dispatcher, 60.0)

    assert state(queue, job.id) == "failed"
    assert transport.calls[-1] == STOP
    assert transport.signals == ["TERM"], "the poll that loses a worker only asks it to stop"
    assert orphans(queue) == [(job.id, "pc", TAG)]


class LocalShell:
    def run(self, argv, *, timeout: float) -> int:
        return subprocess.run(["sh", "-c", argv[-1]], timeout=timeout).returncode


def test_a_launch_still_in_transit_when_its_device_is_disabled_never_starts(
    tmp_path: Path,
) -> None:
    tag = uuid.uuid4().hex
    now, queue, pool, transport, dispatcher, job = lost_setup(tmp_path, reload=True, tag=tag)
    monitor = dispatcher.devices
    monitor.transport = LocalShell()
    monitor.alive = lambda *_: False
    target = device_monitor.remote_target(monitor.config.device("pc"), "claude", job.id, tag)
    assert target is not None
    marker = tmp_path / "late-worker.pid"
    worker = (
        "import os, time; from pathlib import Path; "
        f"Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(30)"
    )
    start = remote.start_script(target, [sys.executable, "-c", worker], {})
    launch = remote.launch_script(
        target, start, size=0, not_after=int(time.time()) + 600, budget=30, grace=1
    )

    disable_pc(tmp_path)
    poll_at(now, dispatcher, 0.0)
    assert state(queue, job.id) == "failed"
    assert pool.used() == {}
    assert orphans(queue) == []

    late = subprocess.run(
        ["sh", "-c", launch], stdin=subprocess.DEVNULL, capture_output=True, timeout=30
    )
    try:
        assert late.stdout.decode().strip() == f"{remote.LAUNCH_MARK} already"
        assert not marker.exists()
        assert remote.check_worker(monitor.transport, target) == remote.GONE
    finally:
        remote.stop_worker(monitor.transport, target, kill=True)


def test_a_check_that_cannot_fence_the_launch_never_says_gone(tmp_path: Path) -> None:
    config = config_of(machines_file(tmp_path))
    target = device_monitor.remote_target(
        config.device("pc"), "claude", "job-1", uuid.uuid4().hex
    )
    assert target is not None
    jobs = Path(target.job_dir).parent
    jobs.mkdir(parents=True)
    jobs.chmod(0o500)
    try:
        assert remote.check_worker(LocalShell(), target) == remote.UNKNOWN
    finally:
        jobs.chmod(0o700)
    assert remote.check_worker(LocalShell(), target) == remote.GONE


def test_a_launch_that_got_there_first_keeps_its_orphan_until_it_is_stopped(
    tmp_path: Path,
) -> None:
    tag = uuid.uuid4().hex
    now, queue, pool, transport, dispatcher, job = lost_setup(tmp_path, reload=True, tag=tag)
    monitor = dispatcher.devices
    monitor.transport = LocalShell()
    monitor.alive = lambda *_: False
    target = device_monitor.remote_target(monitor.config.device("pc"), "claude", job.id, tag)
    assert target is not None
    marker = tmp_path / "early-worker.pid"
    worker = (
        "import os, time; from pathlib import Path; "
        f"Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(30)"
    )
    start = remote.start_script(target, [sys.executable, "-c", worker], {})
    launch = remote.launch_script(
        target, start, size=0, not_after=int(time.time()) + 600, budget=30, grace=1
    )
    try:
        early = subprocess.run(
            ["sh", "-c", launch], stdin=subprocess.DEVNULL, capture_output=True, timeout=30
        )
        assert early.stdout.decode().strip() == f"{remote.LAUNCH_MARK} started"
        deadline = time.monotonic() + 10
        while not marker.exists():
            assert time.monotonic() < deadline
            time.sleep(0.05)

        disable_pc(tmp_path)
        poll_at(now, dispatcher, 0.0)
        assert state(queue, job.id) == "failed"
        assert orphans(queue) == [(job.id, "pc", tag)]

        deadline = time.monotonic() + 10
        while remote.check_worker(monitor.transport, target) != remote.GONE:
            assert time.monotonic() < deadline
            time.sleep(0.1)
        poll_at(now, dispatcher, 60.0)
        assert orphans(queue) == []
    finally:
        remote.stop_worker(monitor.transport, target, kill=True)


def test_a_launch_that_owns_its_tag_when_the_check_scans_never_starts_its_worker(
    tmp_path: Path,
) -> None:
    root = tmp_path
    bins = root / "bin"
    bins.mkdir()

    def wait_for(path: Path) -> None:
        deadline = time.monotonic() + 15
        while not path.exists():
            assert time.monotonic() < deadline, f"timed out waiting for {path}"
            time.sleep(0.01)

    def helper(name: str, body: str) -> None:
        path = bins / name
        path.write_text(
            f"#!{sys.executable}\nimport os, pathlib, sys, time\n"
            f"r = pathlib.Path({str(root)!r})\n{body}"
        )
        path.chmod(0o755)

    helper(
        "mv",
        "if sys.argv[-1].endswith('/current'):\n"
        "    (r / 'launch-paused').touch()\n"
        "    while not (r / 'allow-detach').exists(): time.sleep(.01)\n"
        "os.execv('/usr/bin/mv', ['mv', *sys.argv[1:]])\n",
    )
    helper(
        "grep",
        "if sys.argv[1:2] == ['-lzxsF'] and not os.environ.get('ORCH_WORKER_TAG'):\n"
        "    (r / 'scan-expanded').touch()\n"
        "    while not (r / 'allow-scan').exists(): time.sleep(.01)\n"
        "os.execv('/usr/bin/grep', ['grep', *sys.argv[1:]])\n",
    )
    helper(
        "uname",
        "if os.environ.get('ORCH_WORKER_TAG'):\n"
        "    (r / 'start-paused').touch()\n"
        "    while not (r / 'allow-start').exists(): time.sleep(.01)\n"
        "os.execv('/usr/bin/uname', ['uname', *sys.argv[1:]])\n",
    )
    env = dict(os.environ, PATH=f"{bins}:{os.environ['PATH']}")

    class PausingShell:
        def run(self, argv, *, timeout: float) -> int:
            return subprocess.run(
                ["sh", "-c", argv[-1]], timeout=timeout, env=env, capture_output=True
            ).returncode

    tag = uuid.uuid4().hex
    now, queue, pool, transport, dispatcher, job = lost_setup(root, reload=True, tag=tag)
    monitor = dispatcher.devices
    monitor.alive = lambda *_: False
    monitor.transport = PausingShell()
    target = device_monitor.remote_target(monitor.config.device("pc"), "claude", job.id, tag)
    assert target is not None
    marker = root / "worker-started"
    start = remote.start_script(
        target,
        [sys.executable, "-c", f"import pathlib, time; pathlib.Path({str(marker)!r}).touch(); time.sleep(30)"],
        {},
    )
    launch = remote.launch_script(
        target, start, size=0, not_after=int(time.time()) + 600, budget=30, grace=1
    )
    launcher = subprocess.Popen(
        ["sh", "-c", launch],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    pool_thread = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        wait_for(root / "launch-paused")
        disable_pc(root)
        checked = pool_thread.submit(dispatcher._check_devices)
        wait_for(root / "scan-expanded")
        (root / "allow-detach").touch()
        wait_for(root / "start-paused")
        out, _ = launcher.communicate(timeout=15)
        assert out.decode().strip() == f"{remote.LAUNCH_MARK} started"
        (root / "allow-scan").touch()
        checked.result(timeout=15)
        assert state(queue, job.id) == "failed"
        assert pool.used() == {}

        (root / "allow-start").touch()
        refused = Path(remote.run_file(target, "exit"))
        deadline = time.monotonic() + 15
        while not marker.exists() and not refused.exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert not marker.exists() or orphans(queue), "live lost worker has no cleanup row"
        assert refused.read_text().strip() == str(remote.START_REFUSED)
    finally:
        for name in ("allow-start", "allow-scan", "allow-detach"):
            (root / name).touch()
        subprocess.run(["sh", "-c", remote.stop_script(target, kill=True)], timeout=10)
        pool_thread.shutdown(wait=True)


def test_an_unreachable_device_keeps_its_orphans_for_one_check_a_poll(tmp_path: Path) -> None:
    now, queue, pool, transport, dispatcher, first = lost_setup(tmp_path)
    dispatcher.process(
        ticket(comment("go-2", GO.format(state="building"), author="grok-login"), ticket_id="2")
    )
    second, _ = pool.claim("claude")
    pool.attach_runner(second.id, owner="claude", pid=12, start="r", alive=lambda *_: False)
    pool.attach_worker(second.id, owner="claude", remote=True, tag="e" * 32)
    poll_at(now, dispatcher, 0.0)
    poll_at(now, dispatcher, 600.0)
    assert len(orphans(queue)) == 2

    for at in (660.0, 720.0):
        seen = len(transport.calls)
        poll_at(now, dispatcher, at)
        assert transport.calls[seen:] == [("check", "claude-bot", "fakepc-claude-bot")]
        assert len(orphans(queue)) == 2


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


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()


def past_uncontained(log: bytes) -> bytes:
    said = remote.UNCONTAINED.encode() + b"\n"
    rest = log.removeprefix(said)
    assert said not in rest, log
    return rest


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


@pytest.mark.parametrize("stubborn", [False, True], ids=["plain", "sigterm-proof"])
def test_r1_f11_remote_child_left_in_the_group_is_gone_before_the_slot_is_released(
    tmp_path: Path, stubborn: bool
) -> None:
    """#1210: the device's wrapper kills a child the harness left, one that
    ignores SIGTERM included, before it writes `exit`; the slot goes then."""
    machines, queue, pool, job_id, ssh = remote_setup(tmp_path, "codex")
    child_pid = tmp_path / "child.pid"
    child: int | None = None
    try:
        proc = orch(
            "job", "run", "codex-bot", job_id, "--db", str(tmp_path / "orch.db"),
            "--machines", str(machines), "--",
            *leave_child_command(child_pid, stubborn=stubborn),
            env=ssh.env(),
        )
        _, err = proc.communicate(timeout=90)
        child = int(wait_file(child_pid))
        assert proc.returncode == 0, err
        assert gone(child), "the leftover child died before orch job run returned"
        assert pool.placement(job_id) is None
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
            record.format(pid=other.pid, boot=boot_id()), encoding="utf-8"
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
    (directory / remote.PID_FILE).write_text(record.format(boot=boot_id()), encoding="utf-8")
    transport = ChecksOnly()
    monitor = DeviceMonitor(pool, config_of(machines), transport)
    result = monitor.reconcile(time.time(), 600.0, lambda *_: None)
    assert transport.stops == []
    assert transport.codes == [3]  # UNKNOWN: the device answered, but cannot tell
    assert (result.stopped, result.released) == ((), ())
    assert pool.placement(job_id) is not None


# F1.3: a new runner must not start while an earlier runner's worker lives.


@pytest.mark.parametrize("sigkill", ["prompt", "late"])
def test_r1_f13_box_takeover_is_refused_while_the_dead_runners_worker_lives(
    tmp_path: Path, sigkill: str
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
        late: list[int] = []

        def kill(pid: int, sig: int) -> None:
            if sigkill == "late" and sig == signal.SIGKILL:
                late.append(pid)
            else:
                os.kill(pid, sig)

        monitor = DeviceMonitor(pool, config_of(machines), FakeTransport(), kill=kill)
        first_look = monitor.reconcile(time.time(), 600.0, lambda *_: None)
        assert first_look.stopped == (job_id,)
        if sigkill == "late":
            assert first_look.released == () and late == [worker]
            assert pool.placement(job_id) is not None
            os.kill(worker, signal.SIGKILL)
        assert wait_gone(worker)
        released = first_look.released or monitor.reconcile(
            time.time(), 600.0, lambda *_: None
        ).released
        assert released == (job_id,)
        assert pool.placement(job_id) is None
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
        tag=uuid.uuid4().hex,
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


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the Linux start only")
def test_an_uncontained_start_says_so_in_the_run_log_and_still_runs(tmp_path: Path) -> None:
    target = remote.RemoteTarget(
        device="pc",
        alias="fakepc-codex-bot",
        user="codex-bot",
        workdir_root=str(tmp_path / "work"),
        job_id="abc123",
        tag=uuid.uuid4().hex,
    )
    script = remote.start_script(target, ["/bin/echo", "HARNESS_RAN"], {})
    result = subprocess.run(
        ["sh", "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=30,
        env={"PATH": fake_linger(tmp_path, "no")},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "HARNESS_RAN\n"
    assert result.stderr == remote.UNCONTAINED + "\n"
    assert not (Path(target.job_dir) / remote.scope_record(target.tag)).exists()


AFFINITY = (
    "import os, subprocess, sys\n"
    "print(sorted(os.sched_getaffinity(0)))\n"
    "grandchild = 'import os; print(sorted(os.sched_getaffinity(0)))'\n"
    "subprocess.run(['sh', '-c', f'{sys.executable} -c \"{grandchild}\"'], check=True)\n"
)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the Linux start only")
@pytest.mark.parametrize(
    ("cap", "contained"),
    [(None, False), (1, False), (1, True)],
    ids=["default-cap", "cap-1", "cap-1-in-a-scope"],
)
def test_a_remote_linux_worker_and_its_grandchild_run_on_the_capped_cpu_window(
    tmp_path: Path, cap: int | None, contained: bool
) -> None:
    allowed = sorted(os.sched_getaffinity(0))
    if allowed != list(range(len(allowed))):
        pytest.skip(f"this test's CPUs are {allowed}, not 0..n-1")
    if len(allowed) <= (cap or CPU_CAP):
        pytest.skip(f"{len(allowed)} CPUs fit a cap of {cap or CPU_CAP}")
    if contained and (refusal := scope_refusal(stripped=True)):
        pytest.skip(refusal)
    target = remote.RemoteTarget(
        device="pc",
        alias="fakepc-codex-bot",
        user="codex-bot",
        workdir_root=str(tmp_path / "work"),
        job_id="abc123",
        tag=uuid.uuid4().hex,
    )
    capped = {} if cap is None else {"cpu_cap": cap}
    script = remote.start_script(target, [sys.executable, "-c", AFFINITY], {}, **capped)
    result = subprocess.run(
        ["setsid", "sh", "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=30,
        env={"PATH": fake_linger(tmp_path, "yes" if contained else "no"), "HOME": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    window = sorted(cpu_window(allowed, cap or CPU_CAP, target.tag))
    assert result.stdout == f"{window}\n{window}\n"
    assert result.stderr == ("" if contained else remote.UNCONTAINED + "\n")
    assert (Path(target.job_dir) / remote.scope_record(target.tag)).exists() == contained


def without_cpu_tools(tmp_path: Path, **fakes: str) -> str:
    """The test's PATH, linked into one directory without nproc or taskset,
    with loginctl saying no linger and each named fake in place."""
    farm = tmp_path / "farm-bin"
    farm.mkdir()
    for directory in os.environ["PATH"].split(os.pathsep):
        if not os.path.isdir(directory):
            continue
        for entry in os.scandir(directory):
            link = farm / entry.name
            if entry.name not in {"nproc", "taskset"} and not os.path.lexists(link):
                link.symlink_to(entry.path)
    for name, body in {"loginctl": "echo no", **fakes}.items():
        (farm / name).unlink(missing_ok=True)
        (farm / name).write_text(f"#!/bin/sh\n{body}\n")
        (farm / name).chmod(0o755)
    return str(farm)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the Linux start only")
@pytest.mark.parametrize(
    ("fakes", "why"),
    [
        ({}, "no nproc"),
        ({"nproc": "echo many"}, "no nproc"),
        ({"nproc": "echo 24"}, "no taskset"),
        ({"nproc": "echo 24", "taskset": "exit 1"}, "taskset -pc 22,23,0,1 failed"),
        ({"nproc": "echo 4"}, None),
    ],
    ids=["no-nproc", "nproc-not-a-number", "no-taskset", "taskset-fails", "cpus-fit-the-cap"],
)
def test_a_remote_linux_start_without_affinity_says_so_and_runs_under_the_env_cap(
    tmp_path: Path, fakes: dict[str, str], why: str | None
) -> None:
    target = remote.RemoteTarget(
        device="pc",
        alias="fakepc-codex-bot",
        user="codex-bot",
        workdir_root=str(tmp_path / "work"),
        job_id="abc123",
        tag="00000016" + uuid.uuid4().hex[8:],
    )
    script = remote.start_script(
        target,
        [sys.executable, "-c", "import os; print(os.environ['ORCH_CPU_CAP'])"],
        cpu_cap_env(),
    )
    result = subprocess.run(
        ["sh", "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=30,
        env={"PATH": without_cpu_tools(tmp_path, **fakes)},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{CPU_CAP}\n"
    skipped = (
        f"orch: cpu cap: affinity skipped ({why}); env cap ORCH_CPU_CAP={CPU_CAP} still applies\n"
        if why
        else ""
    )
    assert result.stderr == remote.UNCONTAINED + "\n" + skipped


@pytest.mark.parametrize(
    "systemd_run",
    [
        '#!/bin/sh\nexec {real} --expand-environment=yes "$@"\n',
        '#!/bin/sh\ncase "$*" in *--expand-environment*) exit 1;;\n'
        '--help) {real} --help | grep -v -e --expand-environment; exit 0;; esac\nexec {real} "$@"\n',
    ],
    ids=["expands-by-default", "too-old-for-the-option"],
)
def test_a_contained_start_reaches_the_command_verbatim_whatever_systemd_run_expands(
    tmp_path: Path, systemd_run: str
) -> None:
    if refusal := scope_refusal(stripped=True):
        pytest.skip(refusal)
    real = shutil.which("systemd-run")
    if subprocess.run(
        [real, "--user", "--scope", "--quiet", "--collect", "--expand-environment=yes", "true"],
        capture_output=True, timeout=10,
    ).returncode != 0:
        pytest.skip("this systemd-run cannot expand variables")
    path = fake_linger(tmp_path, "yes")
    wrapper = tmp_path / "fake-bin" / "systemd-run"
    wrapper.write_text(systemd_run.format(real=real))
    wrapper.chmod(0o755)
    target = remote.RemoteTarget(
        device="pc",
        alias="fakepc-codex-bot",
        user="codex-bot",
        workdir_root=str(tmp_path / "work"),
        job_id="abc123",
        tag=uuid.uuid4().hex,
    )
    report = tmp_path / "report.json"
    dump = f"import json, sys\njson.dump(sys.argv[1:], open({str(report)!r}, 'w'))\n"
    script = remote.start_script(
        target, [sys.executable, "-c", dump, "a b", "$(touch pwn6)", "$HOME", "'"],
        {"ORCH_JOB_ID": "abc123"}, guarded_dirs=[str(tmp_path)], insert_at=3,
    )
    result = subprocess.run(
        ["setsid", "sh", "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=30,
        env={"PATH": path, "HOME": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    assert (Path(target.job_dir) / remote.scope_record(target.tag)).exists(), (
        f"the run is contained: {result.stderr}"
    )
    assert "uncontained" not in result.stderr
    assert json.loads(report.read_text(encoding="utf-8")) == [
        "--add-dir", str(tmp_path), "a b", "$(touch pwn6)", "$HOME", "'"
    ]
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
        tag=uuid.uuid4().hex,
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
        tag=uuid.uuid4().hex,
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
        tag=uuid.uuid4().hex,
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
        tag=uuid.uuid4().hex,
    )


ACCOUNT_HOME_VARIABLE = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
ACCOUNT_TOKENS = {
    "claude": ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    "codex": ("OPENAI_API_KEY", "CODEX_API_KEY"),
}


@pytest.mark.parametrize(
    "declare",
    [
        pytest.param("export {name}={value}", id="export"),
        pytest.param("unset() {{ :; }}; export {name}={value}", id="unset-shadowed"),
        pytest.param(
            "declare -nrx {name}={value}",
            id="nameref",
            marks=pytest.mark.skipif(not _BASH_HAS_NAMEREFS, reason="this bash has no name references"),
        ),
    ],
)
@pytest.mark.parametrize("to_file", [False, True], ids=["report-stdout", "report-file"])
@pytest.mark.parametrize("harness", sorted(ACCOUNT_TOKENS))
def test_an_account_start_runs_from_its_home_and_never_passes_an_account_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str, declare: str, to_file: bool
) -> None:
    ssh = Ssh(tmp_path)
    home = tmp_path / "bot-home"
    home.mkdir()
    account_home = str(home / ".factory-accounts" / f"{harness}-a")
    sentinel = f"sk_SYNTHETIC_{uuid.uuid4().hex}"
    for name in ACCOUNT_TOKENS[harness]:
        monkeypatch.setenv(name, sentinel)
    (home / ".bash_profile").write_text(
        "".join(declare.format(name=name, value=sentinel) + "\n" for name in ACCOUNT_TOKENS[harness]),
        encoding="utf-8",
    )
    report = tmp_path / "report.json"
    dump = f"import json, os\njson.dump(dict(os.environ), open({str(report)!r}, 'w'))\n"
    target = _token_target(tmp_path)
    env_file = str(tmp_path / "worker.env") if to_file else None
    script = remote.start_script(
        target,
        [sys.executable, "-c", dump],
        harness_env(harness, account_home),
        unset=account_tokens(harness, account_home),
        report_env=True,
        env_file=env_file,
    )
    argv = remote.ssh_argv(target, script)
    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=30,
        env={**os.environ, **ssh.env(), "FAKE_SSH_HOME": str(home)},
    )
    assert result.returncode == 0, result.stderr
    seen = json.loads(report.read_text(encoding="utf-8"))
    assert seen[ACCOUNT_HOME_VARIABLE[harness]] == account_home
    assert not set(ACCOUNT_TOKENS[harness]) & set(seen)
    lines = Path(env_file).read_text(encoding="utf-8") if env_file else result.stdout
    [line] = [line for line in lines.splitlines() if line.startswith(remote.WORKER_ENV_MARK)]
    reported = telemetry.worker_environ(line.encode())
    assert reported is not None
    assert reported[ACCOUNT_HOME_VARIABLE[harness]] == account_home
    assert sentinel not in script
    assert sentinel not in " ".join(argv)
    assert sentinel not in result.stderr
    assert sentinel not in result.stdout + "".join(reported.values())


@pytest.mark.parametrize(
    ("harness", "name"), [(harness, name) for harness, names in ACCOUNT_TOKENS.items() for name in names]
)
def test_an_account_start_refuses_when_a_profile_account_token_cannot_be_dropped(
    tmp_path: Path, harness: str, name: str
) -> None:
    ssh = Ssh(tmp_path)
    home = tmp_path / "bot-home"
    home.mkdir()
    (home / ".bash_profile").write_text(f"export {name}=profile-sentinel; readonly {name}\n", encoding="utf-8")
    account_home = str(home / ".factory-accounts" / f"{harness}-a")
    ran = tmp_path / "ran"
    target = _token_target(tmp_path)
    script = remote.start_script(
        target,
        [sys.executable, "-c", f"open({str(ran)!r}, 'w')"],
        harness_env(harness, account_home),
        unset=account_tokens(harness, account_home),
    )
    result = subprocess.run(
        remote.ssh_argv(target, script), capture_output=True, text=True, timeout=30,
        env={**ssh.env(), "FAKE_SSH_HOME": str(home)},
    )
    assert result.returncode == remote.START_REFUSED, result.stderr
    assert not ran.exists()


@pytest.mark.parametrize(
    ("env", "unset"),
    [
        pytest.param({"OPENAI_API_KEY": "x"}, ("OPENAI_API_KEY",), id="set-and-unset"),
        pytest.param({}, ("openai_api_key",), id="lowercase"),
        pytest.param({}, ("OPENAI-API-KEY",), id="dash"),
        pytest.param({}, ("A B",), id="space"),
        pytest.param({}, ("$(touch pwn)",), id="substitution"),
        pytest.param({}, ("",), id="empty"),
        pytest.param({}, (remote.TAG_ENV,), id="worker-tag"),
        pytest.param({}, ("ORCH_JOB_WORKDIR",), id="job-workdir"),
    ],
)
def test_a_start_refuses_an_unset_name_that_is_not_plain_or_is_also_set(
    tmp_path: Path, env: dict[str, str], unset: tuple[str, ...]
) -> None:
    with pytest.raises(remote.RemoteError):
        remote.start_script(_token_target(tmp_path), ["codex"], env, unset=unset)


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
    target = _token_target(tmp_path)
    script = remote.start_script(target, [str(tool), "x=1"], {"GH_TOKEN": JOB_SECRET})
    result = subprocess.run(
        remote.ssh_argv(target, script), capture_output=True, text=True, timeout=30,
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


@pytest.mark.parametrize(
    ("old_systemd", "help_fails", "bus_blip", "launch"),
    [
        (False, False, False, ["--expand-environment=no"]),
        (True, False, False, []),
        (False, True, False, ["--expand-environment=no"]),
        (True, True, False, None),
        (False, False, True, None),
        (True, False, True, None),
    ],
    ids=[
        "systemd-254+",
        "systemd-252",
        "systemd-254+-help-fails",
        "systemd-252-help-fails",
        "systemd-254+-bus-blip",
        "systemd-252-bus-blip",
    ],
)
def test_a_contained_start_hands_the_command_its_argv_whatever_systemd_expands(
    tmp_path: Path, old_systemd: bool, help_fails: bool, bus_blip: bool, launch: list[str] | None
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    log = tmp_path / "systemd-run.log"
    blip = tmp_path / "bus-down"
    if bus_blip:
        blip.touch()
    report = tmp_path / "report.json"
    dump = f"import json, os, sys\njson.dump([sys.argv[1:], os.environ.get('GH_TOKEN')], open({str(report)!r}, 'w'))\n"
    target = _token_target(tmp_path)
    args = ["a b", "$(touch pwn6)", "${HOME}", "$HOME"]
    script = remote.start_script(
        target, [sys.executable, "-c", dump, *args], {"GH_TOKEN": "x${HOME}y"},
        guarded_dirs=[str(tmp_path)], insert_at=3, report_env=True,
    )
    result = subprocess.run(
        ["sh", "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=30,
        env={
            "PATH": fake_linger(tmp_path, "yes", systemd_run=True),
            "HOME": str(home),
            "FAKE_SYSTEMD_LOG": str(log),
            "FAKE_SYSTEMD_OLD": "1" if old_systemd else "0",
            "FAKE_SYSTEMD_HELP_FAILS": "1" if help_fails else "0",
            "FAKE_SYSTEMD_BUS_BLIP": str(blip),
        },
    )
    assert result.returncode == 0, result.stderr
    assert "Invalid environment variable" not in result.stderr
    assert json.loads(report.read_text(encoding="utf-8")) == [
        ["--add-dir", str(tmp_path), *args], "x${HOME}y"
    ]
    assert not any(tmp_path.rglob("pwn*"))
    launches = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    if launch is None:
        assert launches == []
    else:
        [options] = launches
        assert f"--unit=orch-run-{target.tag}.scope" in options
        assert [o for o in options if o.startswith("--expand-environment")] == launch


# --- memory-wait: each poll reads device memory and fails open (#1226) --------


LOW = "MemTotal: 33554432 kB\nMemAvailable: 6291456 kB\n"
AMPLE = "MemTotal: 33554432 kB\nMemAvailable: 16777216 kB\n"
CALM = "some avg10=0.50 avg60=0.20 avg300=0.10 total=1\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
PRESSED = "some avg10=42.00 avg60=20.00 avg300=5.00 total=9\nfull avg10=1.00 avg60=0.00 avg300=0.00 total=0\n"


def memory_rows(queue: SQLiteJobQueue) -> dict[str, tuple[float, str]]:
    with queue.connect() as connection:
        try:
            rows = connection.execute(
                "SELECT device, until, reason FROM device_memory_holds WHERE until > ?", (queue.now(),)
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
    return {str(row[0]): (float(row[1]), str(row[2])) for row in rows}


def memory_setup(tmp_path: Path, readings: dict, *, config: MachinesConfig | None = None):
    now = [1000.0]
    queue = SQLiteJobQueue(tmp_path / "d.db", clock=lambda: now[0])
    config = config or config_of(machines_file(tmp_path))
    pool = DevicePool(queue, config)
    read: list[str] = []

    def reader(device, _on_host):
        read.append(device.name)
        reading = readings[device.name]
        if isinstance(reading, Exception):
            raise reading
        return reading

    monitor = DeviceMonitor(pool, config, FakeTransport(), memory=reader)
    return queue, pool, monitor, read, now


def test_an_over_limit_device_is_held_180s_renewed_each_poll_and_a_good_reading_clears_it(
    tmp_path: Path,
) -> None:
    readings = {"box": (LOW, CALM), "pc": (AMPLE, CALM)}
    queue, pool, monitor, read, now = memory_setup(tmp_path, readings)
    lines: list[str] = []

    held = monitor.check_memory(lines.append)

    assert read == ["box", "pc"]
    assert held == (("box", "memory-wait: MemAvailable 6.0 GiB < 8 GiB"),)
    assert memory_rows(queue) == {"box": (1180.0, "memory-wait: MemAvailable 6.0 GiB < 8 GiB")}
    assert lines == ["memory-wait on box: memory-wait: MemAvailable 6.0 GiB < 8 GiB"]

    readings["box"] = (AMPLE, PRESSED)
    now[0] = 1060.0
    monitor.check_memory(lines.append)
    assert memory_rows(queue) == {"box": (1240.0, "memory-wait: PSI some avg10 42 > 10")}
    assert len(lines) == 1

    readings["box"] = (AMPLE, CALM)
    now[0] = 1120.0
    held = monitor.check_memory(lines.append)
    assert held == ()
    assert memory_rows(queue) == {}
    assert lines[1:] == ["memory-wait off box"]
    assert read == ["box", "pc"] * 3


def test_a_memory_held_device_takes_no_placement_until_it_clears(tmp_path: Path) -> None:
    readings = {"box": (LOW, CALM), "pc": (LOW, CALM)}
    queue, pool, monitor, _, _ = memory_setup(tmp_path, readings)
    enqueue(queue, "claude", "1")
    monitor.check_memory(lambda _line: None)
    assert pool.claim("claude") is None

    readings["pc"] = (AMPLE, CALM)
    monitor.check_memory(lambda _line: None)
    claimed = pool.claim("claude")
    assert claimed is not None and claimed[1].device == "pc"


def test_memory_is_read_outside_any_database_transaction(tmp_path: Path) -> None:
    queue = SQLiteJobQueue(tmp_path / "d.db", clock=lambda: 1000.0)
    config = config_of(machines_file(tmp_path))
    pool = DevicePool(queue, config)
    with queue.connect() as connection:
        hold_memory(connection, "pc", until=1100.0, reason="memory-wait: earlier")
    free: list[str] = []

    def reader(device, _on_host):
        other = sqlite3.connect(tmp_path / "d.db", timeout=0)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.rollback()
            free.append(device.name)
        finally:
            other.close()
        return (AMPLE, CALM)

    DeviceMonitor(pool, config, FakeTransport(), memory=reader).check_memory(lambda _l: None)
    assert free == ["box", "pc"]
    assert memory_rows(queue) == {}


def test_a_disabled_or_draining_device_is_never_read(tmp_path: Path) -> None:
    base = config_of(machines_file(tmp_path, box_enabled=False))
    config = dataclasses.replace(
        base, devices=tuple(dataclasses.replace(d, drain=d.name == "pc") for d in base.devices)
    )
    _, _, monitor, read, _ = memory_setup(tmp_path, {}, config=config)
    assert monitor.check_memory(lambda _l: None) == ()
    assert read == []


class MemoryTransport(FakeTransport):
    """Runs each SSH script locally with /proc swapped for a fake device's dir."""

    def __init__(self, proc: Path):
        super().__init__()
        self.proc = proc
        self.code: int | None = None
        self.captured: list[tuple[list[str], float]] = []

    def call(self, argv, *, timeout: float, input=None):
        self.captured.append((list(argv), timeout))
        if self.code is not None:
            return remote.Called(self.code, "", "ssh: connect to host fakepc: timed out")
        result = subprocess.run(
            ["sh", "-c", argv[-1].replace("/proc/", f"{self.proc}/")],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        return remote.Called(result.returncode, result.stdout, result.stderr)


def fake_proc(root: Path, meminfo: str | None, pressure: str | None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, text in (("meminfo", meminfo), ("pressure/memory", pressure)):
        path = root / name
        if text is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    return root


def real_reader_setup(tmp_path: Path):
    box = fake_proc(tmp_path / "box-proc", AMPLE, CALM)
    pc = fake_proc(tmp_path / "pc-proc", AMPLE, CALM)
    transport = MemoryTransport(pc)
    now = [1000.0]
    queue = SQLiteJobQueue(tmp_path / "d.db", clock=lambda: now[0])
    config = config_of(machines_file(tmp_path))
    monitor = DeviceMonitor(
        DevicePool(queue, config),
        config,
        transport,
        memory=functools.partial(device_monitor.read_memory, transport, proc=box),
    )
    return queue, monitor, transport, box, pc, now


def test_a_remote_device_is_read_in_one_ssh_call_with_a_10_second_timeout(tmp_path: Path) -> None:
    queue, monitor, transport, box, pc, now = real_reader_setup(tmp_path)
    fake_proc(pc, LOW, CALM)
    lines: list[str] = []

    held = monitor.check_memory(lines.append)

    assert held == (("pc", "memory-wait: MemAvailable 6.0 GiB < 8 GiB"),)
    [(argv, timeout)] = transport.captured
    assert timeout == 10.0
    assert argv[0] == "ssh" and argv[-2].startswith("fakepc-")
    assert "/proc/meminfo" in argv[-1] and "/proc/pressure/memory" in argv[-1]
    assert lines == ["memory-wait on pc: memory-wait: MemAvailable 6.0 GiB < 8 GiB"]

    fake_proc(pc, AMPLE, PRESSED)
    fake_proc(box, AMPLE, PRESSED)
    now[0] = 1060.0
    held = monitor.check_memory(lines.append)
    assert dict(held) == {
        "box": "memory-wait: PSI some avg10 42 > 10",
        "pc": "memory-wait: PSI some avg10 42 > 10",
    }
    assert len(transport.captured) == 2


@pytest.mark.parametrize("code", [255, 124])
def test_a_failed_or_timed_out_ssh_read_is_logged_and_clears_the_hold(
    tmp_path: Path, code: int
) -> None:
    queue, monitor, transport, _, pc, now = real_reader_setup(tmp_path)
    fake_proc(pc, LOW, CALM)
    lines: list[str] = []
    monitor.check_memory(lines.append)
    assert set(memory_rows(queue)) == {"pc"}

    transport.code = code
    now[0] = 1060.0
    assert monitor.check_memory(lines.append) == ()

    assert memory_rows(queue) == {}
    assert [line for line in lines[1:] if line.startswith("memory read pc")]
    assert lines[-1].startswith("memory-wait off pc")


def test_a_reader_that_raises_fails_open_for_that_device_alone(tmp_path: Path) -> None:
    readings = {"box": (LOW, CALM), "pc": (LOW, CALM)}
    queue, _, monitor, _, now = memory_setup(tmp_path, readings)
    lines: list[str] = []
    monitor.check_memory(lines.append)
    assert set(memory_rows(queue)) == {"box", "pc"}

    readings["box"] = PermissionError(13, "Permission denied", "/proc/meminfo")
    now[0] = 1060.0
    held = monitor.check_memory(lines.append)

    assert [name for name, _ in held] == ["pc"]
    assert set(memory_rows(queue)) == {"pc"}
    assert any(line.startswith("memory read box") and "Permission denied" in line for line in lines)
    assert "memory-wait off box" in [line.split(" (")[0] for line in lines]


@pytest.mark.parametrize(
    ("meminfo", "pressure", "reason"),
    [
        (LOW, None, "memory-wait: MemAvailable 6.0 GiB < 8 GiB"),
        (None, PRESSED, "memory-wait: PSI some avg10 42 > 10"),
        ("MemAvailable: garbage kB\n", PRESSED, "memory-wait: PSI some avg10 42 > 10"),
        (AMPLE, None, None),
        (None, CALM, None),
    ],
)
def test_a_missing_file_is_logged_and_a_good_reading_of_the_other_still_judges(
    tmp_path: Path, meminfo: str | None, pressure: str | None, reason: str | None
) -> None:
    queue, monitor, _, box, _, _ = real_reader_setup(tmp_path)
    fake_proc(box, meminfo, pressure)
    lines: list[str] = []

    held = monitor.check_memory(lines.append)

    assert held == ((("box", reason),) if reason else ())
    assert set(memory_rows(queue)) == ({"box"} if reason else set())
    if meminfo is None or pressure is None:
        missing = "meminfo" if meminfo is None else "pressure/memory"
        assert any(line.startswith("memory read box") and missing in line for line in lines)


def test_a_device_with_no_proc_is_never_held(tmp_path: Path) -> None:
    queue, monitor, transport, box, pc, now = real_reader_setup(tmp_path)
    fake_proc(pc, LOW, PRESSED)
    monitor.check_memory(lambda _l: None)
    assert set(memory_rows(queue)) == {"pc"}
    shutil.rmtree(box)
    shutil.rmtree(pc)
    pc.mkdir()

    for at in (1060.0, 1120.0, 1500.0):
        now[0] = at
        assert monitor.check_memory(lambda _l: None) == ()
        assert memory_rows(queue) == {}
    assert len(transport.captured) == 4


def test_the_poll_checks_memory_and_logs_each_enter_and_leave(tmp_path: Path) -> None:
    readings = {"box": (AMPLE, CALM), "pc": (LOW, CALM)}
    now = [1000.0]
    path = tmp_path / "d.db"
    queue = SQLiteJobQueue(path, clock=lambda: now[0])
    config = config_of(machines_file(tmp_path))
    lines: list[str] = []
    dispatcher = Dispatcher(
        path,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=FakeReviewActions(),
        grok_delivery=FakeGrokDelivery(),
        devices=DeviceMonitor(
            DevicePool(queue, config), config, FakeTransport(), memory=lambda d, _on_host: readings[d.name]
        ),
        log=lines.append,
    )
    dispatcher.reconcile_jobs()
    assert memory_rows(queue) == {"pc": (1180.0, "memory-wait: MemAvailable 6.0 GiB < 8 GiB")}
    readings["pc"] = (AMPLE, CALM)
    poll_at(now, dispatcher, 1060.0)
    assert memory_rows(queue) == {}
    memory_lines = [line for line in lines if line.startswith("memory")]
    assert memory_lines == [
        "memory-wait on pc: memory-wait: MemAvailable 6.0 GiB < 8 GiB",
        "memory-wait off pc",
    ]


def test_a_memory_check_error_is_recorded_and_never_stops_the_poll(tmp_path: Path) -> None:
    path = tmp_path / "d.db"
    queue = SQLiteJobQueue(path, clock=lambda: 0.0)
    signed: list[float] = []

    class Exploding:
        def reconcile(self, now, lost_after, on_lost):
            return None

        def check_memory(self, log):
            raise sqlite3.OperationalError("database is locked")

        def check_sign_ins(self, now):
            signed.append(now)

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
    assert [tuple(row) for row in rows] == [("devices", "memory: OperationalError: database is locked")]
    assert signed == [0.0]


def memory_poller(tmp_path: Path, now: list[float], config: MachinesConfig, memory) -> Dispatcher:
    path = tmp_path / "d.db"
    queue = SQLiteJobQueue(path, clock=lambda: now[0])
    return Dispatcher(
        path,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=FakeReviewActions(),
        grok_delivery=FakeGrokDelivery(),
        devices=DeviceMonitor(DevicePool(queue, config), config, FakeTransport(), memory=memory),
        log=lambda _line: None,
    )


def blocked_devices(dispatcher: Dispatcher) -> set[str]:
    return set(device_pool.placement_blocks(dispatcher.queue, dispatcher.devices.config, None).devices)


@pytest.mark.parametrize(
    "recovered", [(AMPLE, CALM), (None, None), TimeoutError("ssh timed out")]
)
def test_clearing_a_memory_hold_neither_prolongs_nor_erases_an_unreachable_backoff(
    tmp_path: Path, recovered
) -> None:
    readings: dict[str, object] = {"box": (LOW, CALM), "pc": (AMPLE, CALM)}

    def reader(device, *_):
        reading = readings[device.name]
        if isinstance(reading, Exception):
            raise reading
        return reading

    now = [1000.0]
    dispatcher = memory_poller(tmp_path, now, config_of(machines_file(tmp_path)), reader)
    with dispatcher.queue.connect() as connection:
        device_pool.back_off(connection, "box", until=1300.0, reason="unreachable preflight")
    for at in (1000.0, 1060.0, 1120.0, 1180.0, 1240.0):
        poll_at(now, dispatcher, at)
        assert "box" in blocked_devices(dispatcher)

    readings["box"] = recovered
    poll_at(now, dispatcher, 1250.0)
    assert "box" in blocked_devices(dispatcher), "the unreachable backoff still runs to 1300"
    poll_at(now, dispatcher, 1301.0)
    assert "box" not in blocked_devices(dispatcher), "the memory renewals never extended it"


def test_the_orch_host_is_read_directly_even_when_it_is_not_the_local_device(
    tmp_path: Path,
) -> None:
    path = tmp_path / "machines.yml"
    path.write_text(
        f"""
orch_host: pc
devices:
  - name: pc
    tailscale_name: pc.example.ts.net
    slot_max: 3
    harnesses: {{claude: claude-bot}}
    workdir_root: {tmp_path}/remote/{{bot}}/work
    ssh_alias: fakepc-{{bot}}
    factory_root: /srv/factory
""".lstrip(),
        encoding="utf-8",
    )
    config = config_of(path)
    assert config.orch_host == "pc" and not config.devices[0].local
    host_proc = fake_proc(tmp_path / "host-proc", LOW, CALM)
    transport = MemoryTransport(fake_proc(tmp_path / "ssh-proc", AMPLE, CALM))
    now = [1000.0]
    dispatcher = memory_poller(
        tmp_path, now, config, functools.partial(device_monitor.read_memory, transport, proc=host_proc)
    )

    dispatcher.reconcile_jobs()

    assert transport.captured == [], "no SSH from the orch host to itself"
    assert blocked_devices(dispatcher) == {"pc"}


def test_a_hold_runs_180s_from_when_the_slow_reads_finish(tmp_path: Path) -> None:
    now = [1000.0]

    def slow_reader(device, *_):
        now[0] += 200.0
        return (LOW, CALM)

    dispatcher = memory_poller(tmp_path, now, config_of(machines_file(tmp_path)), slow_reader)

    dispatcher.reconcile_jobs()

    assert now[0] == 1400.0
    assert blocked_devices(dispatcher) == {"box", "pc"}
    now[0] = 1579.0
    assert blocked_devices(dispatcher) == {"box", "pc"}
    now[0] = 1580.0
    assert blocked_devices(dispatcher) == set()
