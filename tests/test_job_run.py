import fcntl
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from lib import job_run
from lib.job_state import JobStateConfig
from lib.queue import JobRequest, QueueError, SQLiteJobQueue
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


def _running_job(queue: SQLiteJobQueue, *, timeout: int = 60, recipient: str = "codex"):
    queue.enqueue(
        JobRequest(
            repo="acme/widgets",
            ticket_id="12",
            action="review",
            recipient=recipient,
            source_comment_id="1",
            timeout_seconds=timeout,
        ),
        dedupe_key=f"k-{time.monotonic_ns()}",
    )
    return pool_claim(queue, recipient)


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
        f"json.dump({{k: os.environ.get(k) for k in ('ORCH_JOB_ID', 'ORCH_JOB_BOT', 'ORCH_DB')}}, open({str(out)!r}, 'w')); "
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
