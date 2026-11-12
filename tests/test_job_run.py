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
from tests.pool_helpers import pool_claim

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
        "job", "run", "codex-bot", job.id, "--db", str(tmp_path / "orch.db"),
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
    script = (
        "import subprocess, sys, time; "
        f"subprocess.run([sys.executable, {str(ORCH)!r}, 'job', 'done', 'codex-bot', "
        f"{job.id!r}, '--handoff', '99', '--db', {str(database)!r}], check=True); "
        "time.sleep(0.8)"
    )
    proc = _orch(
        "job", "run", "codex-bot", job.id, "--db", str(database),
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
        "job", "run", "codex-bot", job.id, "--db", str(tmp_path / "orch.db"),
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
        "job", "run", "codex-bot", job.id, "--db", str(tmp_path / "orch.db"),
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
        "job", "run", "codex-bot", job.id, "--db", str(tmp_path / "orch.db"),
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
        "job", "run", "claude-bot", job.id, "--db", database,
        "--", sys.executable, "-c", f"open({str(marker)!r}, 'w')",
    )
    _, stderr = other.communicate(timeout=20)
    assert other.returncode == 2
    assert "belongs to codex-bot" in stderr
    assert not marker.exists()

    zero = _orch(
        "job", "run", "codex-bot", job.id, "--db", database, "--interval", "0", "--", "true"
    )
    _, stderr = zero.communicate(timeout=20)
    assert zero.returncode == 2
    assert "--interval" in stderr

    bare = _orch("job", "run", "codex-bot", job.id, "--db", database)
    _, stderr = bare.communicate(timeout=20)
    assert bare.returncode == 2
    assert "command" in stderr


def test_cli_job_run_caps_a_slow_interval_and_says_so(tmp_path: Path) -> None:
    # Factory request on PR #77: a slow override is capped (here at a third
    # of the 60 s timeout), not refused, and the cap is logged.
    queue = _queue(tmp_path)
    job = _running_job(queue, timeout=60)
    slow = _orch(
        "job", "run", "codex-bot", job.id, "--db", str(tmp_path / "orch.db"),
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
        "job", "run", "codex-bot", job.id, "--db", str(tmp_path / "orch.db"),
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
        "job", "run", "codex-bot", job.id, "--db", str(tmp_path / "orch.db"),
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
