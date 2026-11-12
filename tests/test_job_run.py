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
from lib.queue import JobRequest, QueueError, SQLiteJobQueue

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


def test_interval_defaults_to_a_third_of_the_job_timeout() -> None:
    assert job_run.heartbeat_interval(3600, None) == 1200
    assert job_run.heartbeat_interval(60, None) == 20
    assert job_run.heartbeat_interval(2, None) == job_run.MIN_INTERVAL_SECONDS
    assert job_run.heartbeat_interval(60, 5) == 5


@pytest.mark.parametrize("override", [0, -1, 21, float("nan")])
def test_interval_override_must_beat_within_a_third_of_the_timeout(override) -> None:
    with pytest.raises(ValueError):
        job_run.heartbeat_interval(60, override)


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
    job = queue.claim(recipient)
    assert job is not None
    return job


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


def test_cli_job_run_refuses_another_bots_job_and_a_slow_interval(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    job = _running_job(queue, timeout=60)
    database = str(tmp_path / "orch.db")

    other = _orch("job", "run", "claude-bot", job.id, "--db", database, "--", "true")
    _, stderr = other.communicate(timeout=20)
    assert other.returncode == 2
    assert "belongs to codex-bot" in stderr

    slow = _orch(
        "job", "run", "codex-bot", job.id, "--db", database, "--interval", "30", "--", "true"
    )
    _, stderr = slow.communicate(timeout=20)
    assert slow.returncode == 2
    assert "--interval" in stderr

    bare = _orch("job", "run", "codex-bot", job.id, "--db", database)
    _, stderr = bare.communicate(timeout=20)
    assert bare.returncode == 2
    assert "command" in stderr


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
