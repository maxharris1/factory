"""deploy/self-heal.sh runs `orch health --json | orch heal` on a timer (#107).

Everything runs in a temporary HOME: `orch` is a fake whose `health --json`
prints a fixed report and whose `heal` records what it got and calls a fake
service script, and systemctl and crontab are fakes that record argv.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.child_reaper import TAG
from tests.fake_path import link_path_without
from tests.procs import alive

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "self-heal.sh"
STATE = Path(".local/state/factory")
REPORT = {"known": True, "components": [{"component": "waiter", "bot": "codex-bot",
                                         "device": "box", "state": "down"}]}
SECRET = "Bearer secret-webhook-value-123"

FAKE_ORCH = f"""#!{sys.executable}
import json, os, subprocess, sys
from pathlib import Path

home = Path(os.environ["HOME"])
with (home / "orch-order").open("a") as handle:
    handle.write(" ".join(sys.argv[1:3]) + "\\n")
if sys.argv[1:3] == ["heal", "deploy"]:
    with (home / "deploy-calls").open("a") as handle:
        handle.write(json.dumps({{"argv": sys.argv[1:], "db": os.environ.get("ORCH_DB")}}) + "\\n")
    if os.environ.get("FAKE_DEPLOY_MOVES"):
        (home / "head").write_text("b" * 40)
    print("deploy output")
    raise SystemExit(int(os.environ.get("FAKE_DEPLOY_EXIT", "0")))
if sys.argv[1:3] == ["settings", "apply"]:
    with (home / "settings-calls").open("a") as handle:
        handle.write(json.dumps({{"argv": sys.argv[1:], "db": os.environ.get("ORCH_DB")}}) + "\\n")
    raise SystemExit(int(os.environ.get("FAKE_SETTINGS_EXIT", "0")))
if sys.argv[1:3] in (["heal", "devices"], ["dashboard", "push"]):
    with (home / "detached-calls").open("a") as handle:
        handle.write(json.dumps({{"argv": sys.argv[1:], "sid": os.getsid(0) == os.getpid(),
                                  "db": os.environ.get("ORCH_DB")}}) + "\\n")
    raise SystemExit(0)
if sys.argv[1:] == ["health", "--json"]:
    print({json.dumps(json.dumps(REPORT))})
    raise SystemExit(int(os.environ.get("FAKE_HEALTH_EXIT", "3")))
if sys.argv[1] == "heal":
    record = {{"argv": sys.argv[1:], "stdin": sys.stdin.read(),
               "auth": os.environ.get("FACTORY_WEBHOOK_AUTH"), "db": os.environ.get("ORCH_DB")}}
    with (home / "heal-calls").open("a") as handle:
        handle.write(json.dumps(record) + "\\n")
    subprocess.run([str(home / "deploy" / "waiter-service.sh"), "codex-bot", "restart"], check=True)
    raise SystemExit(int(os.environ.get("FAKE_HEAL_EXIT", "0")))
raise SystemExit(f"unexpected {{sys.argv[1:]}}")
"""

FAKE_GIT = """#!/bin/sh
[ -f "$HOME/head" ] || exit 1
cat "$HOME/head"
"""

FAKE_SERVICE = """#!/bin/sh
echo "$(basename "$0") $*" >>"$HOME/service-calls"
"""

FAKE_JOB_RUN = f"""#!{sys.executable}
import time
time.sleep(60)
"""

FAKE_SYSTEMCTL = """#!/bin/sh
echo "$*" >>"$HOME/systemctl-calls"
exit 0
"""

FAKE_CRONTAB = f"""#!{sys.executable}
import os, sys
from pathlib import Path

table = Path(os.environ["HOME"]) / "crontab.txt"
if os.environ.get("FAKE_CRONTAB_BROKEN"):
    print("crontab: permission denied", file=sys.stderr)
    raise SystemExit(1)
if sys.argv[1:] == ["-l"]:
    if not table.exists():
        print("no crontab for test", file=sys.stderr)
        raise SystemExit(1)
    sys.stdout.write(table.read_text())
elif sys.argv[1:] == ["-"]:
    table.write_text(sys.stdin.read())
else:
    raise SystemExit(f"unexpected crontab arguments {{sys.argv[1:]}}")
"""


def _executable(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def home(tmp_path: Path):
    home = tmp_path / "home"
    _executable(home / ".local" / "bin" / "orch", FAKE_ORCH)
    _executable(home / ".local" / "bin" / "git", FAKE_GIT)
    _executable(home / "deploy" / "waiter-service.sh", FAKE_SERVICE)
    (home / ".config" / "factory").mkdir(parents=True)
    yield home
    _run(home, "stop")


def _run(home: Path, *args: str, **extra: str) -> subprocess.CompletedProcess:
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        TAG: os.environ.get(TAG, ""),
        "ORCH_HEAL_START_WAIT": "10",
        "ORCH_HEAL_STOP_WAIT": "10",
        "ORCH_SYSTEMCTL": str(home / "no-such-systemctl"),
        "ORCH_CRONTAB": str(home / "no-such-crontab"),
        **extra,
    }
    return subprocess.run(["bash", str(SCRIPT), *args], env=env, text=True,
                          capture_output=True, timeout=60, check=False)


def _calls(home: Path) -> list[dict]:
    path = home / "heal-calls"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _service_log(home: Path) -> str:
    path = home / STATE / "self-heal-service.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_once_pipes_the_health_report_into_heal_and_restarts_through_the_service(
    home: Path,
) -> None:
    result = _run(home, "once")

    assert result.returncode == 0, result.stderr
    (call,) = _calls(home)
    assert call["argv"] == ["heal", "--health-json", "-", "--state", str(home / STATE)]
    assert json.loads(call["stdin"]) == REPORT
    assert call["db"] == str(home / STATE / "orch.db")
    assert (home / "service-calls").read_text() == "waiter-service.sh codex-bot restart\n"


def test_once_applies_settings_after_the_deploy_and_before_health(home: Path) -> None:
    result = _run(home, "once")

    assert result.returncode == 0, result.stderr
    (call,) = [json.loads(line) for line in (home / "settings-calls").read_text().splitlines()]
    assert call["argv"] == ["settings", "apply", "--state", str(home / STATE)]
    assert call["db"] == str(home / STATE / "orch.db")
    assert _wait_for(lambda: "heal devices" in (home / "orch-order").read_text())
    order = (home / "orch-order").read_text().splitlines()
    assert order.index("heal deploy") < order.index("settings apply") < order.index("health --json")


def test_a_failed_settings_apply_never_fails_the_pass(home: Path) -> None:
    result = _run(home, "once", FAKE_SETTINGS_EXIT="1")

    assert result.returncode == 0, result.stderr
    assert len(_calls(home)) == 1, "healing ran anyway"
    assert "orch settings apply exited 1; healing anyway" in _service_log(home)


def test_once_never_signals_a_running_job(home: Path) -> None:
    job = _executable(home / "job-bin" / "orch", FAKE_JOB_RUN)
    proc = subprocess.Popen([str(job), "job", "run", "claude-bot", "job-1", "--worktree",
                             "--", "sleep", "60"])
    try:
        assert _run(home, "once").returncode == 0
        time.sleep(0.2)
        assert alive(proc.pid)
    finally:
        proc.kill()
        proc.wait()


def test_webhook_env_reaches_heal_but_is_never_printed(home: Path) -> None:
    env_file = home / ".config" / "factory" / "webhook.env"
    env_file.write_text(f'FACTORY_WEBHOOK_AUTH="{SECRET}"\n', encoding="utf-8")
    env_file.chmod(0o600)

    result = _run(home, "once")

    assert result.returncode == 0, result.stderr
    assert _calls(home)[0]["auth"] == SECRET
    for text in (result.stdout, result.stderr, _service_log(home)):
        assert "secret-webhook-value" not in text


@pytest.mark.parametrize("health_exit, expected", [(0, 0), (1, 0), (3, 0), (2, 1)])
def test_only_an_unexpected_health_exit_fails_the_pass(
    home: Path, health_exit: int, expected: int
) -> None:
    result = _run(home, "once", FAKE_HEALTH_EXIT=str(health_exit))
    assert result.returncode == expected
    if expected:
        assert "orch health exited 2" in _service_log(home)


def test_once_returns_heals_status(home: Path) -> None:
    assert _run(home, "once", FAKE_HEAL_EXIT="1").returncode == 1


def test_a_pass_already_running_makes_once_exit_quietly(home: Path) -> None:
    lock = home / STATE / "self-heal.lock"
    lock.parent.mkdir(parents=True)
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        result = _run(home, "once")
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert _calls(home) == []


def test_once_skips_the_pass_while_an_orch_move_export_drains(home: Path) -> None:
    drain = home / STATE / "waiter-claude-bot.drain"
    drain.parent.mkdir(parents=True)
    drain.write_text("orch-move\n", encoding="utf-8")

    result = _run(home, "once")

    assert result.returncode == 0, result.stderr
    assert not (home / "orch-order").exists()
    assert drain.read_text(encoding="utf-8") == "orch-move\n"
    assert f"skipping the pass: orch-move export is draining ({drain})" in _service_log(home)


def test_once_heals_while_a_drain_file_names_a_commit(home: Path) -> None:
    drain = home / STATE / "waiter-claude-bot.drain"
    drain.parent.mkdir(parents=True)
    drain.write_text("0123abc\n", encoding="utf-8")

    result = _run(home, "once")

    assert result.returncode == 0, result.stderr
    assert len(_calls(home)) == 1
    assert "skipping the pass" not in _service_log(home)


def _pid(home: Path) -> int:
    return int((home / STATE / "self-heal.pid").read_text())


def test_start_status_stop_and_a_silent_ensure(home: Path) -> None:
    assert _run(home, "status").returncode == 3
    started = _run(home, "start", ORCH_HEAL_SECONDS="60")
    assert started.returncode == 0, started.stderr
    pid = _pid(home)
    assert alive(pid)
    assert _run(home, "status").stdout == f"running (pid {pid})\n"

    ensured = _run(home, "ensure")
    assert (ensured.returncode, ensured.stdout, ensured.stderr) == (0, "", "")
    deadline = time.monotonic() + 10
    while not _calls(home) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(_calls(home)) == 1

    assert _run(home, "stop").returncode == 0
    assert not alive(pid)
    assert not (home / STATE / "self-heal.pid").exists()
    held = _run(home, "ensure")
    assert (held.returncode, held.stdout, held.stderr) == (0, "", "")
    assert _run(home, "status").returncode == 3


def test_the_loop_runs_a_pass_every_interval(home: Path) -> None:
    assert _run(home, "start", ORCH_HEAL_SECONDS="0.2").returncode == 0
    deadline = time.monotonic() + 10
    while len(_calls(home)) < 3 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(_calls(home)) >= 3


def test_install_autostart_enables_the_systemd_timer_where_one_runs(home: Path) -> None:
    systemctl = _executable(home / "fake-bin" / "systemctl", FAKE_SYSTEMCTL)
    result = _run(home, "install-autostart", ORCH_SYSTEMCTL=str(systemctl))

    assert result.returncode == 0, result.stderr
    units = home / ".config" / "systemd" / "user"
    service = (units / "factory-self-heal.service").read_text(encoding="utf-8")
    assert f"ExecStart={ROOT}/deploy/self-heal.sh once\n" in service
    assert "KillMode=process\n" in service
    timer = (units / "factory-self-heal.timer").read_text(encoding="utf-8")
    assert "OnUnitActiveSec=2min\n" in timer and "OnBootSec=1min\n" in timer
    assert (home / "systemctl-calls").read_text().splitlines() == [
        "--user show-environment",
        "--user daemon-reload",
        "--user enable --now factory-self-heal.timer",
    ]
    assert not (home / ".config" / "factory" / "self-heal-autostart").exists()

    removed = _run(home, "remove-autostart", ORCH_SYSTEMCTL=str(systemctl))
    assert removed.returncode == 0, removed.stderr
    assert "--user disable --now factory-self-heal.timer" in (home / "systemctl-calls").read_text()


def test_install_autostart_without_systemd_uses_the_profile_flag_and_cron(home: Path) -> None:
    crontab = _executable(home / "fake-bin" / "crontab", FAKE_CRONTAB)
    (home / "crontab.txt").write_text("0 1 * * * other-job\n", encoding="utf-8")

    for _ in range(2):
        result = _run(home, "install-autostart", ORCH_CRONTAB=str(crontab))
        assert result.returncode == 0, result.stderr

    assert (home / ".config" / "factory" / "self-heal-autostart").is_file()
    job = f"{SCRIPT} ensure >>{home / STATE}/self-heal-service.log 2>&1 # factory-self-heal"
    assert (home / "crontab.txt").read_text(encoding="utf-8").splitlines() == [
        "0 1 * * * other-job",
        f"@reboot {job}",
        f"*/5 * * * * {job}",
    ]

    assert _run(home, "remove-autostart", ORCH_CRONTAB=str(crontab)).returncode == 0
    assert not (home / ".config" / "factory" / "self-heal-autostart").exists()
    assert (home / "crontab.txt").read_text(encoding="utf-8") == "0 1 * * * other-job\n"


def test_an_unreadable_crontab_changes_nothing(home: Path) -> None:
    crontab = _executable(home / "fake-bin" / "crontab", FAKE_CRONTAB)
    result = _run(home, "install-autostart", ORCH_CRONTAB=str(crontab), FAKE_CRONTAB_BROKEN="1")
    assert result.returncode == 1
    assert "cannot read the crontab" in result.stderr
    assert not (home / ".config" / "factory" / "self-heal-autostart").exists()


def test_an_unknown_command_prints_usage(home: Path) -> None:
    result = _run(home, "bogus")
    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_the_repo_units_run_once_every_two_minutes() -> None:
    service = (ROOT / "deploy" / "factory-self-heal.service").read_text(encoding="utf-8")
    timer = (ROOT / "deploy" / "factory-self-heal.timer").read_text(encoding="utf-8")
    assert "Type=oneshot" in service and "KillMode=process" in service
    assert "ExecStart=%h/factory/deploy/self-heal.sh once" in service
    assert "SuccessExitStatus=79" in service
    assert "Unit=factory-self-heal.service" in timer
    assert "WantedBy=timers.target" in timer


def test_the_self_heal_unit_runs_at_the_oom_floor_and_leaves_what_it_starts_running() -> None:
    lines = (ROOT / "deploy" / "factory-self-heal.service").read_text(encoding="utf-8").splitlines()
    assert "OOMScoreAdjust=0" in lines
    assert "KillMode=process" in lines


def _detached(home: Path, count: int) -> list[dict]:
    path = home / "detached-calls"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if path.exists():
            calls = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            if len(calls) >= count:
                return calls
        time.sleep(0.05)
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_once_starts_the_device_deploy_detached_after_the_heal_pass(home: Path) -> None:
    (home / "head").write_text("a" * 40 + "\n")

    result = _run(home, "once")

    assert result.returncode == 0, result.stderr
    (call,) = _detached(home, 1)
    assert call["argv"] == ["heal", "devices", "--state", str(home / STATE)]
    assert call["sid"] and call["db"] == str(home / STATE / "orch.db")
    order = (home / "orch-order").read_text().splitlines()
    assert order.index("heal --health-json") < order.index("heal devices")
    time.sleep(0.3)
    assert len(_detached(home, 1)) == 1


def test_once_pushes_the_dashboard_only_after_the_box_deploy_moved_head(home: Path) -> None:
    (home / "head").write_text("a" * 40 + "\n")

    assert _run(home, "once", FAKE_DEPLOY_MOVES="1").returncode == 0

    argvs = sorted(call["argv"][:2] for call in _detached(home, 2))
    assert argvs == [["dashboard", "push"], ["heal", "devices"]]
    assert ["dashboard", "push", "--once"] in [call["argv"] for call in _detached(home, 2)]


def test_a_failed_health_pass_still_starts_the_device_deploy(home: Path) -> None:
    result = _run(home, "once", FAKE_HEALTH_EXIT="2")

    assert result.returncode == 1
    assert [call["argv"][:2] for call in _detached(home, 1)] == [["heal", "devices"]]


def test_once_starts_the_device_deploy_where_setsid_is_missing(home: Path, tmp_path: Path) -> None:
    tools = link_path_without(tmp_path / "no-setsid-bin", ["/usr/bin", "/bin"], {"setsid"})

    result = _run(home, "once", PATH=str(tools))

    assert result.returncode == 0, result.stderr
    assert "unbound variable" not in result.stderr
    assert [call["argv"][:2] for call in _detached(home, 1)] == [["heal", "devices"]]


def _loops(home: Path) -> list[int]:
    state = str(home / STATE)
    found = []
    for cmdline in Path("/proc").glob("[1-9]*/cmdline"):
        try:
            argv = cmdline.read_bytes().split(b"\0")
        except OSError:
            continue
        if b"_loop" in argv and state.encode() in argv and alive(int(cmdline.parent.name)):
            found.append(int(cmdline.parent.name))
    return sorted(found)


def _spawn_loop(home: Path, seconds: str, state: str = "", cwd: Path | None = None,
                ) -> subprocess.Popen:
    with (home / "loops.log").open("a") as out:
        return subprocess.Popen(["bash", str(SCRIPT), "_loop", state or str(home / STATE)],
                                env={"HOME": str(home), "PATH": "/usr/bin:/bin",
                                     "ORCH_HEAL_SECONDS": seconds},
                                cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=out)


def _loops_log(home: Path) -> str:
    return (home / "loops.log").read_text(encoding="utf-8")


def _wait_for(predicate, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.05)
    return predicate()


def test_two_loops_on_one_state_dir_leave_exactly_one_within_an_interval(home: Path) -> None:
    first = _spawn_loop(home, "1")
    assert _wait_for(lambda: len(_calls(home)) == 1)
    second = _spawn_loop(home, "1")
    assert _wait_for(lambda: len(_calls(home)) == 2)

    first.wait(timeout=1.5)

    assert _loops(home) == [second.pid]
    assert _pid(home) == second.pid
    assert f"loop {first.pid} no longer owns" in _loops_log(home)


def test_a_loop_that_lost_its_pidfile_exits_before_the_next_pass_and_says_so(
    home: Path,
) -> None:
    loop = _spawn_loop(home, "0.5")
    assert _wait_for(lambda: len(_calls(home)) == 1)
    (home / STATE / "self-heal.pid").unlink()

    loop.wait(timeout=5)

    assert len(_calls(home)) == 1
    pidfile = home / STATE / "self-heal.pid"
    assert f"loop {loop.pid} no longer owns {pidfile} (missing); exiting" in _loops_log(home)


@pytest.mark.parametrize("pidfile", ["missing", "stale"])
@pytest.mark.parametrize("command", ["start", "ensure"])
def test_start_and_ensure_refuse_a_second_loop_whatever_the_pidfile_says(
    home: Path, command: str, pidfile: str,
) -> None:
    assert _run(home, "start", ORCH_HEAL_SECONDS="60").returncode == 0
    pid = _pid(home)
    if pidfile == "missing":
        (home / STATE / "self-heal.pid").unlink()
    else:
        (home / STATE / "self-heal.pid").write_text("999999999\n")

    result = _run(home, command, ORCH_HEAL_SECONDS="60")

    assert result.returncode == 0, result.stderr
    assert f"self-heal loop already running (pid {pid})" in result.stdout
    assert _loops(home) == [pid]


def _spellings(home: Path, spelling: str) -> tuple[str, Path | None]:
    state = home / STATE
    if spelling == "trailing slash":
        return f"{state}/", None
    if spelling == "dot segments":
        return f"{state}/../factory/.", None
    if spelling == "symlink":
        link = home / "state-link"
        if not link.exists():
            link.symlink_to(state, target_is_directory=True)
        return str(link), None
    return str(STATE), home


@pytest.mark.parametrize("command", ["start", "ensure"])
@pytest.mark.parametrize("spelling", ["trailing slash", "dot segments", "symlink", "relative"])
@pytest.mark.parametrize("side", ["caller", "loop"])
def test_any_spelling_of_the_state_dir_finds_the_loop_on_it(
    home: Path, command: str, spelling: str, side: str,
) -> None:
    (home / STATE).mkdir(parents=True)
    other, cwd = _spellings(home, spelling)
    loop_state, caller_state = (str(home / STATE), other) if side == "caller" else (other, "")
    loop = _spawn_loop(home, "60", loop_state, cwd if side == "loop" else None)
    try:
        assert _wait_for(lambda: len(_calls(home)) == 1)
        (home / STATE / "self-heal.pid").unlink()
        extra = {"ORCH_STATE_DIR": caller_state} if caller_state else {}
        run_cwd = cwd if side == "caller" else None

        result = subprocess.run(["bash", str(SCRIPT), command], cwd=run_cwd, text=True,
                                capture_output=True, timeout=60, check=False,
                                env={"HOME": str(home), "PATH": "/usr/bin:/bin",
                                     "ORCH_HEAL_SECONDS": "60", **extra})

        assert result.returncode == 0, result.stderr
        assert f"self-heal loop already running (pid {loop.pid})" in result.stdout
        assert not (home / STATE / "self-heal.pid").exists()

        stopped = subprocess.run(["bash", str(SCRIPT), "stop"], cwd=run_cwd, text=True,
                                 capture_output=True, timeout=60, check=False,
                                 env={"HOME": str(home), "PATH": "/usr/bin:/bin", **extra})
        assert stopped.returncode == 0, stopped.stderr
        loop.wait(timeout=5)
    finally:
        loop.kill()
        loop.wait()


def test_a_loop_started_before_368_counts_by_its_environment(home: Path, tmp_path: Path) -> None:
    old = _executable(tmp_path / "old-checkout" / "self-heal.sh",
                      "while :; do sleep 0.1; done\n")
    legacy = subprocess.Popen(["bash", str(old), "_loop"], env={"HOME": str(home),
                                                                "PATH": "/usr/bin:/bin"})
    try:
        assert _wait_for(lambda: Path(f"/proc/{legacy.pid}/cmdline").read_bytes().endswith(
            b"_loop\0"))
        refused = _run(home, "start")
        assert f"self-heal loop already running (pid {legacy.pid})" in refused.stdout
        assert not (home / STATE / "self-heal.pid").exists()

        assert _run(home, "stop").returncode == 0
        legacy.wait(timeout=5)
    finally:
        legacy.kill()
        legacy.wait()


def test_stop_stops_every_loop_on_the_state_dir(home: Path) -> None:
    loops = []
    for count in (1, 2, 3):
        loops.append(_spawn_loop(home, "60"))
        assert _wait_for(lambda: len(_calls(home)) == count)
    assert len(_loops(home)) == 3

    assert _run(home, "stop").returncode == 0

    for loop in loops:
        loop.wait(timeout=5)
    assert _loops(home) == []


@pytest.mark.parametrize("command", ["start", "restart", "ensure"])
def test_a_job_environment_never_starts_a_loop_unless_forced(home: Path, command: str) -> None:
    refused = _run(home, command, ORCH_JOB_ID="job-1", ORCH_HEAL_SECONDS="60")

    assert refused.returncode == 0
    assert f"not running {command} from a job" in refused.stderr
    assert not (home / STATE).exists()
    assert _loops(home) == []

    forced = _run(home, command, ORCH_JOB_ID="job-1", ORCH_HEAL_FORCE="1", ORCH_HEAL_SECONDS="60")
    assert forced.returncode == 0, forced.stderr
    assert _loops(home) == [_pid(home)]


def _retire(home: Path) -> Path:
    retired = home / ".config" / "factory" / "orch-retired"
    retired.write_text("pc 2026-11-17T13:00:00Z\nsecond line\n", encoding="utf-8")
    return retired


@pytest.mark.parametrize("command", ["once", "kick", "start", "restart", "ensure"])
def test_a_retired_host_runs_no_pass_and_starts_no_loop(home: Path, command: str) -> None:
    retired = _retire(home)

    result = _run(home, command, ORCH_HEAL_SECONDS="0.2")

    assert result.returncode == 79, result.stderr
    assert f"{retired}: pc 2026-11-17T13:00:00Z" in result.stderr
    assert "second line" not in result.stderr
    time.sleep(0.5)
    assert not (home / "orch-order").exists()
    assert _loops(home) == []
    assert not (home / STATE / "self-heal.pid").exists()


def test_the_loop_exits_before_its_next_pass_once_the_host_is_retired(home: Path) -> None:
    assert _run(home, "start", ORCH_HEAL_SECONDS="0.3").returncode == 0
    pid = _pid(home)
    assert _wait_for(lambda: len(_calls(home)) >= 1)

    _retire(home)

    assert _wait_for(lambda: not alive(pid))
    passes = len(_calls(home))
    time.sleep(0.6)
    assert len(_calls(home)) == passes
    assert "this host is retired as the orch host" in _service_log(home)
    assert not (home / STATE / "self-heal.pid").exists()


def test_wait_idle_returns_when_idle_and_times_out_while_a_pass_holds_the_work_lock(
    home: Path,
) -> None:
    _retire(home)
    assert _run(home, "wait-idle").returncode == 0

    lock = home / STATE / "self-heal-work.lock"
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_SH)
        started = time.monotonic()
        busy = _run(home, "wait-idle", ORCH_HEAL_IDLE_WAIT="1")
        waited = time.monotonic() - started

    assert busy.returncode == 1
    assert "still holds" in busy.stderr
    assert 0.9 <= waited < 10
    assert _run(home, "wait-idle", ORCH_HEAL_IDLE_WAIT="1").returncode == 0


def test_wait_idle_runs_its_command_before_any_pass_can_take_the_work_lock(home: Path) -> None:
    lock = home / STATE / "self-heal-work.lock"
    probe = f"flock -n -s {lock} true"

    assert _run(home, "wait-idle", "sh", "-c", probe).returncode == 1
    assert _run(home, "wait-idle", "sh", "-c", "exit 7").returncode == 7
    assert _run(home, "wait-idle", "true").returncode == 0
    assert subprocess.run(["sh", "-c", probe], check=False).returncode == 0
