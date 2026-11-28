import fcntl
import hashlib
import json
import os
import resource
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from lib import exposure, job_run, process_tree, usage_readings
from lib.brief.skills import Router, resolve, section
from lib.harness_env import CPU_CAP, TEST_WORKERS, cpu_cap_env
from lib.job_state import JobStateConfig
from lib.queue import JobRequest, QueueError, SQLiteJobQueue
from tests.fake_docker import Docker
from tests.handoff_store import serve_job
from tests.pool_helpers import LOCAL_BOX_MACHINES, pool_claim

ROOT = Path(__file__).resolve().parents[1]
ORCH = ROOT / "bin" / "orch"


class Beats:
    def __init__(self, fail_with: list[BaseException] | None = None):
        self.times: list[float] = []
        self.fail_with = list(fail_with or [])
        self.lock = threading.Lock()

    def __call__(self) -> None:
        with self.lock:
            self.times.append(time.monotonic())
            if self.fail_with:
                raise self.fail_with.pop(0)

    def count(self) -> int:
        with self.lock:
            return len(self.times)


def test_interval_defaults_to_15_seconds_or_a_third_of_a_short_timeout() -> None:
    # #75: a 60-minute job used to beat every 20 minutes, so a 2-minute
    # stale threshold would flag every live job.
    assert job_run.heartbeat_interval(3600, None) == 15
    assert job_run.heartbeat_interval(9000, None) == 15
    assert job_run.heartbeat_interval(30, None) == 10
    assert job_run.heartbeat_interval(60, 5) == 5
    assert job_run.heartbeat_interval(3600, 25) == 25


# The default stale threshold (2 minutes) and the smallest orch.yml allows.
DEFAULT_STALE = JobStateConfig().stale_seconds
SMALLEST_STALE = 60.0


@pytest.mark.parametrize("timeout", [60, 3600, 9000, 86400])
@pytest.mark.parametrize("override", [None, 31, 119, 120, 600, 3600, 1e9])
def test_default_and_large_overrides_stay_under_the_stale_threshold(
    timeout: int, override: float | None
) -> None:
    # Factory request on PR #77: default 15 s; any override is capped below
    # the stale threshold and at a third of the timeout.
    assert DEFAULT_STALE == 120
    interval = job_run.heartbeat_interval(timeout, override)
    assert 0 < interval <= timeout / 3
    assert interval < DEFAULT_STALE
    # At least two beats inside even the smallest threshold.
    assert interval <= SMALLEST_STALE / 2


@pytest.mark.parametrize("timeout", [1, 2, 3])
def test_default_interval_is_never_above_a_third_of_a_short_timeout(timeout: int) -> None:
    # Regression (#52 F1.8): a 1s floor beat a 1s or 2s job slower than
    # timeout/3, so T6 could time it out between beats.
    assert job_run.heartbeat_interval(timeout, None) <= timeout / 3


@pytest.mark.parametrize("timeout", [0, -5])
def test_a_job_without_a_positive_timeout_has_no_default_interval(timeout: int) -> None:
    with pytest.raises(ValueError):
        job_run.heartbeat_interval(timeout, None)


@pytest.mark.parametrize("override", [0, -1, float("nan"), float("inf")])
def test_interval_override_must_be_a_number_above_0(override) -> None:
    with pytest.raises(ValueError):
        job_run.heartbeat_interval(60, override)


def test_an_override_above_a_third_of_the_timeout_is_capped_there() -> None:
    assert job_run.heartbeat_interval(60, 21) == 20
    assert job_run.heartbeat_interval(30, 600) == 10


def test_heartbeats_while_the_command_runs_and_stops_when_it_exits() -> None:
    beats = Beats()
    code = job_run.run(
        [sys.executable, "-c", "import time; time.sleep(0.6)"],
        beat=beats,
        interval=0.1,
        log=lambda _m: None,
    )
    assert code == 0
    during = beats.count()
    assert during >= 4  # the first beat, then about one per 0.1s
    time.sleep(0.4)
    assert beats.count() == during


def test_heartbeat_stops_for_good_once_the_job_is_no_longer_running() -> None:
    beats = Beats()
    messages: list[str] = []

    def beat() -> None:
        beats()
        if beats.count() >= 3:
            raise QueueError("heartbeat requires a running job")

    code = job_run.run(
        [sys.executable, "-c", "import time; time.sleep(0.8)"],
        beat=beat,
        interval=0.05,
        log=messages.append,
    )
    assert code == 0
    assert beats.count() == 3
    assert messages == [
        "heartbeat stopped: heartbeat requires a running job"
    ]


def test_transient_heartbeat_errors_are_logged_and_retried() -> None:
    beats = Beats(fail_with=[])
    messages: list[str] = []
    failures = [sqlite3.OperationalError("database is locked")]

    def beat() -> None:
        beats()
        if beats.count() == 2 and failures:
            raise failures.pop()

    code = job_run.run(
        [sys.executable, "-c", "import time; time.sleep(0.5)"],
        beat=beat,
        interval=0.05,
        log=messages.append,
    )
    assert code == 0
    assert beats.count() >= 4
    assert messages == ["heartbeat failed, retrying: database is locked"]


def test_first_heartbeat_failure_refuses_to_start_the_command(tmp_path: Path) -> None:
    marker = tmp_path / "ran"

    def beat() -> None:
        raise QueueError("heartbeat requires a running job")

    with pytest.raises(QueueError):
        job_run.run(
            [sys.executable, "-c", f"open({str(marker)!r}, 'w')"],
            beat=beat,
            interval=1,
            log=lambda _m: None,
        )
    assert not marker.exists()


@pytest.mark.parametrize(
    "source,expected",
    [
        ("raise SystemExit(3)", 3),
        ("import os, signal; os.kill(os.getpid(), signal.SIGKILL)", 128 + 9),
    ],
)
def test_exit_status_is_the_commands(source: str, expected: int) -> None:
    assert (
        job_run.run(
            [sys.executable, "-c", source],
            beat=lambda: None,
            interval=1,
            log=lambda _m: None,
        )
        == expected
    )


def test_missing_command_is_exit_127() -> None:
    messages: list[str] = []
    code = job_run.run(
        ["/nonexistent/harness"],
        beat=lambda: None,
        interval=1,
        log=messages.append,
    )
    assert code == 127
    assert messages and messages[0].startswith("cannot start /nonexistent/harness")


MACOS_ANCHOR = pytest.mark.skipif(sys.platform != "darwin", reason="macOS anchor")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _gone_within(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while _alive(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def _kill_if_alive(pid_file: Path) -> None:
    if not pid_file.exists():
        return
    try:
        os.kill(int(pid_file.read_text()), signal.SIGKILL)
    except ProcessLookupError:
        pass


@MACOS_ANCHOR
@pytest.mark.parametrize(
    "source,expected",
    [
        ("import sys; sys.exit(3)", 3),
        ("import os; os.kill(os.getpid(), 15)", 128 + signal.SIGTERM),
    ],
    ids=["exit-3", "sigterm"],
)
def test_macos_the_anchor_passes_the_commands_exit_status_through(
    source: str, expected: int
) -> None:
    workers: list[int] = []
    code = job_run.run(
        [sys.executable, "-c", source],
        beat=lambda: None,
        interval=1,
        log=lambda _m: None,
        started=workers.append,
    )
    assert code == expected
    assert len(workers) == 1 and not _alive(workers[0]), "the anchor is gone once run returns"


@MACOS_ANCHOR
def test_macos_a_sigterm_to_the_recorded_worker_pid_reaches_the_command(tmp_path: Path) -> None:
    ready, marker = tmp_path / "ready", tmp_path / "marker"
    source = (
        "import signal, sys, time\n"
        "def stop(*_):\n"
        f"    open({str(marker)!r}, 'w').close()\n"
        "    sys.exit(7)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        f"open({str(ready)!r}, 'w').close()\n"
        "time.sleep(30)\n"
    )
    workers: list[int] = []

    def signal_the_worker_once_ready() -> None:
        deadline = time.monotonic() + 20
        while not (workers and ready.exists()):
            if time.monotonic() >= deadline:
                return
            time.sleep(0.02)
        os.kill(workers[0], signal.SIGTERM)

    sender = threading.Thread(target=signal_the_worker_once_ready, daemon=True)
    sender.start()
    code = job_run.run(
        [sys.executable, "-c", source],
        beat=lambda: None,
        interval=1,
        log=lambda _m: None,
        started=workers.append,
    )
    sender.join(5)
    assert ready.exists(), "the command started and installed its SIGTERM handler"
    assert code == 7, "the command exited from its SIGTERM handler"
    assert marker.exists(), "the SIGTERM sent to the worker pid reached the command"


@MACOS_ANCHOR
def test_macos_a_command_that_cannot_start_is_127_and_never_execed() -> None:
    workers: list[int] = []
    execed: list[bool] = []
    code = job_run.run(
        ["/nonexistent/harness"],
        beat=lambda: None,
        interval=1,
        log=lambda _m: None,
        started=workers.append,
        execed=lambda: execed.append(True),
    )
    assert code == 127
    assert execed == [], "execed is never called for a command that did not start"
    assert len(workers) == 1 and not _alive(workers[0]), "nothing of the run is left"


SLOW_LAUNCHER = """\
#!/bin/sh
for word; do last=$word; done
case " $* " in *" --help "*) echo "  --expand-environment=BOOL"; exit 0;; esac
[ "$last" = true ] && exit 0
sleep "$FAKE_LAUNCH_SECONDS"
exit 1
"""


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="systemd scopes are Linux only")
@pytest.mark.parametrize(
    "launch_seconds, budget_seconds", [(process_tree.CONTAIN_SECONDS + 0.5, None), (60, 1.0)]
)
def test_a_launcher_that_never_starts_the_gate_leaves_the_command_unstarted(
    tmp_path: Path, launch_seconds: float, budget_seconds: float | None
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, body in (("loginctl", "#!/bin/sh\necho yes\n"), ("systemd-run", SLOW_LAUNCHER)):
        (fake_bin / name).write_text(body)
        (fake_bin / name).chmod(0o755)
    marker = tmp_path / "ran"
    workers: list[int] = []
    execed: list[bool] = []
    budget = job_run.Budget(budget_seconds) if budget_seconds is not None else None
    began = time.monotonic()
    code = job_run.run(
        [sys.executable, "-c", f"open({str(marker)!r}, 'w')"],
        beat=lambda: None,
        interval=60,
        log=lambda _m: None,
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_LAUNCH_SECONDS": str(launch_seconds),
        },
        started=workers.append,
        execed=lambda: execed.append(True),
        budget=budget,
    )
    assert code == 127
    assert not marker.exists(), "the command never ran"
    assert execed == [], "a launcher's exit is never taken for the command's exec"
    assert workers == [], "a launcher that never started the gate is never recorded as the worker"
    assert time.monotonic() - began < 30, "the wait for the gate ends within the budget"
    if budget is not None:
        assert budget.expired


@pytest.fixture
def descriptors_past_1023():
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    needed = 1100
    if hard != resource.RLIM_INFINITY and hard < needed:
        pytest.skip(f"the hard RLIMIT_NOFILE {hard} is below the {needed} a pipe past fd 1023 needs")
    if soft != resource.RLIM_INFINITY and soft < needed:
        resource.setrlimit(resource.RLIMIT_NOFILE, (needed, hard))
    try:
        yield
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


@pytest.mark.usefixtures("descriptors_past_1023")
def test_a_status_pipe_past_fd_1023_still_reports_the_exit_code() -> None:
    read, write = os.pipe()
    high = fcntl.fcntl(read, fcntl.F_DUPFD, 1024)
    os.close(read)
    child = subprocess.Popen(["true"])
    try:
        exited = job_run._Exit(child, high)
        assert exited.wait(0.05) is None
        os.write(write, b"7\n")
        assert exited.wait(1) == 7
    finally:
        child.wait()
        os.close(write)
        os.close(high)


@pytest.mark.usefixtures("descriptors_past_1023")
def test_a_gated_run_whose_report_pipe_is_past_fd_1023_runs_its_command(tmp_path: Path) -> None:
    filler = []
    while (fd := os.open(os.devnull, os.O_RDONLY)) < 1024:
        filler.append(fd)
    os.close(fd)
    marker = tmp_path / "ran"
    workers: list[int] = []
    execed: list[bool] = []
    try:
        code = job_run.run(
            [sys.executable, "-c", f"open({str(marker)!r}, 'w')"],
            beat=lambda: None,
            interval=60,
            log=lambda _m: None,
            started=workers.append,
            execed=lambda: execed.append(True),
            budget=job_run.Budget(60),
        )
    finally:
        for fd in filler:
            os.close(fd)
    assert code == 0
    assert marker.exists(), "the command ran"
    assert execed == [True]
    assert len(workers) == 1 and not _alive(workers[0]), "nothing of the run is left"


def test_a_startup_wait_that_raises_leaves_no_launcher_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        launched.append(real_popen(*args, **kwargs))
        return launched[-1]

    def refusing(_fd: int, _timeout: float) -> bool:
        raise OSError("the report pipe cannot be polled")

    monkeypatch.setattr(job_run.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(job_run, "_readable", refusing)
    marker = tmp_path / "ran"
    with pytest.raises(OSError, match="cannot be polled"):
        job_run.run(
            [sys.executable, "-c", f"open({str(marker)!r}, 'w')"],
            beat=lambda: None,
            interval=60,
            log=lambda _m: None,
            started=lambda _pid: None,
            budget=job_run.Budget(60),
        )
    assert launched, "the launcher was spawned"
    assert all(child.returncode is not None for child in launched), "the launcher was reaped"
    assert not marker.exists(), "the command never ran"


ANCHOR_SIGNALLED_BEFORE_ITS_COMMAND = """\
import functools, os, subprocess, sys

def signal_the_group_then_popen(group_signal, real_popen, *args, **kwargs):
    os.killpg(0, group_signal)
    return real_popen(*args, **kwargs)

subprocess.Popen = functools.partial(signal_the_group_then_popen, int(sys.argv[1]), subprocess.Popen)
anchor = sys.argv[2]
sys.argv = [sys.argv[0], *sys.argv[3:]]
exec(anchor)
"""


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_the_anchor_sends_a_group_signal_from_before_its_command_existed_to_it(
    signum: int,
) -> None:
    report_read, report_write = os.pipe()
    anchor = subprocess.Popen(
        [
            sys.executable, "-I", "-S", "-c", ANCHOR_SIGNALLED_BEFORE_ITS_COMMAND,
            str(signum), job_run._ANCHOR, str(report_write), "sleep", "20",
        ],
        pass_fds=(report_write,),
        start_new_session=True,
    )
    os.close(report_write)
    said = b""
    try:
        deadline = time.monotonic() + 10
        while not said.endswith(b"\n") and time.monotonic() < deadline:
            if job_run._readable(report_read, 0.1):
                chunk = os.read(report_read, 64)
                if not chunk:
                    break
                said += chunk
    finally:
        os.killpg(anchor.pid, signal.SIGKILL)
        anchor.wait()
        os.close(report_read)
    assert said == b"s%d\n" % -signum, "the command started, then died of the signal"


SETSID_GRANDCHILD = """\
import os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
open(sys.argv[1] + ".tmp", "w").write(str(os.getpid()))
os.replace(sys.argv[1] + ".tmp", sys.argv[1])
time.sleep(60)
"""

LEAVES_A_SETSID_GRANDCHILD = """\
import os, subprocess, sys, time
subprocess.Popen(
    [sys.executable, "-c", sys.argv[1], sys.argv[2]],
    env={"PATH": os.environ.get("PATH", "")}, start_new_session=True,
)
while not os.path.exists(sys.argv[2]):
    time.sleep(0.02)
"""


@MACOS_ANCHOR
def test_macos_run_kills_a_setsid_grandchild_whose_parent_exited_and_the_anchor(
    tmp_path: Path,
) -> None:
    grandchild = tmp_path / "grandchild.pid"
    workers: list[int] = []
    try:
        code = job_run.run(
            [sys.executable, "-c", LEAVES_A_SETSID_GRANDCHILD, SETSID_GRANDCHILD, str(grandchild)],
            beat=lambda: None,
            interval=1,
            log=lambda _m: None,
            started=workers.append,
        )
        assert code == 0
        assert grandchild.exists(), "the grandchild started before the command exited"
        assert len(workers) == 1 and not _alive(workers[0]), "the anchor is gone once run returns"
        assert _gone_within(int(grandchild.read_text()), 2), (
            "the setsid grandchild, untagged and orphaned, died with the run"
        )
    finally:
        _kill_if_alive(grandchild)


KILLS_ITSELF_AFTER_A_SETSID_CHILD = """\
import os, signal, subprocess, sys, time
subprocess.Popen([sys.executable, "-c", sys.argv[1], sys.argv[2]], start_new_session=True)
while not os.path.exists(sys.argv[2]):
    time.sleep(0.02)
os.kill(os.getpid(), signal.SIGKILL)
"""


def test_a_setsid_child_of_a_command_killed_by_sigkill_is_gone_once_run_returns(
    tmp_path: Path,
) -> None:
    """#1210: a vite-like child keeps the run's tag, leaves the session and
    ignores SIGTERM; the command that started it dies by SIGKILL."""
    child = tmp_path / "child.pid"
    try:
        code = job_run.run(
            [sys.executable, "-c", KILLS_ITSELF_AFTER_A_SETSID_CHILD, SETSID_GRANDCHILD, str(child)],
            beat=lambda: None,
            interval=1,
            log=lambda _m: None,
            started=lambda _pid: None,
        )
        assert code == 128 + signal.SIGKILL
        assert child.exists(), "the child started before the command was killed"
        assert _gone_within(int(child.read_text()), 2), "the child died with the run"
    finally:
        _kill_if_alive(child)


SCOPE_SLICE = "/user.slice/user-1000.slice/user@1000.service/app.slice"


@pytest.mark.parametrize(
    "cgroup", ["writable", "absent", "unwritable", "unreachable"],
    ids=["cgroup-kill", "no-cgroup-kill", "unwritable-cgroup-kill", "unreachable-cgroup-kill"],
)
def test_a_scoped_trees_kill_round_writes_cgroup_kill_then_kills_each_member(
    tmp_path: Path, cgroup: str
) -> None:
    """#1210: cgroup.kill of the run's scope comes first; the SIGSTOP/SIGKILL
    rounds follow, and stand alone where cgroup.kill is absent, refuses the
    write, or sits under a directory this user cannot search."""
    unit = process_tree.scope_unit("a" * 32)
    slice_dir = tmp_path / "cgroup" / SCOPE_SLICE.lstrip("/")
    scope_dir = slice_dir / unit
    scope_dir.mkdir(parents=True)
    kill_file = scope_dir / "cgroup.kill"
    if cgroup != "absent":
        kill_file.write_text("")
    if cgroup == "unwritable":
        kill_file.chmod(0o444)
    if cgroup == "unreachable":
        slice_dir.chmod(0)
    live = {
        41: process_tree.Process(41, 1, "s41", False, 41, unit, f"{SCOPE_SLICE}/{unit}"),
        42: process_tree.Process(42, 41, "s42", False, 41, unit, f"{SCOPE_SLICE}/{unit}"),
        43: process_tree.Process(43, 1, "s43", False, 43, "session-4.scope", "/user.slice/session-4.scope"),
    }
    signals: list[tuple[int, int]] = []

    def signal_pid(pid: int, signum: int) -> None:
        signals.append((pid, signum))
        if signum == signal.SIGKILL:
            live.pop(pid, None)

    tree = process_tree.Tree(
        41, None, scan=lambda _tag: dict(live), signal_pid=signal_pid, known={},
        scope=unit, cgroup_root=tmp_path / "cgroup",
    )
    try:
        tree.end(lambda _m: None)
    finally:
        slice_dir.chmod(0o755)
    if cgroup == "writable":
        assert kill_file.read_text() == "1", "the scope's cgroup.kill was written"
    elif cgroup == "absent":
        assert not kill_file.exists(), "no cgroup.kill is made where the kernel has none"
    else:
        assert kill_file.read_text() == "", "cgroup.kill could not be written"
    assert sorted(pid for pid, signum in signals if signum == signal.SIGKILL) == [41, 42]
    assert 43 in live, "a process outside the scope is never signalled"


@MACOS_ANCHOR
def test_macos_a_command_past_its_budget_that_ignores_sigterm_is_killed_with_the_anchor(
    tmp_path: Path,
) -> None:
    command = tmp_path / "command.pid"
    source = (
        "import os, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"open({str(command)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    workers: list[int] = []
    budget = job_run.Budget(seconds=2, grace=1)
    try:
        code = job_run.run(
            [sys.executable, "-c", source],
            beat=lambda: None,
            interval=1,
            log=lambda _m: None,
            started=workers.append,
            budget=budget,
        )
        assert command.exists(), "the command started and ignored SIGTERM"
        assert code == 128 + signal.SIGKILL
        assert budget.expired
        assert len(workers) == 1 and not _alive(workers[0]), "the anchor is gone once run returns"
        assert _gone_within(int(command.read_text()), 2), "the command was killed with the tree"
    finally:
        _kill_if_alive(command)


# --- the CLI, against a real dispatcher database -----------------------------


def _queue(tmp_path: Path) -> SQLiteJobQueue:
    return SQLiteJobQueue(tmp_path / "orch.db", clock=time.time)


def _running_job(
    queue: SQLiteJobQueue,
    *,
    timeout: int = 60,
    recipient: str = "codex",
    action: str = "review",
    machines: Path | None = None,
    detail: str | None = None,
):
    queue.enqueue(
        JobRequest(
            repo="acme/widgets",
            ticket_id="12",
            action=action,
            recipient=recipient,
            source_comment_id="1",
            timeout_seconds=timeout,
            detail=detail,
        ),
        dedupe_key=f"k-{time.monotonic_ns()}",
    )
    return pool_claim(queue, recipient, machines)


def _last_activity(queue: SQLiteJobQueue, job_id: str) -> float:
    with queue.connect() as connection:
        row = connection.execute(
            "SELECT last_activity_at FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    return float(row[0])


def _state(queue: SQLiteJobQueue, job_id: str) -> str:
    with queue.connect() as connection:
        return str(
            connection.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
        )


def _orch(*args: str, **kwargs) -> subprocess.Popen:
    env = {k: v for k, v in os.environ.items() if k != "ORCH_DB"}
    env.update(kwargs.pop("env", {}))
    return subprocess.Popen(
        [sys.executable, str(ORCH), *args],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **kwargs,
    )


def test_cli_job_run_heartbeats_and_passes_the_job_to_the_command(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    job = _running_job(queue)
    claimed_at = _last_activity(queue, job.id)
    out = tmp_path / "env.json"
    script = (
        "import json, os, sys, time; "
        f"json.dump({{k: os.environ.get(k) for k in ('ORCH_JOB_ID', 'ORCH_JOB_BOT', 'ORCH_DB', 'ORCH_CPU_CAP', 'PYTEST_XDIST_AUTO_NUM_WORKERS', 'ORCH_TEST_SUITE_MAX')}}, open({str(out)!r}, 'w')); "
        "time.sleep(1.0)"
    )
    proc = _orch(
        "job", "run", "codex-bot", job.id, "--machines", str(LOCAL_BOX_MACHINES), "--db", str(tmp_path / "orch.db"),
        "--interval", "0.2", "--", sys.executable, "-c", script,
    )
    _, stderr = proc.communicate(timeout=20)

    assert proc.returncode == 0, stderr
    assert _last_activity(queue, job.id) > claimed_at
    assert json.loads(out.read_text()) == {
        "ORCH_JOB_ID": job.id,
        "ORCH_JOB_BOT": "codex-bot",
        "ORCH_DB": str(tmp_path / "orch.db"),
        "ORCH_CPU_CAP": "4",
        "PYTEST_XDIST_AUTO_NUM_WORKERS": str(TEST_WORKERS),
        "ORCH_TEST_SUITE_MAX": "1",
    }
    after = _last_activity(queue, job.id)
    time.sleep(0.6)
    assert _last_activity(queue, job.id) == after  # no orphan heartbeat
    assert _state(queue, job.id) == "running"  # done/failed stays the bot's call


def test_cli_job_run_stops_heartbeating_when_the_command_finishes_the_job(
    tmp_path: Path,
) -> None:
    queue = _queue(tmp_path)
    job = _running_job(queue)
    database = tmp_path / "orch.db"
    serve_job(database, job.id, "99")
    script = (
        "import subprocess, sys, time; "
        f"subprocess.run([sys.executable, {str(ORCH)!r}, 'job', 'done', 'codex-bot', "
        f"{job.id!r}, '--handoff', '99', '--db', {str(database)!r}], check=True); "
        "time.sleep(0.8)"
    )
    proc = _orch(
        "job", "run", "codex-bot", job.id, "--machines", str(LOCAL_BOX_MACHINES), "--db", str(database),
        "--interval", "0.1", "--", sys.executable, "-c", script,
    )
    _, stderr = proc.communicate(timeout=20)

    assert proc.returncode == 0, stderr
    assert _state(queue, job.id) == "done"
    assert stderr.count("heartbeat stopped") == 1
    assert "Traceback" not in stderr


def test_cli_job_run_forwards_sigterm_and_heartbeat_dies_with_it(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    job = _running_job(queue)
    ready = tmp_path / "ready"
    script = (
        "import pathlib, signal, sys, time; "
        "signal.signal(signal.SIGTERM, lambda *a: sys.exit(42)); "
        f"pathlib.Path({str(ready)!r}).touch(); "
        "time.sleep(30)"
    )
    proc = _orch(
        "job", "run", "codex-bot", job.id, "--machines", str(LOCAL_BOX_MACHINES), "--db", str(tmp_path / "orch.db"),
        "--interval", "0.1", "--", sys.executable, "-c", script,
    )
    try:
        deadline = time.monotonic() + 15
        while not ready.exists():
            assert time.monotonic() < deadline
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        _, stderr = proc.communicate(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert proc.returncode == 42, stderr
    after = _last_activity(queue, job.id)
    time.sleep(0.4)
    assert _last_activity(queue, job.id) == after


def test_cli_job_run_stops_on_a_group_sigint_with_its_command(tmp_path: Path) -> None:
    # Regression (#52 F1.7): SIGINT was set to SIG_IGN before the command was
    # spawned, and an ignored signal survives exec, so Ctrl-C (a SIGINT to
    # the foreground process group) stopped neither the wrapper nor the
    # command, and the heartbeat kept beating.
    queue = _queue(tmp_path)
    job = _running_job(queue)
    ready = tmp_path / "ready"
    script = (
        "import pathlib, time; "
        f"pathlib.Path({str(ready)!r}).touch(); "
        "time.sleep(30)"
    )
    proc = _orch(
        "job", "run", "codex-bot", job.id, "--machines", str(LOCAL_BOX_MACHINES), "--db", str(tmp_path / "orch.db"),
        "--interval", "0.1", "--", sys.executable, "-c", script,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 15
        while not ready.exists():
            assert time.monotonic() < deadline
            time.sleep(0.05)
        os.killpg(proc.pid, signal.SIGINT)
        _, stderr = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
    assert proc.returncode == 128 + signal.SIGINT, stderr
    after = _last_activity(queue, job.id)
    time.sleep(0.4)
    assert _last_activity(queue, job.id) == after


def test_the_command_starts_with_default_sigint_handling() -> None:
    probe = (
        "import signal, sys; "
        "sys.exit(0 if signal.getsignal(signal.SIGINT) is signal.default_int_handler else 9)"
    )
    code = job_run.run([sys.executable, "-c", probe], beat=lambda: None, interval=1, log=lambda _m: None)
    assert code == 0


def test_an_ignored_sigint_stays_ignored_for_the_command() -> None:
    # A caller that ignores SIGINT (e.g. a background job of a script) passes
    # that on, as it would to any command it runs.
    probe = (
        "import signal, sys; "
        "sys.exit(0 if signal.getsignal(signal.SIGINT) is signal.SIG_IGN else 9)"
    )
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        code = job_run.run([sys.executable, "-c", probe], beat=lambda: None, interval=1, log=lambda _m: None)
    finally:
        signal.signal(signal.SIGINT, previous)
    assert code == 0


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_a_signal_during_setup_is_not_dropped_and_the_command_never_runs(
    tmp_path: Path, signum: int
) -> None:
    # Regression (#52 F1.7): a TERM/HUP that arrived before the command was
    # spawned was dropped, and the command then ran anyway.
    marker = tmp_path / "ran"
    torn_down: list[bool] = []

    def setup() -> None:
        os.kill(os.getpid(), signum)
        return None

    previous = signal.signal(signum, lambda *_a: None)  # keep pytest alive on the old code
    try:
        code = job_run.run(
            [sys.executable, "-c", f"open({str(marker)!r}, 'w')"],
            beat=lambda: None,
            interval=1,
            log=lambda _m: None,
            setup=setup,
            teardown=lambda: torn_down.append(True),
        )
    finally:
        signal.signal(signum, previous)
    assert code == 128 + signum
    assert not marker.exists()
    assert torn_down == [True]


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_a_signal_before_the_gate_opens_means_the_command_never_runs(
    tmp_path: Path, signum: int
) -> None:
    marker = tmp_path / "ran"
    execed: list[bool] = []
    code = job_run.run(
        [sys.executable, "-c", f"open({str(marker)!r}, 'w')"],
        beat=lambda: None,
        interval=1,
        log=lambda _m: None,
        started=lambda _pid: os.kill(os.getpid(), signum),
        execed=lambda: execed.append(True),
    )
    assert code == 128 + signum
    assert not marker.exists(), "the command never ran"
    assert execed == [], "execed is never called for a command that did not start"


def test_a_signal_while_the_command_is_spawning_is_forwarded(monkeypatch) -> None:
    # Regression (#52 F1.7): a TERM between the spawn and `child` being set
    # was dropped, leaving the command running.
    real_popen = subprocess.Popen

    def spawn_then_signal(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        os.kill(os.getpid(), signal.SIGTERM)
        return child

    monkeypatch.setattr(subprocess, "Popen", spawn_then_signal)
    previous = signal.getsignal(signal.SIGTERM)
    started = time.monotonic()
    try:
        code = job_run.run(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            beat=lambda: None,
            interval=1,
            log=lambda _m: None,
        )
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert code == 128 + signal.SIGTERM
    assert time.monotonic() - started < 4


@pytest.mark.parametrize("waiter_dir", [True, False])
def test_cli_job_run_gives_the_command_a_job_tmpdir_and_removes_it(
    tmp_path: Path, waiter_dir: bool
) -> None:
    queue = _queue(tmp_path)
    job = _running_job(queue)
    job_dir = (
        tmp_path / "jobs" / job.id if waiter_dir else tmp_path / "telemetry-codex-bot" / job.id
    )
    seen = tmp_path / "seen.json"
    script = (
        "import json, os, pathlib; "
        "pathlib.Path(os.environ['TMPDIR'], 'x').write_text('scratch'); "
        f"json.dump({{k: os.environ.get(k) for k in ('TMPDIR', 'TMP', 'TEMP')}}, open({str(seen)!r}, 'w'))"
    )
    proc = _orch(
        "job", "run", "codex-bot", job.id, "--machines", str(LOCAL_BOX_MACHINES), "--db", str(tmp_path / "orch.db"),
        "--", sys.executable, "-c", script,
        env={"ORCH_JOB_STATE_DIR": str(job_dir) if waiter_dir else ""},
    )
    _, stderr = proc.communicate(timeout=20)

    assert proc.returncode == 0, stderr
    scratch = str(job_dir / "tmp")
    assert json.loads(seen.read_text()) == {"TMPDIR": scratch, "TMP": scratch, "TEMP": scratch}
    assert not (job_dir / "tmp").exists()


START_CONTAINER = (
    "import os, subprocess; subprocess.run(['docker', 'run', '-d', '--label', "
    "'factory.job=' + os.environ['ORCH_JOB_ID'], 'alpine', 'sleep', '600'], check=True)"
)


def test_cli_job_run_teardown_removes_only_the_jobs_labelled_containers(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    job = _running_job(queue)
    other = queue.enqueue(
        JobRequest(repo="acme/widgets", ticket_id="13", action="review", recipient="codex",
                   source_comment_id="2", timeout_seconds=60),
        dedupe_key="other",
    )
    docker = Docker(tmp_path)
    docker.start_for(other.id)
    docker.start(app="web")
    proc = _orch(
        "job", "run", "codex-bot", job.id, "--machines", str(LOCAL_BOX_MACHINES), "--db", str(queue.path),
        "--", sys.executable, "-c", START_CONTAINER,
        env={"PATH": docker.path(), "ORCH_TELEMETRY": "off"},
    )
    _, stderr = proc.communicate(timeout=30)

    assert proc.returncode == 0, stderr
    assert ["run", "-d", "--label", f"factory.job={job.id}", "alpine", "sleep", "600"] in docker.calls()
    assert sorted(docker.labelled()) == sorted([other.id, ""])


@pytest.mark.parametrize("end", ["killed", "wall-clock"])
def test_cli_job_run_removes_the_containers_of_an_agent_that_never_cleaned_up(
    tmp_path: Path, end: str
) -> None:
    queue = _queue(tmp_path)
    job = _running_job(queue, timeout=60 if end == "killed" else 2)
    docker = Docker(tmp_path)
    after = (
        "os.kill(os.getpid(), signal.SIGKILL)" if end == "killed" else "time.sleep(60)"
    )
    script = f"import os, signal, time\n{START_CONTAINER}\n{after}\n"
    proc = _orch(
        "job", "run", "codex-bot", job.id, "--machines", str(LOCAL_BOX_MACHINES), "--db", str(queue.path),
        "--", sys.executable, "-c", script,
        env={"PATH": docker.path(), "ORCH_TELEMETRY": "off"},
    )
    _, stderr = proc.communicate(timeout=60)

    assert any(call[0] == "run" for call in docker.calls()), stderr
    if end == "killed":
        assert proc.returncode == 128 + signal.SIGKILL, stderr
    else:
        assert proc.returncode != 0 and "wall-clock" in stderr, stderr
    assert docker.labelled() == []


def test_cli_job_run_removes_a_job_tmpdir_its_command_locked(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    job = _running_job(queue)
    locked = tmp_path / "jobs" / job.id / "tmp" / "locked"
    script = (
        "import os, pathlib; locked = pathlib.Path(os.environ['TMPDIR'], 'locked'); "
        "locked.mkdir(); (locked / 'x').write_text('scratch'); locked.chmod(0)"
    )
    proc = _orch(
        "job", "run", "codex-bot", job.id, "--machines", str(LOCAL_BOX_MACHINES), "--db", str(queue.path),
        "--", sys.executable, "-c", script,
        env={"ORCH_JOB_STATE_DIR": str(locked.parent.parent)},
    )
    try:
        _, stderr = proc.communicate(timeout=20)
        assert proc.returncode == 0, stderr
        assert not locked.parent.exists()
    finally:
        if locked.exists():
            locked.chmod(0o700)


@pytest.mark.parametrize("state", ["queued", "done"])
def test_cli_job_run_refuses_a_job_that_is_not_running(tmp_path: Path, state: str) -> None:
    queue = _queue(tmp_path)
    if state == "queued":
        job = queue.enqueue(
            JobRequest(
                repo="acme/widgets", ticket_id="1", action="review",
                recipient="codex", source_comment_id="1", timeout_seconds=60,
            ),
            dedupe_key="q",
        )
    else:
        job = _running_job(queue)
        queue.finish(job.id, owner="codex", handoff_comment_id="5")
    marker = tmp_path / "ran"
    proc = _orch(
        "job", "run", "codex-bot", job.id, "--machines", str(LOCAL_BOX_MACHINES), "--db", str(tmp_path / "orch.db"),
        "--", sys.executable, "-c", f"open({str(marker)!r}, 'w')",
    )
    _, stderr = proc.communicate(timeout=20)

    assert proc.returncode == 1
    assert "heartbeat requires a running job" in stderr
    assert not marker.exists()


def test_cli_job_run_refuses_another_bots_job_and_a_bad_interval(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    job = _running_job(queue, timeout=60)
    database = str(tmp_path / "orch.db")

    marker = tmp_path / "ran"
    other = _orch(
        "job", "run", "claude-bot", job.id, "--machines", str(LOCAL_BOX_MACHINES), "--db", database,
        "--", sys.executable, "-c", f"open({str(marker)!r}, 'w')",
    )
    _, stderr = other.communicate(timeout=20)
    assert other.returncode == 2
    assert "belongs to codex-bot" in stderr
    assert not marker.exists()

    zero = _orch(
        "job", "run", "codex-bot", job.id, "--machines", str(LOCAL_BOX_MACHINES), "--db", database, "--interval", "0", "--", "true"
    )
    _, stderr = zero.communicate(timeout=20)
    assert zero.returncode == 2
    assert "--interval" in stderr

    bare = _orch("job", "run", "codex-bot", job.id, "--machines", str(LOCAL_BOX_MACHINES), "--db", database)
    _, stderr = bare.communicate(timeout=20)
    assert bare.returncode == 2
    assert "command" in stderr


def test_cli_job_run_caps_a_slow_interval_and_says_so(tmp_path: Path) -> None:
    # Factory request on PR #77: a slow override is capped (here at a third
    # of the 60 s timeout), not refused, and the cap is logged.
    queue = _queue(tmp_path)
    job = _running_job(queue, timeout=60)
    slow = _orch(
        "job", "run", "codex-bot", job.id, "--machines", str(LOCAL_BOX_MACHINES), "--db", str(tmp_path / "orch.db"),
        "--interval", "600", "--", "true",
    )
    _, stderr = slow.communicate(timeout=20)
    assert slow.returncode == 0, stderr
    assert "--interval 600 capped to 20 seconds" in stderr


# --- job worktrees through the CLI --------------------------------------------

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env={**os.environ, **GIT_ENV},
        text=True, capture_output=True, check=True,
    ).stdout.strip()


def _origin(tmp_path: Path) -> tuple[str, str]:
    remotes = tmp_path / "remotes"
    bare = remotes / "acme" / "widgets.git"
    bare.parent.mkdir(parents=True)
    _git("init", "--bare", "-q", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    _git("clone", "-q", str(bare), str(seed))
    (seed / "README").write_text("one\n", encoding="utf-8")
    _git("add", ".", cwd=seed)
    _git("commit", "-q", "-m", "one", cwd=seed)
    _git("push", "-q", "origin", "HEAD:main", cwd=seed)
    return f"file://{remotes}/{{repo}}.git", _git("rev-parse", "HEAD", cwd=seed)


@pytest.mark.parametrize("dirty", [False, True])
def test_cli_job_run_worktree_runs_in_a_worktree_and_cleans_it_up(
    tmp_path: Path, dirty: bool
) -> None:
    template, _head = _origin(tmp_path)
    queue = _queue(tmp_path)
    job = _running_job(queue)
    work = tmp_path / "work"
    seen = tmp_path / "seen.json"
    script = (
        "import json, os, pathlib, subprocess; "
        f"json.dump({{'cwd': os.getcwd(), 'env': os.environ['ORCH_JOB_WORKTREE'], "
        "'git': subprocess.run(['git', 'rev-parse', '--git-common-dir'], capture_output=True, text=True).stdout.strip()}, "
        f"open({str(seen)!r}, 'w')); "
        + ("pathlib.Path('scratch.txt').write_text('wip')" if dirty else "pass")
    )
    proc = _orch(
        "job", "run", "codex-bot", job.id, "--machines", str(LOCAL_BOX_MACHINES), "--db", str(tmp_path / "orch.db"),
        "--worktree", "--work-root", str(work), "--git-url-template", template,
        "--", sys.executable, "-c", script,
        env=GIT_ENV,
    )
    _, stderr = proc.communicate(timeout=60)

    assert proc.returncode == 0, stderr
    checkout = work / "jobs" / job.id
    observed = json.loads(seen.read_text())
    assert observed["cwd"] == observed["env"] == str(checkout)
    assert Path(observed["git"]) == work / "clones" / "acme" / "widgets.git"
    if dirty:
        assert checkout.is_dir()
        assert f"kept job worktree {checkout}: uncommitted changes" in stderr
    else:
        assert not checkout.exists()
        assert not (work / "jobs" / f"{job.id}.json").exists()
        assert f"removed job worktree {checkout}" in stderr
        listing = _git("-C", str(work / "clones/acme/widgets.git"), "worktree", "list")
        assert str(checkout) not in listing


def test_cli_job_run_worktree_failure_does_not_run_the_command(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    job = _running_job(queue)
    marker = tmp_path / "ran"
    proc = _orch(
        "job", "run", "codex-bot", job.id, "--machines", str(LOCAL_BOX_MACHINES), "--db", str(tmp_path / "orch.db"),
        "--worktree", "--work-root", str(tmp_path / "work"),
        "--git-url-template", f"file://{tmp_path}/missing/{{repo}}.git",
        "--", sys.executable, "-c", f"open({str(marker)!r}, 'w')",
        env=GIT_ENV,
    )
    _, stderr = proc.communicate(timeout=60)
    assert proc.returncode == 1
    assert "git fetch failed" in stderr
    assert not marker.exists()


def test_cli_gc_reports_and_removes_old_unchanged_worktrees(
    tmp_path: Path, monkeypatch
) -> None:
    from lib import worktrees

    template, head = _origin(tmp_path)
    work = tmp_path / "work"
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    checkout = worktrees.prepare(
        work, "a" * 32, "acme/widgets", head, url_template=template, pid=2**22 + 7
    )
    record = json.loads(checkout.record.read_text())
    record["created_at"] -= 48 * 3600
    record.pop("pid_start", None)
    checkout.record.write_text(json.dumps(record))

    young = _orch("gc", "--work-root", str(work), "--older-than", "72", env=GIT_ENV)
    out, err = young.communicate(timeout=60)
    assert young.returncode == 0, err
    assert out == ""
    assert checkout.path.exists()

    old = _orch("gc", "--work-root", str(work), env=GIT_ENV)
    out, err = old.communicate(timeout=60)
    assert old.returncode == 0, err
    assert out == f"removed job worktree {checkout.path} (acme/widgets, 48h old)\n"
    assert not checkout.path.exists()


KEEPER_HELD_RUN = """import sys
from pathlib import Path
from lib.job_run import run
here = Path(sys.argv[1])
code = run([sys.executable, "-c", f"open({str(here / 'ran')!r}, 'w')"], beat=lambda: None,
    interval=60, log=print, started=lambda pid: None)
(here / "code").write_text(str(code))
"""


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the keeper is Linux only")
def test_the_command_waits_for_a_keeper_that_ignores_term(tmp_path: Path) -> None:
    """#827 f-6: a TERM that ends the keeper before it ignores TERM leaves the
    session unproven, so the command never runs beside an unprotected keeper."""
    shims = tmp_path / "shims"
    shims.mkdir()
    real = subprocess.run(["sh", "-c", "command -v sh"], capture_output=True, text=True).stdout.strip()
    held = tmp_path / "held"
    (shims / "sh").write_text(
        "#!/bin/sh\n"
        f'if [ "$3" = orch-keeper ]; then echo $$ > {held}.tmp; mv {held}.tmp {held}; '
        "while :; do sleep 0.05; done; fi\n"
        f'exec {real} "$@"\n'
    )
    (shims / "sh").chmod(0o755)
    script = tmp_path / "held_run.py"
    script.write_text(KEEPER_HELD_RUN)
    runner = subprocess.Popen(
        [sys.executable, str(script), str(tmp_path)],
        env={**os.environ, "PATH": f"{shims}:{os.environ['PATH']}",
             "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 15
        while not held.exists():
            assert time.monotonic() < deadline, "the runner started the keeper"
            time.sleep(0.05)
        time.sleep(0.5)
        assert not (tmp_path / "ran").exists(), "the command waits while the keeper is held"
        os.kill(int(held.read_text()), signal.SIGTERM)
        output, _ = runner.communicate(timeout=15)
    finally:
        if runner.poll() is None:
            os.killpg(runner.pid, signal.SIGKILL)
            runner.communicate()
    assert (tmp_path / "code").read_text() == "127", output
    assert "cannot start the session keeper" in output
    assert not (tmp_path / "ran").exists(), "the command never ran"


KEEPER_BARE_PATH_RUN = """import sys
from pathlib import Path
from lib.job_run import run
here = Path(sys.argv[1])
code = run([sys.executable, "-c", f"open({str(here / 'ran')!r}, 'w')"], env={"PATH": "/nonexistent"},
    beat=lambda: None, interval=60, log=print)
(here / "code").write_text(str(code))
"""


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the keeper is Linux only")
def test_the_keeper_does_not_search_the_command_path(tmp_path: Path) -> None:
    """#863 f-8: the keeper is the runner's own process, so a command env whose
    PATH holds no shell still runs the command."""
    script = tmp_path / "bare_path_run.py"
    script.write_text(KEEPER_BARE_PATH_RUN)
    runner = subprocess.run(
        [sys.executable, str(script), str(tmp_path)],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
        start_new_session=True,
    )
    assert (tmp_path / "code").read_text() == "0", runner.stdout + runner.stderr
    assert (tmp_path / "ran").exists(), "the command ran"


# --- what the job ran under (F-01a) -------------------------------------------


def _box_only(tmp_path: Path, factory_root: Path) -> Path:
    machines = tmp_path / "machines.yml"
    machines.write_text(
        "devices:\n"
        "  - name: box\n"
        "    local: true\n"
        "    slot_max: 3\n"
        "    harnesses:\n"
        "      claude: claude-bot\n"
        "      codex: codex-bot\n"
        f"    factory_root: {factory_root}\n",
        encoding="utf-8",
    )
    return machines


def _fake_harness(tmp_path: Path, version: str) -> Path:
    path = tmp_path / "fake-harness"
    path.write_text(f'#!/bin/sh\nif [ "$1" = --version ]; then {version}; fi\nexit 0\n', encoding="utf-8")
    path.chmod(0o755)
    return path


def _brief_file(tmp_path: Path, action: str, factory_root: Path) -> tuple[Path, Router]:
    router = resolve("reviewer" if action == "review" else "builder", "codex-bot", str(factory_root))
    assert isinstance(router, Router), router
    path = tmp_path / "brief.md"
    path.write_text(f"# Job\n\n{section(router)}\n\n## Task\nDo the {action}.\n", encoding="utf-8")
    return path, router


@pytest.mark.parametrize("wrapper", [(), ("/usr/bin/env", "--")], ids=["direct", "env"])
@pytest.mark.parametrize("telemetry", ["on", "off"])
@pytest.mark.parametrize("action", ["plan", "build", "fix", "review"])
def test_cli_job_run_records_what_the_job_ran_under(
    tmp_path: Path, action: str, telemetry: str, wrapper: tuple[str, ...]
) -> None:
    factory = tmp_path / "factory"
    factory.mkdir()
    _git("init", "-q", str(factory))
    _git("commit", "-q", "--allow-empty", "-m", "factory", cwd=factory)
    revision = _git("rev-parse", "HEAD", cwd=factory)
    machines = _box_only(tmp_path, factory)
    queue = _queue(tmp_path)
    job = _running_job(queue, action=action, machines=machines)
    brief, router = _brief_file(tmp_path, action, factory)
    with brief.open("rb") as stdin:
        proc = _orch(
            "job", "run", "codex-bot", job.id, "--machines", str(machines), "--db", str(tmp_path / "orch.db"),
            "--", *wrapper, str(_fake_harness(tmp_path, "echo fake-cli 9.9.9")), "--model", "test-model-1",
            "-c", "model_reasoning_effort=high",
            env={"ORCH_JOB_BRIEF": str(brief), "ORCH_TELEMETRY": telemetry},
            stdin=stdin,
        )
        _, stderr = proc.communicate(timeout=30)

    assert proc.returncode == 0, stderr
    with queue.connect() as connection:
        recorded = exposure.read(connection, job.id)
    assert recorded == exposure.Exposure(
        model="test-model-1",
        cli_version="fake-cli 9.9.9",
        brief_sha256=hashlib.sha256(brief.read_bytes()).hexdigest(),
        skill_paths=router.paths,
        host="box",
        factory_revision=revision,
        effort="high",
        harness="codex",
    )
    assert stderr.count("exposure: recorded (") == 1


_PLAN_ROUTING = {
    "builder": "claude", "builder_model": "claude-opus-5-5", "builder_effort": "medium",
    "planner": "claude", "plan_model": "claude-fable-5-2", "plan_effort": "high",
}
_REVIEW_ROUTING = {"builder": "claude", "reviewer": "codex", "reviewer_model": "gpt-6.2-sol", "reviewer_effort": "high"}


@pytest.mark.parametrize(
    ("bot", "recipient", "action", "wrapper_name", "args", "routing", "model", "harness"),
    [
        ("claude-bot", "claude", "plan", "claude-harness",
         ("-p", "--model", "claude-opus-5-5", "--effort", "medium"), _PLAN_ROUTING, "claude-fable-5-2", "claude"),
        ("codex-bot", "codex", "review", "codex-review-harness",
         ("exec", "-m", "gpt-5.5"), _REVIEW_ROUTING, "gpt-6.2-sol", "codex"),
    ],
)
def test_cli_job_run_records_the_routed_model_a_wrapper_harness_ran_under(
    tmp_path: Path, bot: str, recipient: str, action: str, wrapper_name: str,
    args: tuple[str, ...], routing: dict, model: str, harness: str,
) -> None:
    factory = tmp_path / "factory"
    factory.mkdir()
    machines = _box_only(tmp_path, factory)
    queue = _queue(tmp_path)
    detail = json.dumps({"action": action, "routing": routing})
    job = _running_job(queue, recipient=recipient, action=action, machines=machines, detail=detail)
    wrapper = tmp_path / wrapper_name
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrapper.chmod(0o755)
    proc = _orch(
        "job", "run", bot, job.id, "--machines", str(machines), "--db", str(tmp_path / "orch.db"),
        "--", str(wrapper), *args,
        env={"ORCH_JOB_BRIEF": str(tmp_path / "no-brief.md"), "ORCH_TELEMETRY": "off"},
        stdin=subprocess.DEVNULL,
    )
    _, stderr = proc.communicate(timeout=30)

    assert proc.returncode == 0, stderr
    with queue.connect() as connection:
        recorded = exposure.read(connection, job.id)
    assert (recorded.model, recorded.effort, recorded.harness) == (model, "high", harness)


def test_cli_job_run_records_unknown_and_still_runs_when_it_cannot_tell(tmp_path: Path) -> None:
    factory = tmp_path / "factory"
    factory.mkdir()
    machines = _box_only(tmp_path, factory)
    queue = _queue(tmp_path)
    job = _running_job(queue, action="build", machines=machines)
    ran = tmp_path / "ran"
    harness = tmp_path / "fake-harness"
    harness.write_text(f'#!/bin/sh\n[ "$1" = --version ] && exit 3\ntouch {ran}\n', encoding="utf-8")
    harness.chmod(0o755)
    proc = _orch(
        "job", "run", "codex-bot", job.id, "--machines", str(machines), "--db", str(tmp_path / "orch.db"),
        "--", str(harness),
        env={"ORCH_JOB_BRIEF": str(tmp_path / "no-brief.md"), "GIT_CEILING_DIRECTORIES": str(tmp_path),
             "CODEX_HOME": str(tmp_path / "no-codex-home")},
        stdin=subprocess.DEVNULL,
    )
    _, stderr = proc.communicate(timeout=30)

    assert proc.returncode == 0, stderr
    assert ran.exists(), "the command ran"
    with queue.connect() as connection:
        recorded = exposure.read(connection, job.id)
    assert recorded == exposure.Exposure(
        exposure.UNKNOWN, exposure.UNKNOWN, exposure.UNKNOWN, None, "box", exposure.UNKNOWN, exposure.UNKNOWN,
        "codex",
    )


@pytest.mark.parametrize(
    ("printed", "expected"),
    [
        ('{"type":"system","subtype":"init","model":"claude-opus-5-5"}', "claude-opus-5-5"),
        ("no init event here", exposure.UNKNOWN),
    ],
)
def test_cli_job_run_records_the_model_a_claude_harness_reports(
    tmp_path: Path, printed: str, expected: str
) -> None:
    factory = tmp_path / "factory"
    factory.mkdir()
    machines = _box_only(tmp_path, factory)
    queue = _queue(tmp_path)
    job = _running_job(queue, recipient="claude", action="build", machines=machines)
    proc = _orch(
        "job", "run", "claude-bot", job.id, "--machines", str(machines), "--db", str(tmp_path / "orch.db"),
        "--", sys.executable, "-c", f"print({printed!r})",
        env={"ORCH_JOB_BRIEF": str(tmp_path / "no-brief.md"), "ORCH_TELEMETRY": "on"},
        stdin=subprocess.DEVNULL,
    )
    _, stderr = proc.communicate(timeout=30)

    assert proc.returncode == 0, stderr
    with queue.connect() as connection:
        recorded = exposure.read(connection, job.id)
    assert recorded is not None and recorded.model == expected, stderr


def test_cli_job_run_records_the_harness_and_project_config_of_the_job_worktree(tmp_path: Path) -> None:
    template, _head = _origin(tmp_path)
    seed = tmp_path / "seed"
    _fake_harness(seed, "echo harness-cli 2.0.0").rename(seed / "harness")
    (seed / ".codex").mkdir()
    (seed / ".codex" / "config.toml").write_text('model = "project-model"\n', encoding="utf-8")
    _git("add", ".", cwd=seed)
    _git("commit", "-q", "-m", "harness", cwd=seed)
    _git("push", "-q", "origin", "HEAD:main", cwd=seed)
    waiter = tmp_path / "waiter"
    waiter.mkdir()
    _fake_harness(waiter, "echo harness-cli 1.0.0").rename(waiter / "harness")
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text('model = "default-model"\n', encoding="utf-8")
    queue = _queue(tmp_path)
    job = _running_job(queue, action="build")
    proc = _orch(
        "job", "run", "codex-bot", job.id, "--machines", str(LOCAL_BOX_MACHINES), "--db", str(tmp_path / "orch.db"),
        "--worktree", "--work-root", str(tmp_path / "work"), "--git-url-template", template,
        "--", "./harness", "exec",
        env={**GIT_ENV, "CODEX_HOME": str(home)},
        cwd=waiter,
        stdin=subprocess.DEVNULL,
    )
    _, stderr = proc.communicate(timeout=60)

    assert proc.returncode == 0, stderr
    with queue.connect() as connection:
        recorded = exposure.read(connection, job.id)
    assert recorded is not None, stderr
    assert (recorded.model, recorded.cli_version) == (exposure.UNKNOWN, "harness-cli 2.0.0")


USAGE = ROOT / "tests" / "fixtures" / "usage"
ROLLOUT_THREAD = "00000000-0000-4000-8000-000000000151"
ACCOUNT_HOME = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
ACCOUNT_TOKENS = {
    "claude": ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    "codex": ("OPENAI_API_KEY", "CODEX_API_KEY"),
}
ACCOUNT_HARNESS = f"""#!{sys.executable}
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
probe = "--input-format" in sys.argv
account = os.path.basename(os.environ.get("CLAUDE_CONFIG_DIR", "login"))
json.dump(dict(os.environ), open(os.path.join(here, f"probe-env-{{account}}.json" if probe else "env.json"), "w"))
if probe:
    sys.stdout.write(open({str(USAGE / "claude-2.1.322-get_usage.jsonl")!r}).read())
elif os.path.basename(__file__) == "codex":
    sys.stdout.write(open({str(USAGE / "codex-exec-json.jsonl")!r}).read())
"""


def _plant_rollout(home: Path, lines: int | None = None) -> None:
    day = home / "sessions" / "2026" / "11" / "14"
    day.mkdir(parents=True)
    rollout = (USAGE / "codex-rollout.jsonl").read_text(encoding="utf-8").splitlines(keepends=True)
    (day / f"rollout-2026-11-14T16-20-00-{ROLLOUT_THREAD}.jsonl").write_text(
        "".join(rollout[:lines]), encoding="utf-8"
    )


@pytest.mark.parametrize("account", ["placed", "default"])
@pytest.mark.parametrize("owner", ["claude", "codex"])
def test_cli_job_run_runs_a_box_job_as_its_slots_account_and_reads_every_account_on_the_box(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch, owner: str,
    account: str,
) -> None:
    bot = f"{owner}-bot"
    placed = f"{owner}-max-2"
    machines = tmp_path / "machines.yml"
    machines.write_text(
        "accounts:\n"
        "  - {id: claude-max-2, harness: claude, signed_in_on: [box]}\n"
        "  - {id: codex-max-2, harness: codex, signed_in_on: [box]}\n"
        "devices:\n"
        "  - name: box\n    local: true\n    slot_max: 3\n"
        f"    home: {tmp_path}/homes/{{bot}}\n"
        "    harnesses: {claude: claude-bot, codex: codex-bot}\n",
        encoding="utf-8",
    )
    account_home = tmp_path / "homes" / bot / ".factory-accounts" / placed
    login = tmp_path / "login"
    queue = _queue(tmp_path)
    assert usage_readings.migrate(
        queue.path, lambda: sqlite3.connect(queue.path), directory=tmp_path_factory.mktemp("manual")
    )
    job = _running_job(queue, recipient=owner, machines=machines)
    if account == "placed":
        with queue.connect() as connection:
            connection.execute("UPDATE device_slots SET account = ? WHERE job_id = ?", (placed, job.id))
    _plant_rollout(account_home)
    _plant_rollout(login / ".codex", lines=3)
    harness = tmp_path / "bin" / owner
    harness.parent.mkdir()
    harness.write_text(ACCOUNT_HARNESS, encoding="utf-8")
    harness.chmod(0o755)
    for name in ACCOUNT_HOME.values():
        monkeypatch.delenv(name, raising=False)
    planted = {name: f"planted-{name}" for names in ACCOUNT_TOKENS.values() for name in names}
    command = [str(harness), "-p"] if owner == "claude" else [str(harness), "exec", "-"]

    proc = _orch(
        "job", "run", bot, job.id, "--machines", str(machines), "--db", str(tmp_path / "orch.db"),
        "--interval", "0.2", "--", *command,
        env={"HOME": str(login), "ORCH_JOB_STATE_DIR": str(tmp_path / "state"), **planted},
    )
    _, stderr = proc.communicate(timeout=30)

    assert proc.returncode == 0, stderr
    as_side = [json.loads((harness.parent / "env.json").read_text())] if account == "placed" else []
    as_login = [] if account == "placed" else [json.loads((harness.parent / "env.json").read_text())]
    if owner == "claude":
        as_side.append(json.loads((harness.parent / f"probe-env-{placed}.json").read_text()))
        as_login.append(json.loads((harness.parent / "probe-env-login.json").read_text()))
    for env in as_side:
        assert env[ACCOUNT_HOME[owner]] == str(account_home)
        assert not set(ACCOUNT_TOKENS[owner]) & set(env)
    for env in as_login:
        assert ACCOUNT_HOME[owner] not in env
        assert {name: env[name] for name in ACCOUNT_TOKENS[owner]} == {
            name: planted[name] for name in ACCOUNT_TOKENS[owner]
        }
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        rows = connection.execute(
            "SELECT account, window, used FROM usage_account_readings ORDER BY account, window"
        ).fetchall()
        login_rows = connection.execute(
            "SELECT account, window, used FROM usage_readings ORDER BY window"
        ).fetchall()
    login_used = (0.125, 0.345) if owner == "claude" else (0.05, 0.2)
    assert rows == [
        (f"{owner}-default", "five_hour", login_used[0]), (f"{owner}-default", "seven_day", login_used[1]),
        (placed, "five_hour", 0.125), (placed, "seven_day", 0.345),
    ]
    assert login_rows == rows[:2], "only the device's login writes the old row"


CAPPED_ENV = {
    "ORCH_CPU_CAP": "4",
    "PYTEST_XDIST_AUTO_NUM_WORKERS": str(TEST_WORKERS),
    "VITEST_MAX_THREADS": str(TEST_WORKERS),
    "VITEST_MAX_FORKS": str(TEST_WORKERS),
    "VITEST_MIN_THREADS": "1",
    "VITEST_MIN_FORKS": "1",
}
CAP_NAMES = tuple(CAPPED_ENV)
LINUX_AFFINITY = pytest.mark.skipif(
    not hasattr(os, "sched_getaffinity"), reason="needs the Linux affinity API"
)
_GRANDCHILD = f"""\
import json, os, shutil, subprocess
seen = {{"cpus": len(os.sched_getaffinity(0)), "env": {{k: os.environ.get(k) for k in {CAP_NAMES!r}}}}}
node = shutil.which("node")
if node:
    seen["node"] = subprocess.run(
        [node, "-e", "const os = require('os'); console.log(os.availableParallelism ? os.availableParallelism() : 'none')"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
print(json.dumps(seen))
"""
_WORKER = f"""\
import json, os, subprocess, sys
grandchild = subprocess.run([sys.executable, "-c", {_GRANDCHILD!r}], capture_output=True, text=True, check=True)
json.dump({{"cpus": len(os.sched_getaffinity(0)), "grandchild": json.loads(grandchild.stdout)}}, open(sys.argv[1], "w"))
"""


@LINUX_AFFINITY
@pytest.mark.skipif(
    hasattr(os, "sched_getaffinity") and len(os.sched_getaffinity(0)) <= CPU_CAP,
    reason="the host has no more CPUs than the cap",
)
def test_cli_job_run_caps_the_worker_and_its_grandchildren_at_four_cpus(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    job = _running_job(queue)
    out = tmp_path / "seen.json"
    proc = _orch(
        "job", "run", "codex-bot", job.id, "--machines", str(LOCAL_BOX_MACHINES), "--db", str(tmp_path / "orch.db"),
        "--interval", "0.2", "--", sys.executable, "-c", _WORKER, str(out),
    )
    _, stderr = proc.communicate(timeout=30)

    assert proc.returncode == 0, stderr
    seen = json.loads(out.read_text())
    assert seen["cpus"] == CPU_CAP
    assert seen["grandchild"]["cpus"] == CPU_CAP
    assert seen["grandchild"]["env"] == CAPPED_ENV
    node = seen["grandchild"].get("node", "none")
    if node != "none":
        assert int(node) == CPU_CAP and int(node) - 1 <= 3


@LINUX_AFFINITY
def test_a_cap_of_one_is_set_before_the_gate_opens_and_reaches_a_grandchild(tmp_path: Path) -> None:
    out = tmp_path / "seen.json"
    at_gate: list[int] = []
    code = job_run.run(
        [sys.executable, "-c", _WORKER, str(out)],
        beat=lambda: None,
        interval=1,
        log=lambda _m: None,
        env={**os.environ, **cpu_cap_env(1)},
        started=lambda pid: at_gate.append(len(os.sched_getaffinity(pid))),
        cpu_cap=1,
    )

    assert code == 0
    assert at_gate == [1], "the worker pid was capped before started ran and go was written"
    seen = json.loads(out.read_text())
    assert seen["cpus"] == 1
    assert seen["grandchild"]["cpus"] == 1
    if seen["grandchild"].get("node", "none") != "none":
        assert seen["grandchild"]["node"] == "1"


def _fail_affinity(*_args) -> None:
    raise OSError(22, "Invalid argument")


@pytest.mark.parametrize("broken", ["fails", "missing"])
def test_a_missing_or_failing_affinity_call_is_logged_and_the_run_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broken: str
) -> None:
    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: set(range(64)), raising=False)
    if broken == "fails":
        monkeypatch.setattr(os, "sched_setaffinity", _fail_affinity, raising=False)
    else:
        monkeypatch.delattr(os, "sched_setaffinity", raising=False)
    out = tmp_path / "cap"
    logs: list[str] = []
    code = job_run.run(
        [sys.executable, "-c", f"import os; open({str(out)!r}, 'w').write(os.environ['ORCH_CPU_CAP'])"],
        beat=lambda: None,
        interval=1,
        log=logs.append,
        env={**os.environ, **cpu_cap_env()},
        started=lambda _pid: None,
        cpu_cap=CPU_CAP,
    )

    assert code == 0
    assert out.read_text() == "4"
    assert any("affinity path skipped" in line and "ORCH_CPU_CAP=4" in line for line in logs), logs
