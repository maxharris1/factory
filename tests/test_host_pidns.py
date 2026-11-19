"""#308: the host PID namespace check and the service scripts' refusal."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from pathlib import Path

import pytest

from tests.procs import alive

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "deploy" / "host-pidns.sh"
HOST_NS = "pid:[4026531836]"
SANDBOX_NS = "pid:[4026532782]"
OLD_NS = "pid:[4026531111]"
OLD_BOOT = "6b1c0c1e-4f0e-4d4a-9b7e-0d3f2a1b9c01"
NEW_BOOT = "e2a7d9f4-1c3b-4a5e-8f60-7b9d0c2e4a13"


def fake_proc(tmp_path: Path, nspid: str | None, ns: str = HOST_NS,
              boot_id: str | None = None, pids: dict[int, str] | None = None) -> Path:
    proc = tmp_path / "proc"
    (proc / "self" / "ns").mkdir(parents=True)
    lines = "Name:\tsh\n" + (f"NSpid:\t{nspid}\n" if nspid is not None else "")
    (proc / "self" / "status").write_text(lines, encoding="utf-8")
    (proc / "self" / "ns" / "pid").symlink_to(ns)
    if boot_id is not None:
        (proc / "sys" / "kernel" / "random").mkdir(parents=True)
        (proc / "sys" / "kernel" / "random" / "boot_id").write_text(boot_id + "\n",
                                                                     encoding="utf-8")
    for pid, pid_ns in (pids or {}).items():
        (proc / str(pid) / "ns").mkdir(parents=True)
        (proc / str(pid) / "ns" / "pid").symlink_to(pid_ns)
    return proc


def record(home: Path, value: str, boot: str | None = None) -> Path:
    path = home / ".config" / "factory" / "host-pidns"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    if boot is not None:
        path.with_name("host-pidns.boot").write_text(boot + "\n", encoding="utf-8")
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
def test_service_refreshes_a_stale_record_after_a_reboot(tmp_path: Path,
                                                         command: list[str]) -> None:
    home = tmp_path / "home"
    (home / ".local" / "state" / "factory").mkdir(parents=True)
    path = record(home, OLD_NS, OLD_BOOT)
    proc = fake_proc(tmp_path, "4484", HOST_NS, NEW_BOOT)

    result = _run(command, home, proc)

    assert result.returncode == 0, result.stderr
    assert path.read_text(encoding="utf-8") == HOST_NS + "\n"
    assert path.with_name("host-pidns.boot").read_text(encoding="utf-8") == NEW_BOOT + "\n"
    assert OLD_NS in result.stderr and HOST_NS in result.stderr
    assert "refusing" not in result.stderr


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
    path = record(home, OLD_NS, OLD_BOOT)
    boot = path.with_name("host-pidns.boot")
    before = (path.read_bytes(), boot.read_bytes())
    proc = fake_proc(tmp_path, "4484", HOST_NS, NEW_BOOT, {4242: OLD_NS})

    result = _run(_service(script, *args), home, proc)

    assert result.returncode == 4, result.stderr
    assert f"refusing to {args[-1]} from a non-host PID namespace" in result.stderr
    assert (path.read_bytes(), boot.read_bytes()) == before


@pytest.mark.parametrize(
    ("nspid", "recorded_boot", "extra"),
    [
        ("4484", OLD_BOOT, {"ORCH_JOB_PROOF": "x"}),
        ("4484", OLD_BOOT, {"CODEX_SANDBOX": "seatbelt"}),
        ("1728748\t61", OLD_BOOT, {}),
        ("4484", NEW_BOOT, {}),
        ("4484", None, {}),
    ],
    ids=["job", "codex-sandbox", "two-field-nspid", "same-boot", "legacy-record"],
)
def test_refresh_never_runs_outside_the_host(
    tmp_path: Path, nspid: str, recorded_boot: str | None, extra: dict[str, str],
) -> None:
    home = tmp_path / "home"
    (home / ".local" / "state" / "factory").mkdir(parents=True)
    path = record(home, OLD_NS, recorded_boot)
    boot = path.with_name("host-pidns.boot")
    before = boot.read_bytes() if recorded_boot else None
    proc = fake_proc(tmp_path, nspid, HOST_NS, NEW_BOOT)

    result = _run(_service("poller-service.sh", "ensure"), home, proc, **extra)

    assert result.returncode == 4, result.stderr
    assert "refusing to ensure from a non-host PID namespace" in result.stderr
    assert path.read_text(encoding="utf-8") == OLD_NS + "\n"
    assert (boot.read_bytes() if boot.exists() else None) == before


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
