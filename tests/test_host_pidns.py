"""#308: the host PID namespace check and the service scripts' refusal."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from tests.procs import alive

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "deploy" / "host-pidns.sh"
HOST_NS = "pid:[4026531836]"
SANDBOX_NS = "pid:[4026532782]"
NEW_HOST_NS = "pid:[4026532250]"
RECORD = Path(".config/factory/host-pidns")
WSL_BOOT = ROOT / "deploy" / "wsl-boot.sh"
REFUSED_AS_RETIRED = 79
BOOT_LINE = "command = umask 022; readlink /proc/self/ns/pid >/run/factory-host-pidns"


def fake_proc(tmp_path: Path, nspid: str | None, ns: str = HOST_NS,
              pids: dict[int, dict[str, str | None]] | None = None, **self_files: str) -> Path:
    proc = tmp_path / "proc"
    (proc / "self" / "ns").mkdir(parents=True)
    lines = "Name:\tsh\n" + (f"NSpid:\t{nspid}\n" if nspid is not None else "")
    (proc / "self" / "status").write_text(lines, encoding="utf-8")
    (proc / "self" / "ns" / "pid").symlink_to(ns)
    for name, text in self_files.items():
        (proc / "self" / name).write_text(text, encoding="utf-8")
    for pid, entries in (pids or {}).items():
        for name, value in entries.items():
            path = proc / str(pid) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if value is None:
                path.touch(mode=0)
            elif name in ("ns/pid", "exe"):
                path.symlink_to(value)
            else:
                path.write_text(value, encoding="utf-8")
    return proc


def record(home: Path, value: str) -> None:
    path = home / RECORD
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")


def attestation(tmp_path: Path, text: str | None, mode: int = 0o644,
                uid: int | None = None) -> dict[str, str]:
    path = tmp_path / "factory-host-pidns"
    if text is not None:
        path.write_bytes(text.encode())
        path.chmod(mode)
    return {"ORCH_HOST_PIDNS_ATTESTATION": str(path),
            "ORCH_HOST_PIDNS_ATTESTATION_UID": str(os.getuid() if uid is None else uid)}


def _env(home: Path, proc: Path | None, **extra: str) -> dict[str, str]:
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", **extra}
    if proc is not None:
        env["ORCH_PROC_DIR"] = str(proc)
    return env


def helper(home: Path, proc: Path | None, function: str = "factory_in_host_pidns",
           **extra: str) -> subprocess.CompletedProcess:
    shell = shutil.which("dash") or "sh"
    return subprocess.run(
        [shell, "-c", f'. "$1" && {function}', "sh", str(HELPER)],
        env=_env(home, proc, **extra), text=True, capture_output=True, timeout=10, check=False,
    )


def service(home: Path, proc: Path, script: str, command: str,
            **extra: str) -> subprocess.CompletedProcess:
    args = ["claude-bot", command] if script == "waiter-service.sh" else [command]
    return subprocess.run(
        ["bash", str(ROOT / "deploy" / script), *args], env=_env(home, proc, **extra),
        text=True, capture_output=True, timeout=30, check=False,
    )


GATED = [(s, c) for s in ["poller-service.sh", "waiter-service.sh", "linear-webhook-service.sh",
                          "self-heal.sh"] for c in ["start", "ensure"]]
ENTRIES = ["refresh", *GATED]
ENTRY_IDS = ["refresh", *[f"{s}-{c}" for s, c in GATED]]


def attempt(entry: str | tuple[str, str], home: Path, proc: Path | None,
            **extra: str) -> subprocess.CompletedProcess:
    if entry == "refresh":
        return helper(home, proc, "factory_refresh_host_pidns", **extra)
    return service(home, proc, *entry, **extra)


def assert_refused(entry: str | tuple[str, str], result: subprocess.CompletedProcess,
                   home: Path) -> None:
    assert (home / RECORD).read_text(encoding="utf-8") == HOST_NS + "\n"
    if entry == "refresh":
        assert result.returncode != 0, result.stderr
    else:
        assert result.returncode == 4, result.stderr
        assert f"refusing to {entry[1]} from a non-host PID namespace" in result.stderr


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

    result = helper(tmp_path, proc)

    assert (result.returncode == 0) is host, result.stderr
    assert ("not the host PID namespace" in result.stderr) is not host


def test_no_proc_counts_as_host(tmp_path: Path) -> None:
    record(tmp_path, SANDBOX_NS)
    assert helper(tmp_path, tmp_path / "no-proc").returncode == 0


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


def _together(*scenarios: tuple[dict, dict]) -> tuple[dict, dict]:
    pids: dict[int, dict] = {}
    files: dict[str, str] = {}
    for scenario_pids, scenario_files in scenarios:
        for pid, entries in scenario_pids.items():
            pids[pid] = {**pids.get(pid, {}), **entries}
        files |= scenario_files
    return pids, files


IN_SANDBOX = {"ns/pid": SANDBOX_NS}
INITIAL_UID_MAP = "         0          0 4294967295\n"
INIT_ROOTFS = "1 0 0:2 /init /init ro,relatime - rootfs rootfs ro\n"
NAMED_FORGERIES = {
    "ac6-privileged-sbin-init-and-systemd": (
        {1: {**IN_SANDBOX, "status": "Name:\tsystemd\nPPid:\t0\n", "cmdline": "/sbin/init\0",
             "uid_map": INITIAL_UID_MAP}},
        {"uid_map": INITIAL_UID_MAP},
    ),
    "ac7-init-pid1-and-child": (
        {1: {**IN_SANDBOX, "status": "Name:\tinit\nPPid:\t0\n", "cmdline": "/init\0"},
         2: {**IN_SANDBOX, "status": "Name:\tinit\nPPid:\t1\n", "cmdline": "/init\0"}},
        {},
    ),
    "ac8-inherited-init-rootfs-mount": (
        {1: {**IN_SANDBOX, "mountinfo": INIT_ROOTFS}},
        {"mountinfo": INIT_ROOTFS},
    ),
    "ac9-renamed-non-dumpable-init": (
        {1: {**IN_SANDBOX, "status": "Name:\tinit(Ubuntu)\n", "cmdline": "/init\0", "exe": None},
         7: {**IN_SANDBOX, "status": "Name:\tinit(Ubuntu)\n", "exe": "/init (deleted)"}},
        {},
    ),
}
FORGERIES = {**NAMED_FORGERIES, "all-together": _together(*NAMED_FORGERIES.values())}


@pytest.mark.parametrize("ending", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_refresh_rewrites_a_stale_record_the_boot_attestation_agrees_with(
    tmp_path: Path, ending: str,
) -> None:
    proc = fake_proc(tmp_path, "4484", NEW_HOST_NS)
    record(tmp_path, HOST_NS)

    result = attempt("refresh", tmp_path, proc, **attestation(tmp_path, NEW_HOST_NS + ending))

    assert result.returncode == 0, result.stderr
    assert (tmp_path / RECORD).read_text(encoding="utf-8") == NEW_HOST_NS + "\n"


@pytest.mark.parametrize(("script", "command"), GATED, ids=ENTRY_IDS[1:])
def test_start_and_ensure_refresh_a_stale_record_after_a_restart(
    tmp_path: Path, script: str, command: str,
) -> None:
    proc = fake_proc(tmp_path, "4484", NEW_HOST_NS)
    record(tmp_path, HOST_NS)
    (tmp_path / RECORD).with_name("orch-retired").write_text("test\n", encoding="utf-8")

    result = service(tmp_path, proc, script, command, **attestation(tmp_path, NEW_HOST_NS + "\n"))

    assert "from a non-host PID namespace" not in result.stderr
    assert result.returncode == REFUSED_AS_RETIRED, result.stderr
    assert (tmp_path / RECORD).read_text(encoding="utf-8") == NEW_HOST_NS + "\n"


@pytest.mark.parametrize("recorded", [None, NEW_HOST_NS], ids=["no-record", "current-record"])
def test_refresh_needs_no_attestation_when_there_is_nothing_to_refresh(
    tmp_path: Path, recorded: str | None,
) -> None:
    proc = fake_proc(tmp_path, "4484", NEW_HOST_NS)
    if recorded:
        record(tmp_path, recorded)

    result = attempt("refresh", tmp_path, proc, **attestation(tmp_path, None))

    assert result.returncode == 0, result.stderr
    if recorded:
        assert (tmp_path / RECORD).read_text(encoding="utf-8") == recorded + "\n"
    else:
        assert not (tmp_path / RECORD).exists()


@pytest.mark.parametrize("entry", ENTRIES, ids=ENTRY_IDS)
def test_a_live_process_in_the_recorded_namespace_keeps_the_record(
    tmp_path: Path, entry: str | tuple[str, str],
) -> None:
    proc = fake_proc(tmp_path, "4484", NEW_HOST_NS, pids={4242: {"ns/pid": HOST_NS}})
    record(tmp_path, HOST_NS)

    result = attempt(entry, tmp_path, proc, **attestation(tmp_path, NEW_HOST_NS + "\n"))

    assert_refused(entry, result, tmp_path)
    assert result.returncode == 4, result.stderr


@pytest.mark.parametrize("entry", ENTRIES, ids=ENTRY_IDS)
@pytest.mark.parametrize(
    ("nspid", "extra"),
    [("4484", {"ORCH_JOB_PROOF": "x"}), ("4484", {"CODEX_SANDBOX": "seatbelt"}),
     ("1728748\t61", {})],
    ids=["job", "codex-sandbox", "two-field-nspid"],
)
def test_a_job_or_nested_namespace_never_refreshes(
    tmp_path: Path, entry: str | tuple[str, str], nspid: str, extra: dict[str, str],
) -> None:
    proc = fake_proc(tmp_path, nspid, NEW_HOST_NS)
    record(tmp_path, HOST_NS)

    result = attempt(entry, tmp_path, proc, **attestation(tmp_path, NEW_HOST_NS + "\n"), **extra)

    assert_refused(entry, result, tmp_path)


@pytest.mark.parametrize(
    ("text", "mode", "uid"),
    [
        (None, 0o644, None),
        (NEW_HOST_NS + "\n", 0o644, os.getuid() + 1),
        (NEW_HOST_NS + "\n", 0o664, None),
        (NEW_HOST_NS + "\n", 0o646, None),
        ("", 0o644, None),
        ("garbage\n", 0o644, None),
        (f"{NEW_HOST_NS}\n{NEW_HOST_NS}\n", 0o644, None),
        (f"{NEW_HOST_NS}x\n", 0o644, None),
        (SANDBOX_NS + "\n", 0o644, None),
    ],
    ids=["missing", "wrong-owner", "group-writable", "other-writable", "empty", "garbage",
         "two-lines", "trailing-text", "other-namespace"],
)
def test_refresh_fails_closed_on_a_bad_attestation(
    tmp_path: Path, text: str | None, mode: int, uid: int | None,
) -> None:
    proc = fake_proc(tmp_path, "4484", NEW_HOST_NS)
    record(tmp_path, HOST_NS)

    result = attempt("refresh", tmp_path, proc, **attestation(tmp_path, text, mode, uid))

    assert result.returncode == 1, result.stderr
    assert (tmp_path / RECORD).read_text(encoding="utf-8") == HOST_NS + "\n"


@pytest.mark.parametrize("attested", [None, HOST_NS + "\n"],
                         ids=["no-attestation", "attestation-names-the-host"])
@pytest.mark.parametrize("forged", FORGERIES)
def test_forged_init_attributes_never_refresh(
    tmp_path: Path, forged: str, attested: str | None,
) -> None:
    pids, files = FORGERIES[forged]
    proc = fake_proc(tmp_path, "4484", SANDBOX_NS, pids=pids, **files)
    record(tmp_path, HOST_NS)

    result = attempt("refresh", tmp_path, proc, **attestation(tmp_path, attested))

    assert result.returncode != 0, result.stderr
    assert (tmp_path / RECORD).read_text(encoding="utf-8") == HOST_NS + "\n"


@pytest.mark.parametrize("entry", ENTRIES, ids=ENTRY_IDS)
@pytest.mark.parametrize("forged", NAMED_FORGERIES)
def test_a_sandbox_with_a_forged_init_is_refused(
    tmp_path: Path, forged: str, entry: str | tuple[str, str],
) -> None:
    pids, files = NAMED_FORGERIES[forged]
    proc = fake_proc(tmp_path, "4484", SANDBOX_NS, pids=pids, **files)
    record(tmp_path, HOST_NS)

    result = attempt(entry, tmp_path, proc, **attestation(tmp_path, HOST_NS + "\n"))

    assert_refused(entry, result, tmp_path)


RENAMED_INIT = """
import ctypes, time
libc = ctypes.CDLL(None, use_errno=True)
assert libc.prctl(15, b"init(Ubuntu)", 0, 0, 0) == 0
assert libc.prctl(4, 0, 0, 0, 0) == 0
print("ready", flush=True)
time.sleep(60)
"""


@pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="needs Linux /proc")
def test_a_real_renamed_non_dumpable_init_does_not_refresh(tmp_path: Path) -> None:
    child = subprocess.Popen([sys.executable, "-c", RENAMED_INIT], stdout=subprocess.PIPE,
                             text=True)
    try:
        assert child.stdout.readline() == "ready\n"
        record(tmp_path, "pid:[1]")

        result = attempt("refresh", tmp_path, None, **attestation(tmp_path, "pid:[2]\n"))

        assert result.returncode != 0, result.stderr
        assert (tmp_path / RECORD).read_text(encoding="utf-8") == "pid:[1]\n"
    finally:
        child.kill()
        child.wait(timeout=10)


def test_the_host_check_reads_no_forgeable_process_attribute() -> None:
    forgeable = re.findall(r"/1/|\bcomm\b|cmdline|\bexe\b|mountinfo|Name:", HELPER.read_text())
    assert forgeable == []


def wsl_boot(conf: Path) -> subprocess.CompletedProcess:
    return subprocess.run([str(WSL_BOOT), str(conf)], text=True, capture_output=True,
                          timeout=10, check=False)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (None, f"[boot]\n{BOOT_LINE}\n"),
        ("", f"[boot]\n{BOOT_LINE}\n"),
        ("[network]\ngenerateResolvConf = false\n",
         f"[network]\ngenerateResolvConf = false\n\n[boot]\n{BOOT_LINE}\n"),
        ("[boot]\nsystemd=true\n[user]\ndefault=bot\n",
         f"[boot]\n{BOOT_LINE}\nsystemd=true\n[user]\ndefault=bot\n"),
    ],
    ids=["missing", "empty", "other-sections", "boot-without-command"],
)
def test_wsl_boot_adds_the_attestation_command_once(
    tmp_path: Path, before: str | None, after: str,
) -> None:
    conf = tmp_path / "wsl.conf"
    if before is not None:
        conf.write_text(before, encoding="utf-8")
        conf.chmod(0o640)

    first = wsl_boot(conf)
    second = wsl_boot(conf)

    assert first.returncode == 0, first.stderr
    assert "wsl --shutdown" in first.stdout
    assert second.returncode == 0, second.stderr
    assert "already set" in second.stdout
    assert conf.read_text(encoding="utf-8") == after
    assert conf.stat().st_mode & 0o777 == (0o644 if before is None else 0o640)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["wsl.conf"]


def test_wsl_boot_leaves_another_boot_command_for_a_person_to_merge(tmp_path: Path) -> None:
    conf = tmp_path / "wsl.conf"
    conf.write_text("[boot]\nsystemd=true\ncommand = mount --make-rshared /\n", encoding="utf-8")
    before = conf.read_bytes()

    result = wsl_boot(conf)

    assert result.returncode == 1
    assert "command = mount --make-rshared /" in result.stderr
    assert BOOT_LINE in result.stderr
    assert "merge by hand: join both with ;" in result.stderr
    assert conf.read_bytes() == before
