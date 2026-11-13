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
    state,
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

    def refuse_starts(self) -> None:
        """No boot session id for a start (it carries ORCH_WORKER_TAG): it refuses, 125."""
        self._tool(
            "sysctl",
            '[ -z "$ORCH_WORKER_TAG" ] || exit 1\n'
            '[ "$*" = "-n kern.bootsessionuuid" ] || exit 1\n'
            f"exec cat {shlex.quote(str(self.boot))}\n",
        )

    def partial_ps(self) -> None:
        """A ps that lists one unrelated process of this user, then fails."""
        self._tool("ps", f'echo "  999999 999999 {os.getuid()}"\nexit 1\n')

    def ps_listing(self, group: str, leader_start: str) -> None:
        """A ps whose only process leads group `group` as this user, started at leader_start."""
        self._tool(
            "ps",
            f'case "$*" in *lstart*) echo {shlex.quote(leader_start)};; '
            f'*) echo "  {group} {group} {os.getuid()}";; esac\n',
        )

    def pause_starts(self, gate: Path, paused: Path) -> None:
        """The first start (the only caller carrying ORCH_WORKER_TAG) to run ps
        waits, before it can write its record, until gate is removed. A check
        never waits: sshd gives it no tag."""
        real = shutil.which("ps")
        self._tool(
            "ps",
            f'if [ -n "$ORCH_WORKER_TAG" ] && [ -e {shlex.quote(str(gate))} ] '
            f"&& mkdir {shlex.quote(str(paused))} 2>/dev/null; then "
            f"while [ -e {shlex.quote(str(gate))} ]; do sleep 0.05; done; fi\n"
            f'exec {shlex.quote(str(real))} "$@"\n',
        )

    def awk(self, body: str) -> None:
        """An awk that ignores its program and runs body instead."""
        self._tool("awk", body)

    def real_awk(self) -> None:
        """Back to the awk on the rest of PATH."""
        (self.bin / "awk").unlink()

    def listing(self, line: str) -> None:
        """A ps whose process listing is line (exit 0); `-o lstart=` is the real ps."""
        real = shutil.which("ps")
        self._tool(
            "ps",
            f'case "$*" in *lstart*) exec {shlex.quote(str(real))} "$@";; esac\n'
            f"printf '%s\\n' {shlex.quote(line)}\n",
        )

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
    return Path(t.job_dir) / remote.darwin_record(t.require_tag())


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
    assert result.returncode == 3  # no record: fenced, cannot tell yet


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
    fields = (Path(t.job_dir) / remote.PID_FILE).read_text(encoding="utf-8").split()
    assert len(fields) == 2 and fields[1:] == boot_id  # the Linux `<group> <boot id>` record


# --- start, check, stop -----------------------------------------------------


def test_darwin_tagged_worker_is_running_until_its_group_is_sent_sigterm(
    tmp_path: Path, mac_ssh
) -> None:
    ssh, mac = mac_ssh
    t = target(tmp_path)
    report = tmp_path / "worker.json"
    transport = remote.SshTransport()
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
        other = target(tmp_path, tag="f" * 32)  # another start's tag: its own record
        assert remote.check_worker(transport, other) == remote.UNKNOWN  # none yet: fenced
        assert remote.check_worker(transport, other) == remote.GONE
        assert remote.check_worker(transport, t) == remote.RUNNING
        assert remote.stop_worker(transport, t) is True
        assert wait_gone(seen["pid"])
        proc.wait(timeout=20)
        assert remote.check_worker(transport, t) == remote.GONE
        assert record_of(t).exists()  # the record is never removed
    finally:
        kill_quietly(seen.get("pgid"), group=True)
        if proc.poll() is None:
            proc.kill()


def test_darwin_child_left_in_the_group_after_the_leader_exits_is_unknown_until_it_ends(
    tmp_path: Path, mac_ssh
) -> None:
    """Nothing ties a leaderless group to the start (F1.2): the slot is kept,
    nothing is signalled, and the check says gone once the group is empty."""
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

        assert remote.check_worker(transport, t) == remote.UNKNOWN
        assert remote.stop_worker(transport, t) is True  # answered, signalled nothing
        time.sleep(0.2)
        assert not gone(child)
        kill_quietly(child)
        assert wait_gone(child)
        assert remote.check_worker(transport, t) == remote.GONE
    finally:
        kill_quietly(child)


@pytest.mark.parametrize(
    "record",
    ["{pid} 00000000-0000-0000-0000-000000000000 {tag} {start}\n", "fenced\n"],
    ids=["earlier-boot", "fenced"],
)
def test_darwin_record_naming_no_worker_of_this_start_is_gone_and_signals_nothing(
    tmp_path: Path, mac_ssh, record: str
) -> None:
    ssh, mac = mac_ssh
    t = target(tmp_path)
    other = stranger()
    try:
        write_record(t, record.format(pid=other.pid, boot=BOOT, tag=TAG, start=lstart(other.pid)))
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
     "{pid} {boot} {other} {start}\n",  # names another start's tag
     "{pid} {boot}\n",  # a Linux record
     "{pid} {boot} {tag}\n",  # no start time
     "{pid}\n",
     "\n",
     "007 {boot} {tag} {start}\n",
     "-5 {boot} {tag} {start}\n",
     "12abc {boot} {tag} {start}\n",
     "{pid} not-a-boot-session {tag} {start}\n",  # r2 audit: was read as an earlier boot
     "{pid} 174C3433-704E-4945-8B27 {tag} {start}\n"],
    ids=["other-start-time", "another-tag", "linux-record", "no-start-time", "pid-only", "empty",
         "leading-zero", "negative", "junk", "bad-boot", "short-boot"],
)
def test_darwin_malformed_or_reused_records_are_unknown_and_never_signalled(
    tmp_path: Path, mac_ssh, record: str
) -> None:
    ssh, mac = mac_ssh
    t = target(tmp_path)
    other = stranger()
    try:
        write_record(t, record.format(
            pid=other.pid, boot=BOOT, tag=TAG, other="f" * 32, start=lstart(other.pid)
        ))
        transport = remote.SshTransport()
        assert remote.check_worker(transport, t) == remote.UNKNOWN
        assert remote.stop_worker(transport, t) is True  # answered, stopped nothing
        time.sleep(0.2)
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


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
    calls = ssh.calls()
    tag = calls[0]["script"].split()[2].removeprefix("ORCH_WORKER_TAG=")
    record = (job_dir(tmp_path, "codex-bot", job_id) / remote.darwin_record(tag)).read_text(
        encoding="utf-8"
    )
    assert record.split()[1:3] == [BOOT, tag]
    # the launch and its polls (#128), the check that the worker is gone, then
    # (exit 3) whether it ran the harness
    assert len(calls) >= 4 and f"= {tag} ]" in calls[-1]["script"]
    assert pool.placement(job_id) is None
    assert state(queue, job_id) == "running"  # it ran: no unstarted requeue


def test_darwin_remote_child_left_in_the_group_keeps_the_slot_until_it_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
        assert "not confirmed gone (unknown)" in err, err
        assert "stopping it" not in err, err
        assert not gone(child)  # never signalled
        assert pool.placement(job_id) is not None

        # Once the leftover child ends, the poller confirms the group empty.
        kill_quietly(child)
        assert wait_gone(child)
        use_fake_ssh(monkeypatch, ssh)
        monkeypatch.setenv("PATH", mac.path(os.environ["PATH"]))
        queue.finish(job_id, owner="codex", handoff_comment_id="c1")
        monitor = DeviceMonitor(pool, config_of(machines), remote.SshTransport())
        assert monitor.reconcile(time.time(), lambda *_: None).released == (job_id,)
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


# --- Codex review round 1 ----------------------------------------------------
# F1.1: a missing record is not evidence that no start is pending.


def wait_path(path: Path, seconds: float = 20.0) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists():
        assert time.monotonic() < deadline, f"{path} never appeared"
        time.sleep(0.05)


def test_r1_f11_darwin_check_while_a_start_is_paused_before_its_record_is_never_gone(
    tmp_path: Path, mac_ssh
) -> None:
    ssh, mac = mac_ssh
    t = target(tmp_path)
    gate, paused, marker = tmp_path / "gate", tmp_path / "paused", tmp_path / "ran"
    gate.touch()
    mac.pause_starts(gate, paused)
    proc = start(t, ["touch", str(marker)])
    try:
        wait_path(paused)
        transport = remote.SshTransport()
        # No record yet, and the start may still write one: never gone.
        assert remote.check_worker(transport, t) == remote.UNKNOWN
        assert remote.stop_worker(transport, t) is True  # answered, signalled nothing
        assert proc.poll() is None
        gate.unlink()
        proc.wait(timeout=30)
        # The check fenced the start: it can never record itself or run its command.
        assert proc.returncode == remote.START_REFUSED, proc.stderr.read()
        assert not marker.exists()
        assert remote.check_worker(transport, t) == remote.GONE
    finally:
        gate.unlink(missing_ok=True)
        if proc.poll() is None:
            proc.kill()


def test_r1_f11_darwin_takeover_while_the_dead_runners_start_is_paused_starts_no_second_worker(
    tmp_path: Path,
) -> None:
    machines, queue, pool, job_id, ssh = remote_setup(tmp_path, "codex")
    mac = Mac(tmp_path)
    gate, paused = tmp_path / "gate", tmp_path / "paused"
    first_ran, second_ran, third_ran = (tmp_path / n for n in ("first", "second", "third"))
    gate.touch()
    mac.pause_starts(gate, paused)

    def run(marker: Path) -> subprocess.Popen:
        return orch(
            "job", "run", "codex-bot", job_id, "--db", str(tmp_path / "orch.db"),
            "--machines", str(machines), "--", "touch", str(marker),
            env=mac_env(ssh, mac),
        )

    first = run(first_ran)
    try:
        wait_path(paused)
        first.kill()  # the runner dies while its start has not written a record
        first.wait(timeout=20)

        second = run(second_ran)
        _, err = second.communicate(timeout=30)
        assert second.returncode == 2, err
        assert "earlier worker on pc is not confirmed gone (unknown)" in err
        assert len(starts(ssh)) == 1

        gate.unlink()  # the orphaned start resumes, finds the fence and refuses
        assert wait_gone(starts(ssh)[0]["pid"])
        assert not first_ran.exists()

        third = run(third_ran)
        _, err = third.communicate(timeout=30)
        assert third.returncode == 0, err
        assert third_ran.exists() and not first_ran.exists() and not second_ran.exists()
        assert len(starts(ssh)) == 2
        assert pool.placement(job_id) is None
    finally:
        gate.unlink(missing_ok=True)
        if first.poll() is None:
            first.kill()


# F1.2: a leaderless group carries no evidence that it is this start's.


def test_r1_f12_darwin_reused_leaderless_group_is_unknown_and_never_signalled(
    tmp_path: Path, mac_ssh
) -> None:
    """The record names group P; P emptied and the number now names another
    group of this user whose leader has exited and left a child."""
    ssh, mac = mac_ssh
    t = target(tmp_path)
    child_pid = tmp_path / "child.pid"
    leader = subprocess.Popen(
        leave_child_command(child_pid, stubborn=False), start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    child: int | None = None
    try:
        leader.wait(timeout=30)
        child = int(wait_file(child_pid))
        assert os.getpgid(child) == leader.pid  # a live group with no leader
        write_record(t, f"{leader.pid} {BOOT} {TAG} Mon Jan 1 00:00:00 2001\n")
        transport = remote.SshTransport()
        assert remote.check_worker(transport, t) == remote.UNKNOWN
        assert remote.stop_worker(transport, t) is True  # answered, signalled nothing
        time.sleep(0.2)
        assert not gone(child)
    finally:
        kill_quietly(child)


# Codex PR #129 r2: a stop snapshots the group it signals, so a member
# that outlives the leader is still known to be the start's.

# A leader that starts a child ignoring SIGTERM in its own group, writes both
# pids, and waits for the child.
WAITS_ON_STUBBORN_CHILD = (
    "import os, subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import signal, time; "
    "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)'])\n"
    "open(sys.argv[1] + '.tmp', 'w').write(f'{os.getpid()} {child.pid}')\n"
    "os.rename(sys.argv[1] + '.tmp', sys.argv[1])\n"
    "child.wait()\n"
)


def members_of(t: remote.RemoteTarget) -> Path:
    return Path(t.job_dir) / f".orch-worker-{t.require_tag()}.members.pid"


def test_r2_darwin_member_left_after_a_stop_killed_the_leader_is_running_until_killed(
    tmp_path: Path, mac_ssh
) -> None:
    ssh, mac = mac_ssh
    t = target(tmp_path)
    pids = tmp_path / "pids"
    transport = remote.SshTransport()
    proc = start(t, [sys.executable, "-c", WAITS_ON_STUBBORN_CHILD, str(pids)])
    leader = child = None
    try:
        leader, child = map(int, wait_file(pids, proc).split())
        assert os.getpgid(child) == leader
        assert remote.check_worker(transport, t) == remote.RUNNING
        assert remote.stop_worker(transport, t) is True
        assert wait_gone(leader)
        assert not gone(child)
        assert f"{child} {lstart(child)}" in members_of(t).read_text().splitlines()

        assert remote.check_worker(transport, t) == remote.RUNNING
        assert remote.stop_worker(transport, t) is True
        time.sleep(0.2)
        assert not gone(child)  # SIGTERM again: ignored
        assert remote.stop_worker(transport, t, kill=True) is True
        assert wait_gone(child)
        assert remote.check_worker(transport, t) == remote.GONE
    finally:
        kill_quietly(child)
        kill_quietly(leader)
        if proc.poll() is None:
            proc.kill()


@pytest.mark.parametrize("member", ["wrong-start", "other-group"])
def test_r2_darwin_snapshot_member_that_does_not_match_proves_nothing(
    tmp_path: Path, mac_ssh, member: str
) -> None:
    """A leaderless group stays unknown unless a snapshot member is in it
    with its recorded start time."""
    ssh, mac = mac_ssh
    t = target(tmp_path)
    child_pid = tmp_path / "child.pid"
    leader = subprocess.Popen(
        leave_child_command(child_pid, stubborn=False), start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    other = stranger()
    child: int | None = None
    try:
        leader.wait(timeout=30)
        child = int(wait_file(child_pid))
        assert os.getpgid(child) == leader.pid
        write_record(t, f"{leader.pid} {BOOT} {TAG} Mon Jan 1 00:00:00 2001\n")
        line = {
            "wrong-start": f"{child} Mon Jan 1 00:00:00 2001",
            "other-group": f"{other.pid} {lstart(other.pid)}",
        }[member]
        members_of(t).write_text(line + "\n", encoding="utf-8")
        transport = remote.SshTransport()
        assert remote.check_worker(transport, t) == remote.UNKNOWN
        assert remote.stop_worker(transport, t, kill=True) is True  # signalled nothing
        time.sleep(0.2)
        assert not gone(child) and other.poll() is None

        members_of(t).write_text(f"{child} {lstart(child)}\n", encoding="utf-8")
        assert remote.check_worker(transport, t) == remote.RUNNING
    finally:
        kill_quietly(child)
        other.kill()
        other.wait()


# F1.3: GONE from the process listing needs ps to have succeeded.


def test_r1_f13_darwin_ps_that_fails_after_listing_part_cannot_tell(
    tmp_path: Path, mac_ssh
) -> None:
    ssh, mac = mac_ssh
    t = target(tmp_path)
    other = stranger()  # the live worker ps failed to list
    try:
        write_record(t, f"{other.pid} {BOOT} {TAG} {lstart(other.pid)}\n")
        mac.partial_ps()
        transport = remote.SshTransport()
        assert remote.check_worker(transport, t) == remote.UNKNOWN
        assert remote.stop_worker(transport, t) is False  # exit 3: could not tell
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


# The group 0/1 refusal on its own: everything else in the record matches.


@pytest.mark.parametrize("pgid", ["0", "1"])
def test_r1_darwin_record_of_group_0_or_1_is_unknown_even_when_all_else_matches(
    tmp_path: Path, mac_ssh, pgid: str
) -> None:
    """Check only: a stop built from these must never be run here (it would hit every process)."""
    ssh, mac = mac_ssh
    t = target(tmp_path)
    when = "Mon Jan 1 00:00:00 2001"
    mac.ps_listing(pgid, when)
    write_record(t, f"{pgid} {BOOT} {TAG} {when}\n")
    assert remote.check_worker(remote.SshTransport(), t) == remote.UNKNOWN


def test_r1_f11_darwin_start_that_never_ran_is_fenced_then_its_slot_is_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A start that wrote no record (here refused, 125) keeps its slot for one
    check: that check fences the tag, and the next one says gone."""
    machines, queue, pool, job_id, ssh = remote_setup(tmp_path, "codex")
    mac = Mac(tmp_path)
    mac.refuse_starts()
    marker = tmp_path / "ran"
    proc = orch(
        "job", "run", "codex-bot", job_id, "--db", str(tmp_path / "orch.db"),
        "--machines", str(machines), "--", "touch", str(marker),
        env=mac_env(ssh, mac),
    )
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == remote.START_REFUSED, err
    assert not marker.exists()
    assert "not confirmed gone (unknown)" in err, err
    placement = pool.placement(job_id)
    assert placement is not None and placement.worker_tag is not None
    fence = job_dir(tmp_path, "codex-bot", job_id) / remote.darwin_record(placement.worker_tag)
    assert fence.read_text(encoding="utf-8").split() == ["fenced"]

    use_fake_ssh(monkeypatch, ssh)
    monkeypatch.setenv("PATH", mac.path(os.environ["PATH"]))
    queue.finish(job_id, owner="codex", handoff_comment_id="c1")
    monitor = DeviceMonitor(pool, config_of(machines), remote.SshTransport())
    assert monitor.reconcile(time.time(), lambda *_: None).released == (job_id,)


# --- Codex review round 2 ----------------------------------------------------
# F2.1: GONE from the process listing needs the group parse (awk) to succeed
# and print exactly three fields, each 0 or 1. Anything else cannot tell.


@pytest.mark.parametrize(
    "awk",
    ["exit 1\n",
     "exit 0\n",
     "echo '1 0 1'; exit 1\n",
     "echo '0 0'\n",
     "echo '0 0 0 0'\n",
     "echo '0 0 2'\n",
     "echo 'x 0 0'\n",
     "echo '00 0 0'\n"],
    ids=["fails-silent", "succeeds-silent", "right-answer-but-fails", "two-fields", "four-fields",
         "not-0-or-1", "not-a-number", "two-digits"],
)
def test_r2_f21_darwin_group_parse_that_fails_or_is_malformed_cannot_tell(
    tmp_path: Path, mac_ssh, awk: str
) -> None:
    ssh, mac = mac_ssh
    t = target(tmp_path)
    other = stranger()  # the live worker the record names
    try:
        write_record(t, f"{other.pid} {BOOT} {TAG} {lstart(other.pid)}\n")
        transport = remote.SshTransport()
        assert remote.check_worker(transport, t) == remote.RUNNING  # with the real awk
        mac.awk(awk)
        assert remote.check_worker(transport, t) == remote.UNKNOWN
        assert remote.stop_worker(transport, t) is False  # exit 3: could not tell
        time.sleep(0.2)
        assert other.poll() is None  # never signalled
    finally:
        other.kill()
        other.wait()


def test_r2_f21_darwin_awk_that_reads_only_part_of_the_listing_cannot_tell(
    tmp_path: Path, mac_ssh
) -> None:
    """The pipe into awk loses all but the first line; the worker is not on it.
    awk itself succeeds and prints a well-formed `0 0 0`."""
    ssh, mac = mac_ssh
    real = shutil.which("awk")
    t = target(tmp_path)
    other = stranger()
    try:
        write_record(t, f"{other.pid} {BOOT} {TAG} {lstart(other.pid)}\n")
        transport = remote.SshTransport()
        assert remote.check_worker(transport, t) == remote.RUNNING  # with the whole listing
        mac.awk(f'head -n 1 | exec {shlex.quote(str(real))} "$@"\n')
        assert remote.check_worker(transport, t) == remote.UNKNOWN
        assert remote.stop_worker(transport, t) is False  # exit 3: could not tell
        time.sleep(0.2)
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


def test_r2_f21_darwin_awk_failure_keeps_a_live_workers_slot_and_refuses_takeover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The takeover guard and the monitor both see UNKNOWN: nothing is
    released, stopped or started while awk fails. Once awk works again, the
    monitor stops the orphan and then releases its slot."""
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
        mac.awk("exit 1\n")

        second = orch(
            "job", "run", "codex-bot", job_id, "--db", str(tmp_path / "orch.db"),
            "--machines", str(machines), "--", "touch", str(marker),
            env=mac_env(ssh, mac),
        )
        _, err = second.communicate(timeout=30)
        assert second.returncode == 2, err
        assert "earlier worker on pc is not confirmed gone (unknown)" in err
        assert not marker.exists()
        assert len(starts(ssh)) == 1

        use_fake_ssh(monkeypatch, ssh)
        monkeypatch.setenv("PATH", mac.path(os.environ["PATH"]))
        queue.finish(job_id, owner="codex", handoff_comment_id="c1")
        monitor = DeviceMonitor(pool, config_of(machines), remote.SshTransport())
        result = monitor.reconcile(time.time(), lambda *_: None)
        assert result.released == () and result.stopped == ()
        assert pool.placement(job_id) is not None
        assert group_alive(group)

        mac.real_awk()
        assert monitor.reconcile(time.time(), lambda *_: None).stopped == (job_id,)
        assert wait_group_gone(group)
        assert monitor.reconcile(time.time(), lambda *_: None).released == (job_id,)
    finally:
        kill_quietly(group, group=True)
        if first.poll() is None:
            first.kill()


# Round 2 audit: the other observations a GONE rests on must be well formed too.


@pytest.mark.parametrize(
    "boot",
    ["not-a-boot-session", "174C3433-704E-4945", BOOT + "0", BOOT.replace("-", "x")],
    ids=["junk", "truncated", "too-long", "no-dashes"],
)
def test_r2_darwin_boot_session_id_that_is_not_a_uuid_refuses_start_and_cannot_tell(
    tmp_path: Path, mac_ssh, boot: str
) -> None:
    """sysctl exits 0 but prints no boot session id: before, a live worker's
    record read as an earlier boot (GONE)."""
    ssh, mac = mac_ssh
    t = target(tmp_path)
    other = stranger()
    try:
        write_record(t, f"{other.pid} {BOOT} {TAG} {lstart(other.pid)}\n")
        mac.boot.write_text(boot + "\n", encoding="utf-8")
        transport = remote.SshTransport()
        assert remote.check_worker(transport, t) == remote.UNKNOWN
        assert remote.stop_worker(transport, t) is False  # exit 3: could not tell
        time.sleep(0.2)
        assert other.poll() is None

        t2 = target(tmp_path, job_id="job2")
        marker = tmp_path / "ran"
        proc = start(t2, ["touch", str(marker)])
        proc.wait(timeout=30)
        assert proc.returncode == remote.START_REFUSED
        assert not marker.exists() and not record_of(t2).exists()
    finally:
        other.kill()
        other.wait()


@pytest.mark.parametrize(
    "line",
    ["garbage", "{pid}", "{pid} {pid}", "{pid} {pid} {uid} extra", "{pid} x{pid} {uid}"],
    ids=["junk", "one-field", "two-fields", "four-fields", "not-a-number"],
)
def test_r2_darwin_ps_listing_that_is_not_three_numbers_a_line_cannot_tell(
    tmp_path: Path, mac_ssh, line: str
) -> None:
    """ps exits 0 but its listing is not `<group> <pid> <uid>` lines."""
    ssh, mac = mac_ssh
    t = target(tmp_path)
    other = stranger()
    try:
        write_record(t, f"{other.pid} {BOOT} {TAG} {lstart(other.pid)}\n")
        mac.listing(line.format(pid=other.pid, uid=os.getuid()))
        transport = remote.SshTransport()
        assert remote.check_worker(transport, t) == remote.UNKNOWN
        assert remote.stop_worker(transport, t) is False  # exit 3: could not tell
        time.sleep(0.2)
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


@pytest.mark.parametrize("uid", ["", "abc", "501 502"], ids=["empty", "junk", "two-words"])
def test_r2_darwin_user_id_that_is_not_a_number_cannot_tell(
    tmp_path: Path, mac_ssh, uid: str
) -> None:
    ssh, mac = mac_ssh
    mac._tool("id", f"echo {shlex.quote(uid)}\n")
    t = target(tmp_path)
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
