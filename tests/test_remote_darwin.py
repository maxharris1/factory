"""Issue #73: remote start, check and stop on a macOS (Darwin) device.

macOS has no /proc and cannot read another process's environment, so the
scripts in lib/remote.py take a Darwin path chosen by `uname -s` on the
device: the boot session id from `sysctl -n kern.bootsessionuuid`, and the
worker named by its record (process group, boot session, tag, leader start
time) plus `ps -o pgid=,pid=,uid=` and `ps -o lstart=`.

These tests play a Mac on this Linux host: a fake `uname` that prints Darwin
and a fake `sysctl` that prints a boot session id come first on PATH, and the
real procps `ps` answers the same `-A`, `-p`, `-o pgid=,pid=,uid=,lstart=`
options macOS's ps does. Every remote call goes through tests/fake_ssh.py
(a new session per command, as sshd runs one); no test reaches a real host.
The Linux path keeps its own tests in tests/test_remote_start.py.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lib import remote
from lib.device_monitor import DeviceMonitor
from tests.test_remote_start import (
    Ssh,
    config_of,
    gone,
    job_dir,
    kill_quietly,
    leave_child_command,
    orch,
    remote_setup,
    starts,
    use_fake_ssh,
    wait_file,
    wait_gone,
)

BOOT = "174C3433-704E-4945-8B27-75E60FAA3D00"
TAG = "0123456789abcdef0123456789abcdef"


class Mac:
    """Fake `uname`, `sysctl` (and, on request, `id` and `ps`) first on PATH."""

    def __init__(self, tmp_path: Path):
        self.bin = tmp_path / "fake-darwin"
        self.bin.mkdir()
        self.boot = tmp_path / "boot-session"
        self.boot.write_text(BOOT + "\n", encoding="utf-8")
        self._tool("uname", 'echo Darwin\n')
        self._tool(
            "sysctl",
            '[ "$*" = "-n kern.bootsessionuuid" ] || exit 1\n'
            f"[ -s {shlex.quote(str(self.boot))} ] || exit 1\n"
            f"exec cat {shlex.quote(str(self.boot))}\n",
        )

    def _tool(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        path.chmod(0o755)

    def foreign_uid(self) -> None:
        """`id -u` names another user, so every process looks like someone else's."""
        self._tool("id", 'echo 424242\n')

    def blind_ps(self) -> None:
        """A ps that lists nothing (and fails), as a broken device would."""
        self._tool("ps", "exit 1\n")

    def path(self, rest: str) -> str:
        return f"{self.bin}{os.pathsep}{rest}"


@pytest.fixture
def mac_ssh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The fake ssh, playing a Mac, for in-process SshTransport calls."""
    ssh = Ssh(tmp_path)
    mac = Mac(tmp_path)
    use_fake_ssh(monkeypatch, ssh)
    monkeypatch.setenv("PATH", mac.path(os.environ["PATH"]))
    return ssh, mac


def mac_env(ssh: Ssh, mac: Mac) -> dict[str, str]:
    env = ssh.env()
    env["PATH"] = mac.path(env["PATH"])
    return env


def target(tmp_path: Path, job_id: str = "job1", tag: str = TAG) -> remote.RemoteTarget:
    return remote.RemoteTarget(
        device="mac",
        alias="fakepc-codex-bot",
        user="codex-bot",
        workdir_root=str(tmp_path / "work"),
        job_id=job_id,
        tag=tag,
    )


def lstart(pid: int) -> str:
    """`ps -o lstart=` for pid as the scripts compare it: C locale, UTC, one space."""
    out = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)],
        env={**os.environ, "LC_ALL": "C", "TZ": "UTC0"},
        capture_output=True, text=True, check=True,
    ).stdout
    return " ".join(out.split())


def start(t: remote.RemoteTarget, command: list[str]) -> subprocess.Popen:
    """The start, through the fake ssh (a new session, as sshd runs it)."""
    return subprocess.Popen(
        remote.ssh_argv(t, remote.start_script(t, command, {"ORCH_JOB_ID": t.job_id})),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )


def record_of(t: remote.RemoteTarget) -> Path:
    return Path(t.job_dir) / remote.PID_FILE


def write_record(t: remote.RemoteTarget, line: str) -> None:
    record_of(t).parent.mkdir(parents=True, exist_ok=True)
    record_of(t).write_text(line, encoding="utf-8")


def stranger() -> subprocess.Popen:
    """A live process group of this user that is no job's worker."""
    return subprocess.Popen(["sleep", "60"], start_new_session=True)


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_group_gone(pgid: int, seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + seconds
    while group_alive(pgid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


# A worker that reports its pid, process group and tag, then sleeps.
REPORTER = (
    "import json, os, sys, time\n"
    "path = sys.argv[1]\n"
    "json.dump({'pid': os.getpid(), 'pgid': os.getpgid(0),"
    " 'tag': os.environ.get('ORCH_WORKER_TAG'), 'cwd': os.getcwd()}, open(path + '.tmp', 'w'))\n"
    "os.rename(path + '.tmp', path)\n"
    "time.sleep(60)\n"
)


# --- the scripts run under POSIX sh whatever the login shell ----------------


def test_every_remote_script_is_one_posix_sh_command_for_any_login_shell(tmp_path: Path) -> None:
    t = target(tmp_path)
    for script in (remote.check_script(t), remote.stop_script(t)):
        words = shlex.split(script)
        assert words[:3] == ["exec", "sh", "-c"] and len(words) == 4
        assert words[3].startswith(f"t={TAG}; ")  # the tag is a shell variable, never exported
    words = shlex.split(remote.start_script(t, ["true"], {}))
    assert words[:5] == ["exec", "env", f"ORCH_WORKER_TAG={TAG}", "sh", "-c"] and len(words) == 6


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_check_runs_from_a_zsh_login_shell(tmp_path: Path) -> None:
    mac = Mac(tmp_path)
    t = target(tmp_path)
    result = subprocess.run(
        ["zsh", "-c", remote.check_script(t)],
        env={"PATH": mac.path(os.environ["PATH"]), "HOME": str(tmp_path)},
        start_new_session=True, timeout=30,
    )
    assert result.returncode == 0  # no record: gone


def test_a_device_that_is_not_darwin_keeps_the_linux_path(tmp_path: Path) -> None:
    ssh = Ssh(tmp_path)
    other = tmp_path / "other-unix"
    other.mkdir()
    (other / "uname").write_text("#!/bin/sh\necho FreeBSD\n", encoding="utf-8")
    (other / "uname").chmod(0o755)
    env = {**os.environ, **ssh.env()}
    env["PATH"] = f"{other}{os.pathsep}{env['PATH']}"
    t = target(tmp_path)
    result = subprocess.run(
        remote.ssh_argv(t, remote.start_script(t, ["true"], {})),
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").split()
    fields = record_of(t).read_text(encoding="utf-8").split()
    assert len(fields) == 2 and fields[1:] == boot_id  # the Linux `<group> <boot id>` record


# --- start, check, stop -----------------------------------------------------


def test_darwin_tagged_worker_is_running_until_its_group_is_sent_sigterm(
    tmp_path: Path, mac_ssh
) -> None:
    ssh, mac = mac_ssh
    t = target(tmp_path)
    report = tmp_path / "worker.json"
    transport = remote.SshTransport()
    assert remote.check_worker(transport, t) == remote.GONE  # nothing started yet
    proc = start(t, [sys.executable, "-c", REPORTER, str(report)])
    seen: dict = {}
    try:
        seen = json.loads(wait_file(report, proc))
        assert seen["pgid"] == seen["pid"]  # the worker leads its own group ...
        assert seen["tag"] == TAG  # ... and carries the tag
        assert seen["cwd"] == t.job_dir
        fields = record_of(t).read_text(encoding="utf-8").split(maxsplit=3)
        assert fields[:3] == [str(seen["pgid"]), BOOT, TAG]
        assert fields[3].strip() == lstart(seen["pid"])

        assert remote.check_worker(transport, t) == remote.RUNNING
        assert remote.check_worker(transport, target(tmp_path, tag="f" * 32)) == remote.GONE
        assert remote.stop_worker(transport, t) is True
        assert wait_gone(seen["pid"])
        proc.wait(timeout=20)
        assert remote.check_worker(transport, t) == remote.GONE
        assert record_of(t).exists()  # the record is never removed
    finally:
        kill_quietly(seen.get("pgid"), group=True)
        if proc.poll() is None:
            proc.kill()


def test_darwin_child_left_in_the_group_after_the_leader_exits_runs_until_stopped(
    tmp_path: Path, mac_ssh
) -> None:
    ssh, mac = mac_ssh
    t = target(tmp_path)
    child_pid = tmp_path / "child.pid"
    transport = remote.SshTransport()
    proc = start(t, leave_child_command(child_pid, stubborn=False))
    child: int | None = None
    try:
        proc.wait(timeout=30)
        assert proc.returncode == 0, proc.stderr.read()
        child = int(wait_file(child_pid))
        leader = int(record_of(t).read_text(encoding="utf-8").split()[0])
        assert os.getpgid(child) == leader and gone(leader)  # leaderless group

        assert remote.check_worker(transport, t) == remote.RUNNING
        assert remote.stop_worker(transport, t) is True
        assert wait_gone(child)
        assert remote.check_worker(transport, t) == remote.GONE
    finally:
        kill_quietly(child)


@pytest.mark.parametrize(
    "record",
    [None, "{pid} 00000000-0000-0000-0000-000000000000 {tag} {start}\n",
     "{pid} {boot} {other} {start}\n"],
    ids=["no-record", "earlier-boot", "another-start"],
)
def test_darwin_record_naming_no_worker_of_this_start_is_gone_and_signals_nothing(
    tmp_path: Path, mac_ssh, record: str | None
) -> None:
    ssh, mac = mac_ssh
    t = target(tmp_path)
    other = stranger()
    try:
        if record is not None:
            write_record(t, record.format(
                pid=other.pid, boot=BOOT, tag=TAG, other="f" * 32, start=lstart(other.pid)
            ))
        transport = remote.SshTransport()
        assert remote.check_worker(transport, t) == remote.GONE
        assert remote.stop_worker(transport, t) is True
        time.sleep(0.2)
        assert other.poll() is None  # never signalled
    finally:
        other.kill()
        other.wait()


@pytest.mark.parametrize(
    "record",
    ["{pid} {boot} {tag} Mon Jan 1 00:00:00 2001\n",  # the number was reused
     "{pid} {boot}\n",  # a Linux record
     "{pid} {boot} {tag}\n",  # no start time
     "{pid}\n",
     "\n",
     "007 {boot} {tag} {start}\n",
     "-5 {boot} {tag} {start}\n",
     "12abc {boot} {tag} {start}\n"],
    ids=["other-start-time", "linux-record", "no-start-time", "pid-only", "empty",
         "leading-zero", "negative", "junk"],
)
def test_darwin_malformed_or_reused_records_are_unknown_and_never_signalled(
    tmp_path: Path, mac_ssh, record: str
) -> None:
    ssh, mac = mac_ssh
    t = target(tmp_path)
    other = stranger()
    try:
        write_record(t, record.format(pid=other.pid, boot=BOOT, tag=TAG, start=lstart(other.pid)))
        transport = remote.SshTransport()
        assert remote.check_worker(transport, t) == remote.UNKNOWN
        assert remote.stop_worker(transport, t) is True  # answered, stopped nothing
        time.sleep(0.2)
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


@pytest.mark.parametrize("pgid", ["0", "1"])
def test_darwin_records_of_group_0_or_1_are_unknown(tmp_path: Path, mac_ssh, pgid: str) -> None:
    """Check only: a stop built from these must never be run here (it would hit every process)."""
    ssh, mac = mac_ssh
    t = target(tmp_path)
    write_record(t, f"{pgid} {BOOT} {TAG} Mon Jan 1 00:00:00 2001\n")
    assert remote.check_worker(remote.SshTransport(), t) == remote.UNKNOWN


def test_darwin_record_matching_a_live_group_and_its_leader_is_running(
    tmp_path: Path, mac_ssh
) -> None:
    """The start time format the start writes is the one the check reads back."""
    ssh, mac = mac_ssh
    t = target(tmp_path)
    other = stranger()
    try:
        write_record(t, f"{other.pid} {BOOT} {TAG}   {lstart(other.pid)}  \n")
        assert remote.check_worker(remote.SshTransport(), t) == remote.RUNNING
    finally:
        other.kill()
        other.wait()


def test_darwin_group_holding_another_users_process_is_unknown_and_left_alone(
    tmp_path: Path, mac_ssh
) -> None:
    ssh, mac = mac_ssh
    mac.foreign_uid()
    t = target(tmp_path)
    other = stranger()
    try:
        write_record(t, f"{other.pid} {BOOT} {TAG} {lstart(other.pid)}\n")
        transport = remote.SshTransport()
        assert remote.check_worker(transport, t) == remote.UNKNOWN
        assert remote.stop_worker(transport, t) is True
        time.sleep(0.2)
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


def test_darwin_device_without_a_boot_session_refuses_start_and_cannot_tell(
    tmp_path: Path, mac_ssh
) -> None:
    ssh, mac = mac_ssh
    mac.boot.write_text("", encoding="utf-8")
    t = target(tmp_path)
    marker = tmp_path / "ran"
    proc = start(t, ["touch", str(marker)])
    proc.wait(timeout=30)
    assert proc.returncode == remote.START_REFUSED
    assert not marker.exists() and not record_of(t).exists()

    other = stranger()
    try:
        write_record(t, f"{other.pid} {BOOT} {TAG} {lstart(other.pid)}\n")
        transport = remote.SshTransport()
        assert remote.check_worker(transport, t) == remote.UNKNOWN
        assert remote.stop_worker(transport, t) is False  # exit 3: could not tell
        time.sleep(0.2)
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


def test_darwin_ps_that_lists_nothing_cannot_tell(tmp_path: Path, mac_ssh) -> None:
    ssh, mac = mac_ssh
    t = target(tmp_path)
    write_record(t, f"4242 {BOOT} {TAG} Mon Jan 1 00:00:00 2001\n")
    mac.blind_ps()
    transport = remote.SshTransport()
    assert remote.check_worker(transport, t) == remote.UNKNOWN
    assert remote.stop_worker(transport, t) is False


def test_darwin_start_refuses_unless_it_leads_its_own_process_group(tmp_path: Path) -> None:
    """Without sshd's new session the recorded group would be someone else's."""
    mac = Mac(tmp_path)
    t = target(tmp_path)
    marker = tmp_path / "ran"
    result = subprocess.run(
        ["sh", "-c", remote.start_script(t, ["touch", str(marker)], {})],
        env={"PATH": mac.path(os.environ["PATH"])}, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == remote.START_REFUSED, result.stderr
    assert not marker.exists() and not record_of(t).exists()


def test_darwin_hostile_workdir_and_environment_values_reach_the_command_verbatim(
    tmp_path: Path,
) -> None:
    mac = Mac(tmp_path)
    root = tmp_path / "w o'r\"k $(touch pwn1) `touch pwn2`"
    t = remote.RemoteTarget(
        device="mac", alias="fakepc-codex-bot", user="codex-bot",
        workdir_root=str(root), job_id="abc123", tag=TAG,
    )
    values = {"ORCH_JOB_BOT": "x'; touch pwn3 #", "ORCH_JOB_DEVICE": "$(touch pwn4)\n`touch pwn5`\""}
    report = tmp_path / "report.json"
    dump = (
        "import json, os, sys\n"
        "json.dump({'cwd': os.getcwd(), 'argv': sys.argv[1:],"
        " 'env': {k: os.environ.get(k) for k in ('ORCH_JOB_BOT', 'ORCH_JOB_DEVICE', 'ORCH_JOB_WORKDIR')}},"
        f" open({str(report)!r}, 'w'))\n"
    )
    script = remote.start_script(t, [sys.executable, "-c", dump, "a b", "$(touch pwn6)", "'"], values)
    result = subprocess.run(
        ["sh", "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=30,
        env={"PATH": mac.path(os.environ["PATH"])}, start_new_session=True,
    )
    assert result.returncode == 0, result.stderr
    seen = json.loads(report.read_text(encoding="utf-8"))
    assert seen["cwd"] == t.job_dir
    assert seen["argv"] == ["a b", "$(touch pwn6)", "'"]
    assert seen["env"] == {**values, "ORCH_JOB_WORKDIR": t.job_dir}
    assert len(record_of(t).read_text(encoding="utf-8").split()) >= 4  # the Darwin record
    assert not any(tmp_path.rglob("pwn*"))


# --- orch job run and the poller on a Mac -----------------------------------


def test_darwin_remote_job_runs_and_releases_its_slot_once_the_worker_is_gone(
    tmp_path: Path,
) -> None:
    machines, queue, pool, job_id, ssh = remote_setup(tmp_path, "codex")
    mac = Mac(tmp_path)
    proc = orch(
        "job", "run", "codex-bot", job_id, "--db", str(tmp_path / "orch.db"),
        "--machines", str(machines), "--", sys.executable, "-c", "import sys; sys.exit(3)",
        env=mac_env(ssh, mac),
    )
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 3, err
    record = (job_dir(tmp_path, "codex-bot", job_id) / remote.PID_FILE).read_text(encoding="utf-8")
    calls = ssh.calls()
    tag = calls[0]["script"].split()[2].removeprefix("ORCH_WORKER_TAG=")
    assert record.split()[1:3] == [BOOT, tag]
    assert len(calls) == 2  # the start, then the check that the worker is gone
    assert pool.placement(job_id) is None


def test_darwin_remote_child_left_in_the_group_is_stopped_before_the_slot_is_released(
    tmp_path: Path,
) -> None:
    machines, queue, pool, job_id, ssh = remote_setup(tmp_path, "codex")
    mac = Mac(tmp_path)
    child_pid = tmp_path / "child.pid"
    child: int | None = None
    try:
        proc = orch(
            "job", "run", "codex-bot", job_id, "--db", str(tmp_path / "orch.db"),
            "--machines", str(machines), "--",
            *leave_child_command(child_pid, stubborn=False),
            env=mac_env(ssh, mac),
        )
        _, err = proc.communicate(timeout=60)
        child = int(wait_file(child_pid))
        assert proc.returncode == 0, err
        assert "stopping it" in err, err
        assert wait_gone(child)
        assert pool.placement(job_id) is None
    finally:
        kill_quietly(child)


def test_darwin_remote_takeover_is_refused_while_the_dead_runners_worker_lives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    machines, queue, pool, job_id, ssh = remote_setup(tmp_path, "codex")
    mac = Mac(tmp_path)
    group_file = tmp_path / "group"
    marker = tmp_path / "second-ran"
    group: int | None = None
    script = (
        "import os, time\n"
        f"open({str(group_file)!r} + '.tmp', 'w').write(str(os.getpgid(0)))\n"
        f"os.rename({str(group_file)!r} + '.tmp', {str(group_file)!r})\n"
        "time.sleep(60)\n"
    )
    first = orch(
        "job", "run", "codex-bot", job_id, "--db", str(tmp_path / "orch.db"),
        "--machines", str(machines), "--", sys.executable, "-c", script,
        env=mac_env(ssh, mac),
    )
    try:
        group = int(wait_file(group_file, first))
        first.kill()  # the runner dies; the remote worker keeps running
        first.wait(timeout=20)

        second = orch(
            "job", "run", "codex-bot", job_id, "--db", str(tmp_path / "orch.db"),
            "--machines", str(machines), "--", "touch", str(marker),
            env=mac_env(ssh, mac),
        )
        _, err = second.communicate(timeout=30)
        assert second.returncode == 2, err
        assert "earlier worker on pc is not confirmed gone (running)" in err
        assert not marker.exists()
        assert len(starts(ssh)) == 1

        # The poller stops the orphan, and only then frees the slot.
        use_fake_ssh(monkeypatch, ssh)
        monkeypatch.setenv("PATH", mac.path(os.environ["PATH"]))
        queue.finish(job_id, owner="codex", handoff_comment_id="c1")
        monitor = DeviceMonitor(pool, config_of(machines), remote.SshTransport())
        result = monitor.reconcile(time.time(), lambda *_: None)
        assert result.stopped == (job_id,)
        assert pool.placement(job_id) is not None
        assert wait_group_gone(group)
        assert monitor.reconcile(time.time(), lambda *_: None).released == (job_id,)
    finally:
        kill_quietly(group, group=True)
        if first.poll() is None:
            first.kill()
