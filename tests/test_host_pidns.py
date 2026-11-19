"""#308: the host PID namespace check and the service scripts' refusal."""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

import pytest

from tests.conftest import reap_home
from tests.procs import alive

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "deploy" / "host-pidns.sh"
HOST_NS = "pid:[4026531836]"
SANDBOX_NS = "pid:[4026532782]"
OLD_NS = "pid:[4026531111]"
WSL_NS = "pid:[4026532216]"
INITIAL_USERNS = "         0          0 4294967295\n"
WSL_ROOTFS = "rootfs"
SANDBOX_USERNS = "         0       1000          1\n"


def fake_proc(tmp_path: Path, nspid: str | None, ns: str = HOST_NS,
              uid_map: str | None = None, pids: dict[int, str] | None = None,
              init: str | None = None, procs: dict[int, tuple[str, int]] | None = None,
              init_fs: str | None = None, init_comm: str | None = None,
              init_exe: str | None = None) -> Path:
    proc = tmp_path / "proc"
    (proc / "self" / "ns").mkdir(parents=True)
    lines = "Name:\tsh\n" + (f"NSpid:\t{nspid}\n" if nspid is not None else "")
    (proc / "self" / "status").write_text(lines, encoding="utf-8")
    (proc / "self" / "ns" / "pid").symlink_to(ns)
    if uid_map is not None:
        (proc / "self" / "uid_map").write_text(uid_map, encoding="utf-8")
    for pid, pid_ns in (pids or {}).items():
        (proc / str(pid) / "ns").mkdir(parents=True)
        (proc / str(pid) / "ns" / "pid").symlink_to(pid_ns)
    if init is not None:
        (proc / "1").mkdir(exist_ok=True)
        (proc / "1" / "cmdline").write_text(init.replace(" ", "\0") + "\0", encoding="utf-8")
        comm = (init_comm or "init(Ubuntu)") if init == "/init" else "systemd"
        (proc / "1" / "stat").write_text(f"1 ({comm}) S 0 0 0\n", encoding="utf-8")
    for pid, (cmdline, ppid) in (procs or {}).items():
        (proc / str(pid)).mkdir(exist_ok=True)
        (proc / str(pid) / "cmdline").write_text(cmdline + "\0", encoding="utf-8")
        comm = init_comm or "init-systemd(Ub"
        (proc / str(pid) / "stat").write_text(f"{pid} ({comm}) S {ppid} 0 0\n",
                                              encoding="utf-8")
    for cmdline in proc.glob("[0-9]*/cmdline"):
        if init_exe is not None and cmdline.read_text(encoding="utf-8").split("\0")[0] == "/init":
            (cmdline.parent / "exe").symlink_to(init_exe)
    if init_fs is not None:
        for cmdline in proc.glob("[0-9]*/cmdline"):
            if cmdline.read_text(encoding="utf-8").split("\0")[0] == "/init":
                (cmdline.parent / "mountinfo").write_text(
                    "80 65 8:48 / / rw,relatime - overlay overlay rw\n"
                    f"109 80 0:2 /init /init ro - {init_fs} {init_fs} rw\n",
                    encoding="utf-8")
    return proc


def record(home: Path, value: str) -> Path:
    path = home / ".config" / "factory" / "host-pidns"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    return path


def in_host(home: Path, proc: Path) -> subprocess.CompletedProcess:
    shell = shutil.which("dash") or "sh"
    return subprocess.run(
        [shell, "-c", '. "$1" && factory_in_host_pidns', "sh", str(HELPER)],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "ORCH_PROC_DIR": str(proc)},
        text=True, capture_output=True, timeout=10, check=False,
    )


@pytest.mark.parametrize(
    ("nspid", "recorded", "host"),
    [
        ("4484", None, True),
        ("4484", HOST_NS, True),
        ("4484", SANDBOX_NS, False),
        ("1728748\t61", None, False),
        ("1728748\t61", HOST_NS, False),
        (None, None, True),
    ],
)
def test_factory_in_host_pidns(tmp_path: Path, nspid: str | None, recorded: str | None,
                               host: bool) -> None:
    proc = fake_proc(tmp_path, nspid)
    if recorded:
        record(tmp_path, recorded)

    result = in_host(tmp_path, proc)

    assert (result.returncode == 0) is host, result.stderr
    assert ("not the host PID namespace" in result.stderr) is not host


def test_no_proc_counts_as_host(tmp_path: Path) -> None:
    record(tmp_path, SANDBOX_NS)
    assert in_host(tmp_path, tmp_path / "no-proc").returncode == 0


SERVICES = [
    *[("waiter-service.sh", ["claude-bot", c], "waiter-claude-bot.pid")
      for c in ["_supervise", "start", "stop", "restart", "ensure"]],
    *[("poller-service.sh", [c], "poller.pid") for c in ["start", "stop", "restart", "ensure"]],
    *[("linear-webhook-service.sh", [c], "linear-webhook-listener.pid")
      for c in ["_listener-loop", "_tunnel-loop", "start", "stop", "restart", "ensure"]],
    *[("self-heal.sh", [c], "self-heal.pid") for c in ["_loop", "start", "stop", "restart", "ensure"]],
]


@pytest.mark.parametrize(("script", "args", "pidfile"), SERVICES,
                         ids=[f"{s}-{a[-1]}" for s, a, _ in SERVICES])
def test_service_refuses_outside_the_host_pid_namespace(
    tmp_path: Path, script: str, args: list[str], pidfile: str,
) -> None:
    home = tmp_path / "home"
    state = home / ".local" / "state" / "factory"
    state.mkdir(parents=True)
    (state / pidfile).write_text("1667506\n", encoding="utf-8")
    before = sorted(p.relative_to(home) for p in home.rglob("*"))
    proc = fake_proc(tmp_path, "1728748\t61")

    result = subprocess.run(
        ["bash", str(ROOT / "deploy" / script), *args],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "ORCH_PROC_DIR": str(proc)},
        text=True, capture_output=True, timeout=30, check=False,
    )

    assert result.returncode == 4, result.stderr
    assert f"refusing to {args[-1]} from a non-host PID namespace" in result.stderr
    assert (state / pidfile).read_text(encoding="utf-8") == "1667506\n"
    assert sorted(p.relative_to(home) for p in home.rglob("*")) == before


def _run(command: list[str], home: Path, proc: Path,
         **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "ORCH_PROC_DIR": str(proc), **extra},
        text=True, capture_output=True, timeout=30, check=False,
    )


def _service(script: str, *args: str) -> list[str]:
    return ["bash", str(ROOT / "deploy" / script), *args]


REFRESHING = {
    "helper": [shutil.which("dash") or "sh", "-c",
               '. "$1" && factory_host_pidns_or_refuse start && factory_host_pidns_or_refuse ensure',
               "sh", str(HELPER)],
    "poller-stop": _service("poller-service.sh", "stop"),
    "waiter-stop": _service("waiter-service.sh", "claude-bot", "stop"),
    "linear-webhook-stop": _service("linear-webhook-service.sh", "stop"),
    "self-heal-stop": _service("self-heal.sh", "stop"),
}


@pytest.mark.parametrize("command", REFRESHING.values(), ids=REFRESHING.keys())
def test_service_refreshes_a_stale_record_after_a_restart(tmp_path: Path,
                                                          command: list[str]) -> None:
    home = tmp_path / "home"
    state = home / ".local" / "state" / "factory"
    state.mkdir(parents=True)
    path = record(home, OLD_NS)
    proc = fake_proc(tmp_path, "4484", HOST_NS, INITIAL_USERNS)

    result = _run(command, home, proc)

    assert result.returncode == 0, result.stderr
    assert path.read_text(encoding="utf-8") == HOST_NS + "\n"
    assert OLD_NS in result.stderr and HOST_NS in result.stderr
    assert "refusing" not in result.stderr


WSL_INIT = {2: ("/init", 1)}


@pytest.mark.parametrize(
    ("init", "procs"),
    [("/init", None), ("/sbin/init", WSL_INIT), ("/usr/lib/systemd/systemd --system", WSL_INIT)],
    ids=["wsl-init", "wsl-systemd-sbin-init", "wsl-systemd"],
)
@pytest.mark.parametrize("exe", [None, "/init"], ids=["exe-unreadable", "exe-init"])
def test_wsl_distro_refreshes_a_stale_record_after_a_restart(
    tmp_path: Path, init: str, procs: dict[int, tuple[str, int]] | None, exe: str | None,
) -> None:
    home = tmp_path / "home"
    (home / ".local" / "state" / "factory").mkdir(parents=True)
    path = record(home, OLD_NS)
    proc = fake_proc(tmp_path, "4484", WSL_NS, INITIAL_USERNS, init=init, procs=procs,
                     init_fs=WSL_ROOTFS, init_exe=exe)

    result = _run(REFRESHING["helper"], home, proc)

    assert result.returncode == 0, result.stderr
    assert path.read_text(encoding="utf-8") == WSL_NS + "\n"


def test_linear_forgets_child_records_of_a_retired_namespace(tmp_path: Path) -> None:
    home = tmp_path / "home"
    state = home / ".local" / "state" / "factory"
    state.mkdir(parents=True)
    stale = state / "linear-webhook-listener-child.pid"
    stale.write_text(f"4242 100 {OLD_NS}\n", encoding="utf-8")
    other = state / "linear-webhook-tunnel-child.pid"
    other.write_text(f"4343 200 {SANDBOX_NS}\n", encoding="utf-8")
    record(home, OLD_NS)
    proc = fake_proc(tmp_path, "4484", HOST_NS, INITIAL_USERNS)

    refreshed = _run(REFRESHING["helper"], home, proc)
    assert refreshed.returncode == 0, refreshed.stderr
    assert stale.exists(), refreshed.stderr
    stopped = _run(_service("linear-webhook-service.sh", "stop"), home, proc)

    assert not stale.exists(), stopped.stderr
    assert other.read_text(encoding="utf-8") == f"4343 200 {SANDBOX_NS}\n"
    assert f"{other} names a process in another PID namespace" in stopped.stderr


def test_refresh_keeps_a_namespace_where_a_linear_child_of_another_state_dir_runs(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / ".local" / "state" / "factory").mkdir(parents=True)
    state = home / "custom-state"
    state.mkdir()
    child = state / "linear-webhook-listener-child.pid"
    child.write_text(f"4242 100 {OLD_NS}\n", encoding="utf-8")
    path = record(home, OLD_NS)
    proc = fake_proc(tmp_path, "4484", HOST_NS, INITIAL_USERNS, {4242: OLD_NS})

    refreshed = _run(REFRESHING["helper"], home, proc)
    stopped = _run(_service("linear-webhook-service.sh", "stop"), home, proc,
                   ORCH_STATE_DIR=str(state))

    assert refreshed.returncode == 4, refreshed.stderr
    assert f"pid 4242 still runs in {OLD_NS}" in refreshed.stderr
    assert path.read_text(encoding="utf-8") == OLD_NS + "\n"
    assert not (home / ".config" / "factory" / "host-pidns-retired").exists()
    assert child.read_text(encoding="utf-8") == f"4242 100 {OLD_NS}\n", stopped.stderr


@pytest.mark.parametrize("visible", [4242, 9999], ids=["same-pid", "other-pid"])
def test_linear_keeps_a_retired_namespace_child_record_while_its_namespace_runs(
    tmp_path: Path, visible: int,
) -> None:
    home = tmp_path / "home"
    state = home / "custom-state"
    state.mkdir(parents=True)
    child = state / "linear-webhook-listener-child.pid"
    child.write_text(f"4242 100 {OLD_NS}\n", encoding="utf-8")
    path = record(home, HOST_NS)
    path.with_name("host-pidns-retired").write_text(OLD_NS + "\n", encoding="utf-8")
    proc = fake_proc(tmp_path, "4484", HOST_NS, INITIAL_USERNS, {visible: OLD_NS})

    stopped = _run(_service("linear-webhook-service.sh", "stop"), home, proc,
                   ORCH_STATE_DIR=str(state))

    assert stopped.returncode != 0, stopped.stderr
    assert "cannot verify it is gone" in stopped.stderr
    assert child.read_text(encoding="utf-8") == f"4242 100 {OLD_NS}\n"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.parametrize("custom_state", [False, True], ids=["own-state", "custom-state"])
def test_linear_listener_starts_after_a_restart_left_its_child_record(
    tmp_path: Path, custom_state: bool,
) -> None:
    home = tmp_path / "home"
    default_state = home / ".local" / "state" / "factory"
    default_state.mkdir(parents=True)
    state = home / "custom-state" if custom_state else default_state
    state.mkdir(exist_ok=True)
    (state / "linear-webhook-listener-child.pid").write_text(f"4242 100 {OLD_NS}\n",
                                                             encoding="utf-8")
    record(home, OLD_NS)
    proc = fake_proc(tmp_path, "4484", HOST_NS, INITIAL_USERNS)
    started = home / "serve-started"
    serve = home / "fake-orch"
    serve.write_text(f'#!/bin/sh\ntouch "{started}"\nsleep 1\n', encoding="utf-8")
    serve.chmod(0o755)
    extra = {"ORCH_BIN": str(serve), "LINEAR_WEBHOOK_TUNNEL_HOST": "",
             "LINEAR_WEBHOOK_PORT": str(_free_port()), "LINEAR_WEBHOOK_START_WAIT": "2",
             "LINEAR_WEBHOOK_RETRY_SEC": "0.1", "LINEAR_WEBHOOK_RETRY_MAX_SEC": "0.1"}
    if custom_state:
        refreshed = _run(REFRESHING["helper"], home, proc)
        assert refreshed.returncode == 0, refreshed.stderr
        extra["ORCH_STATE_DIR"] = str(state)

    try:
        result = _run(_service("linear-webhook-service.sh", "ensure"), home, proc, **extra)

        assert result.returncode == 0, result.stderr
        deadline = time.monotonic() + 10
        while not started.exists():
            assert time.monotonic() < deadline, (state / "linear-webhook-listener.log").read_text(
                encoding="utf-8")
            time.sleep(0.1)
    finally:
        _run(_service("linear-webhook-service.sh", "stop"), home, proc, **extra)
        reap_home(home)


GATED = [(s, [*a, c]) for s, a in [("waiter-service.sh", ["claude-bot"]), ("poller-service.sh", []),
                                   ("linear-webhook-service.sh", []), ("self-heal.sh", [])]
         for c in ["start", "ensure"]]


@pytest.mark.parametrize(("script", "args"), GATED, ids=[f"{s}-{a[-1]}" for s, a in GATED])
def test_service_refuses_while_a_service_lives_in_the_recorded_namespace(
    tmp_path: Path, script: str, args: list[str],
) -> None:
    home = tmp_path / "home"
    state = home / ".local" / "state" / "factory"
    state.mkdir(parents=True)
    (state / "poller.pid").write_text("4242\n", encoding="utf-8")
    path = record(home, OLD_NS)
    proc = fake_proc(tmp_path, "4484", HOST_NS, INITIAL_USERNS, {4242: OLD_NS})

    result = _run(_service(script, *args), home, proc)

    assert result.returncode == 4, result.stderr
    assert f"refusing to {args[-1]} from a non-host PID namespace" in result.stderr
    assert "4242" in result.stderr
    assert path.read_text(encoding="utf-8") == OLD_NS + "\n"


@pytest.mark.parametrize(
    ("nspid", "uid_map", "init", "extra", "procs", "init_fs"),
    [
        ("4484", INITIAL_USERNS, "/init", {"ORCH_JOB_PROOF": "x"}, None, WSL_ROOTFS),
        ("4484", INITIAL_USERNS, "/init", {"CODEX_SANDBOX": "seatbelt"}, None, WSL_ROOTFS),
        ("1728748\t61", INITIAL_USERNS, "/init", {}, None, WSL_ROOTFS),
        ("61", SANDBOX_USERNS, "/init", {}, None, WSL_ROOTFS),
        ("61", None, "/init", {}, None, WSL_ROOTFS),
        ("61", INITIAL_USERNS, None, {}, None, None),
        ("61", INITIAL_USERNS, "/bin/sh -c poller-service.sh stop", {}, None, None),
        ("61", INITIAL_USERNS, "/sbin/docker-init -- sh", {}, None, None),
        ("61", INITIAL_USERNS, "bwrap --unshare-pid sh", {}, None, None),
        ("61", INITIAL_USERNS, "/sbin/init", {}, None, None),
        ("61", INITIAL_USERNS, "/usr/lib/systemd/systemd --system", {}, None, None),
        ("61", INITIAL_USERNS, "/sbin/init", {}, {7: ("/init", 5)}, WSL_ROOTFS),
        ("61", INITIAL_USERNS, "/init", {}, None, None),
        ("61", INITIAL_USERNS, "/init", {}, None, "overlay"),
        ("61", INITIAL_USERNS, "/sbin/init", {}, {2: ("/init", 1)}, None),
        ("61", INITIAL_USERNS, "/sbin/init", {}, {2: ("/init", 1)}, "ext4"),
    ],
    ids=["job", "codex-sandbox", "two-field-nspid", "own-proc-sandbox", "no-uid-map",
         "privileged-sandbox-no-pid1", "privileged-sandbox-shell", "privileged-docker-init",
         "privileged-bwrap", "privileged-sbin-init", "privileged-systemd",
         "privileged-init-not-child-of-pid1", "privileged-pid1-named-init",
         "privileged-pid1-init-from-image", "privileged-child-named-init",
         "privileged-child-init-from-image"],
)
def test_refresh_never_runs_outside_the_host(
    tmp_path: Path, nspid: str, uid_map: str | None, init: str | None, extra: dict[str, str],
    procs: dict[int, tuple[str, int]] | None, init_fs: str | None,
) -> None:
    _assert_sandbox_refused(tmp_path, nspid, uid_map, init, extra, procs, init_fs)


@pytest.mark.parametrize(("init", "procs"), [("/init", None), ("/sbin/init", WSL_INIT)],
                         ids=["pid1", "child-of-pid1"])
@pytest.mark.parametrize(("comm", "exe"),
                         [("sleep", None), ("init", None), ("sleep", "/usr/bin/sleep"),
                          ("init(Ubuntu)", "/usr/bin/sleep"),
                          ("init-systemd(Ub", "/usr/bin/sleep")],
                         ids=["comm-sleep", "comm-init", "exe-sleep", "renamed-init",
                              "renamed-init-systemd"])
def test_refresh_refuses_a_sandbox_that_inherits_wsl_init_mount(
    tmp_path: Path, init: str, procs: dict[int, tuple[str, int]] | None, comm: str,
    exe: str | None,
) -> None:
    _assert_sandbox_refused(tmp_path, "61", INITIAL_USERNS, init, {}, procs, WSL_ROOTFS, comm,
                            exe)


def _assert_sandbox_refused(
    tmp_path: Path, nspid: str, uid_map: str | None, init: str | None, extra: dict[str, str],
    procs: dict[int, tuple[str, int]] | None, init_fs: str | None, init_comm: str | None = None,
    init_exe: str | None = None,
) -> None:
    home = tmp_path / "home"
    state = home / ".local" / "state" / "factory"
    state.mkdir(parents=True)
    child = state / "linear-webhook-listener-child.pid"
    child.write_text(f"4242 100 {OLD_NS}\n", encoding="utf-8")
    path = record(home, OLD_NS)
    proc = fake_proc(tmp_path, nspid, SANDBOX_NS, uid_map, init=init, procs=procs,
                     init_fs=init_fs, init_comm=init_comm, init_exe=init_exe)

    result = _run(_service("poller-service.sh", "stop"), home, proc, **extra)

    assert result.returncode == 4, result.stderr
    assert "refusing to stop from a non-host PID namespace" in result.stderr
    assert path.read_text(encoding="utf-8") == OLD_NS + "\n"
    assert child.read_text(encoding="utf-8") == f"4242 100 {OLD_NS}\n"
    assert not (state / "poller.stopped").exists()
    assert not (home / ".config" / "factory" / "host-pidns-retired").exists()


def test_waiter_status_still_answers_outside_the_host_pid_namespace(tmp_path: Path) -> None:
    proc = fake_proc(tmp_path, "1728748\t61")
    result = subprocess.run(
        ["bash", str(ROOT / "deploy" / "waiter-service.sh"), "claude-bot", "status"],
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "ORCH_PROC_DIR": str(proc)},
        text=True, capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 3, result.stderr
    assert "refusing" not in result.stderr


def test_factory_detach_survives_its_callers_group_dying_the_moment_it_returns(
    tmp_path: Path,
) -> None:
    shell = shutil.which("dash") or "sh"
    launch = ('. "$1"; factory_detach sleep 60 >/dev/null 2>&1 </dev/null; '
              'echo $! >"$2"; kill -KILL 0')
    for trial in range(20):
        pidfile = tmp_path / f"pid-{trial}"
        caller = subprocess.Popen([shell, "-c", launch, "sh", str(HELPER), str(pidfile)],
                                  env={"PATH": "/usr/bin:/bin"}, process_group=0)
        caller.wait(timeout=30)
        pid = int(pidfile.read_text())
        try:
            assert alive(pid), f"trial {trial}: the caller's group took the launch down"
            assert os.getsid(pid) == pid, f"trial {trial}: the launch kept the caller's session"
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
