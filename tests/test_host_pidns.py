"""#308: the host PID namespace check and the service scripts' refusal."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "deploy" / "host-pidns.sh"
HOST_NS = "pid:[4026531836]"
SANDBOX_NS = "pid:[4026532782]"


def fake_proc(tmp_path: Path, nspid: str | None, ns: str = HOST_NS) -> Path:
    proc = tmp_path / "proc"
    (proc / "self" / "ns").mkdir(parents=True)
    lines = "Name:\tsh\n" + (f"NSpid:\t{nspid}\n" if nspid is not None else "")
    (proc / "self" / "status").write_text(lines, encoding="utf-8")
    (proc / "self" / "ns" / "pid").symlink_to(ns)
    return proc


def record(home: Path, value: str) -> None:
    path = home / ".config" / "factory" / "host-pidns"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")


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


def test_waiter_status_still_answers_outside_the_host_pid_namespace(tmp_path: Path) -> None:
    proc = fake_proc(tmp_path, "1728748\t61")
    result = subprocess.run(
        ["bash", str(ROOT / "deploy" / "waiter-service.sh"), "claude-bot", "status"],
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "ORCH_PROC_DIR": str(proc)},
        text=True, capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 3, result.stderr
    assert "refusing" not in result.stderr
