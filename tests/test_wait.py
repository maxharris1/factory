import ast
import io
import json
import os
import select
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lib.cli import main as cli_main
from lib.dispatcher import Comment, Dispatcher, TrackedTicket
from lib.factory_md import FactoryConfig
from lib.queue import JobRequest, SQLiteJobQueue
from lib.wait import (
    OWNING_BOTS,
    POLL_SECONDS,
    ExistingJobQueue,
    WaitError,
    open_queue,
    wait_for_job,
)

ROOT = Path(__file__).resolve().parents[1]
ORCH = ROOT / "bin" / "orch"
HARNESS_BINARIES = ("claude", "codex", "cursor-agent")
GO = "<!-- orch:v1 kind=go from=grok-bot role=orchestrator state=building -->\nGo.\n"
TRUSTED_LOGINS = {"grok-login": "grok-bot", "claude-login": "claude-bot"}


def make_db(tmp_path: Path) -> tuple[Path, SQLiteJobQueue]:
    path = tmp_path / "orch.db"
    return path, SQLiteJobQueue(path, clock=time.time)


def dispatcher_for(path: Path) -> Dispatcher:
    queue = SQLiteJobQueue(path, clock=time.time)
    return Dispatcher(path, queue, trusted_logins=TRUSTED_LOGINS)


def request(recipient: str = "claude", action: str = "build") -> JobRequest:
    return JobRequest(
        repo="owner/repo",
        ticket_id="42",
        action=action,
        recipient=recipient,
        source_comment_id="go-1",
        timeout_seconds=5400,
        head="3f9c2ab",
    )


def tracked(*comments: Comment) -> TrackedTicket:
    return TrackedTicket(
        repo="owner/repo",
        ticket_id="42",
        factory=FactoryConfig(
            base_branch="main",
            tickets="github",
            builder_harness="claude",
            reviewer_harness="codex",
            rounds_low=1,
            rounds_high=3,
            verify="none",
            ship="merge",
            skills=(),
        ),
        comments=comments,
        current_head=None,
        ci={},
        job_timeout_minutes=90,
    )


def run(args: list[str], db: Path | None = None, **env: str) -> subprocess.CompletedProcess[str]:
    extra = ["--db", str(db)] if db is not None else []
    return subprocess.run(
        [sys.executable, str(ORCH), *args, *extra],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
        env={**os.environ, **env},
    )


def start_wait(bot: str, db: Path, **env: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, str(ORCH), "wait", bot, "--db", str(db)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1", **env},
    )


def state_of(db: Path, job_id: str) -> sqlite3.Row:
    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        connection.close()
    assert row is not None
    return row


def wait_until_sleeping(proc: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out, err = proc.communicate()
            raise AssertionError(f"wait exited early ({proc.returncode}): {out!r} {err!r}")
        if _proc_state(proc.pid).startswith("S"):
            time.sleep(POLL_SECONDS * 1.5)
            if _proc_state(proc.pid).startswith("S"):
                return
        time.sleep(0.02)
    raise AssertionError("wait never blocked")


def test_wait_blocks_on_the_shell_until_the_dispatcher_queues_a_job(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    proc = start_wait("claude-bot", db)
    try:
        wait_until_sleeping(proc)
        assert proc.poll() is None
        assert not _readable(proc.stdout)
        assert _child_pids(proc.pid) == []
        assert _socket_fds(proc.pid) == []
        dispatcher_for(db).process(
            tracked(Comment(id="go-1", author="grok-login", body=GO, created_at=1))
        )
        stdout, stderr = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == 0, stderr
    assert stderr == ""
    job = json.loads(stdout)
    assert job["bot"] == "claude-bot"
    assert job["recipient"] == "claude"
    assert job["action"] == "build"
    assert job["repo"] == "owner/repo"
    assert job["ticket_id"] == "42"
    assert job["source_comment_id"] == "go-1"
    assert job["attempt"] == 1
    assert state_of(db, job["id"])["state"] == "running"


def test_wait_delivers_one_job_per_call_oldest_first(tmp_path: Path) -> None:
    db, queue = make_db(tmp_path)
    first = queue.enqueue(request(), dedupe_key="a")
    second = queue.enqueue(request(action="fix"), dedupe_key="b")
    queue.enqueue(request(recipient="codex", action="review"), dedupe_key="c")
    one = run(["wait", "claude-bot"], db)
    two = run(["wait", "claude-bot"], db)
    other = run(["wait", "codex-bot"], db)
    assert [json.loads(r.stdout)["id"] for r in (one, two)] == [first.id, second.id]
    assert json.loads(other.stdout)["action"] == "review"


def test_wait_reads_the_database_from_orch_db(tmp_path: Path) -> None:
    db, queue = make_db(tmp_path)
    job = queue.enqueue(request(), dedupe_key="a")
    result = run(["wait", "claude-bot"], ORCH_DB=str(db))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["id"] == job.id


def test_wait_without_a_database_is_refused(tmp_path: Path) -> None:
    env = {key: value for key, value in os.environ.items() if key != "ORCH_DB"}
    result = subprocess.run(
        [sys.executable, str(ORCH), "wait", "claude-bot"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 2
    assert "ORCH_DB" in result.stderr
    missing = run(["wait", "claude-bot"], tmp_path / "missing.db")
    assert missing.returncode == 2
    assert missing.stderr.startswith("orch wait: cannot open dispatcher database")
    assert not (tmp_path / "missing.db").exists()


# F1.1: done and heartbeat use F1-04's finish and heartbeat.


def test_job_done_finishes_with_the_handoff_comment_and_repeats_idempotently(
    tmp_path: Path,
) -> None:
    db, queue = make_db(tmp_path)
    job = queue.enqueue(request(), dedupe_key="a")
    unclaimed = run(["job", "done", "claude-bot", job.id, "--handoff", "c-9"], db)
    assert unclaimed.returncode == 1
    assert "running" in unclaimed.stderr
    run(["wait", "claude-bot"], db)
    done = run(["job", "done", "claude-bot", job.id, "--handoff", "c-9"], db)
    assert done.returncode == 0, done.stderr
    assert done.stdout == f"done {job.id}\n"
    row = state_of(db, job.id)
    assert (row["state"], row["handoff_comment_id"]) == ("done", "c-9")
    again = run(["job", "done", "claude-bot", job.id, "--handoff", "c-9"], db)
    assert again.returncode == 0, again.stderr
    other = run(["job", "done", "claude-bot", job.id, "--handoff", "c-10"], db)
    assert other.returncode == 1


def test_job_done_failed_hands_the_job_to_t6(tmp_path: Path) -> None:
    db, queue = make_db(tmp_path)
    job = queue.enqueue(request(), dedupe_key="a")
    run(["wait", "claude-bot"], db)
    failed = run(["job", "done", "claude-bot", job.id, "--failed"], db)
    assert failed.returncode == 0, failed.stderr
    assert failed.stdout == f"failed {job.id}\n"
    dispatcher_for(db).reconcile_jobs()
    retry = run(["wait", "claude-bot"], db)
    assert retry.returncode == 0, retry.stderr
    assert json.loads(retry.stdout)["attempt"] == 2


def test_job_done_requires_exactly_one_outcome(tmp_path: Path) -> None:
    db, queue = make_db(tmp_path)
    job = queue.enqueue(request(), dedupe_key="a")
    neither = run(["job", "done", "claude-bot", job.id], db)
    both = run(["job", "done", "claude-bot", job.id, "--handoff", "c", "--failed"], db)
    empty = run(["job", "done", "claude-bot", job.id, "--handoff", ""], db)
    assert (neither.returncode, both.returncode, empty.returncode) == (2, 2, 2)


def test_heartbeat_keeps_a_running_job_out_of_t6(tmp_path: Path) -> None:
    db, queue = make_db(tmp_path)
    kept = queue.enqueue(request(), dedupe_key="a")
    lapsed = queue.enqueue(request(action="fix"), dedupe_key="b")
    run(["wait", "claude-bot"], db)
    run(["wait", "claude-bot"], db)
    stale = time.time() - 5400 - 60
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE jobs SET last_activity_at = ?", (stale,))
    probe = SQLiteJobQueue(db, clock=time.time)
    assert all(
        probe.now() - job.last_activity_at >= job.request.timeout_seconds
        for job in probe.outstanding()
    )
    before = time.time()
    beat = run(["job", "heartbeat", "claude-bot", kept.id], db)
    assert beat.returncode == 0, beat.stderr
    assert beat.stdout == f"heartbeat {kept.id}\n"
    assert state_of(db, kept.id)["last_activity_at"] >= before
    assert state_of(db, lapsed.id)["last_activity_at"] == stale
    dispatcher_for(db).reconcile_jobs()
    assert state_of(db, kept.id)["state"] == "running"
    assert state_of(db, lapsed.id)["state"] == "failed"


def test_heartbeat_on_a_finished_job_fails(tmp_path: Path) -> None:
    db, queue = make_db(tmp_path)
    job = queue.enqueue(request(), dedupe_key="a")
    run(["wait", "claude-bot"], db)
    run(["job", "done", "claude-bot", job.id, "--handoff", "c-1"], db)
    beat = run(["job", "heartbeat", "claude-bot", job.id], db)
    assert beat.returncode == 1
    assert "running" in beat.stderr


# F1.3: a claimed job the bot never received goes straight to T6.


class Broken(io.StringIO):
    def write(self, _text: str) -> int:
        raise BrokenPipeError("bot went away")


def test_a_job_that_cannot_be_printed_is_failed_for_t6(
    tmp_path: Path, restore_signals: None
) -> None:
    db, queue = make_db(tmp_path)
    job = queue.enqueue(request(), dedupe_key="a")
    with pytest.raises(WaitError, match="failed it for T6"):
        wait_for_job(queue, "claude", Broken())
    assert state_of(db, job.id)["state"] == "failed"
    dispatcher_for(db).reconcile_jobs()
    out = io.StringIO()
    retried = wait_for_job(queue, "claude", out)
    assert retried.request.attempt == 2
    assert json.loads(out.getvalue())["id"] == retried.id


def test_sigterm_while_waiting_claims_nothing(tmp_path: Path) -> None:
    db, queue = make_db(tmp_path)
    proc = start_wait("claude-bot", db)
    try:
        wait_until_sleeping(proc)
        proc.send_signal(signal.SIGTERM)
        stdout, _ = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == 143
    assert stdout == ""
    job = queue.enqueue(request(), dedupe_key="a")
    assert state_of(db, job.id)["state"] == "queued"


# F1.4: bad storage is reported, and one bad row does not wedge other bots.


def test_a_corrupt_database_is_reported_without_a_traceback(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    db.write_bytes(b"not sqlite at all, just bytes" * 10)
    result = run(["wait", "claude-bot"], db)
    assert result.returncode == 2
    assert result.stderr.startswith("orch wait: cannot open dispatcher database")
    assert "Traceback" not in result.stderr
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    result = run(["wait", "claude-bot"], empty)
    assert result.returncode == 2
    assert "database has no jobs table" in result.stderr
    assert empty.read_bytes() == b""


def test_a_malformed_row_for_one_bot_does_not_block_another(tmp_path: Path) -> None:
    db, queue = make_db(tmp_path)
    bad = queue.enqueue(request(recipient="codex", action="review"), dedupe_key="bad")
    good = queue.enqueue(request(), dedupe_key="good")
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE jobs SET timeout_seconds = 'oops' WHERE id = ?",
            (bad.id,),
        )
    claude = run(["wait", "claude-bot"], db)
    assert claude.returncode == 0, claude.stderr
    assert json.loads(claude.stdout)["id"] == good.id
    codex = run(["wait", "codex-bot"], db)
    assert codex.returncode == 1
    assert codex.stderr.startswith("orch wait: claim failed")
    assert "Traceback" not in codex.stderr


# F1.6: a bounded poll, one claim per tick.


def test_wait_polls_once_per_second_with_one_claim_per_tick(
    tmp_path: Path, restore_signals: None
) -> None:
    assert POLL_SECONDS == 1.0
    _, real = make_db(tmp_path)
    real.enqueue(request(recipient="codex"), dedupe_key="a")
    calls: list[str] = []
    sleeps: list[float] = []

    class Queue:
        def claim(self, recipient: str):
            calls.append(recipient)
            return real.claim(recipient) if len(calls) == 4 else None

        def finish(self, job_id: str, *, owner: str, handoff_comment_id: str | None):
            raise AssertionError("delivered jobs are not failed")

    job = wait_for_job(Queue(), "codex", io.StringIO(), sleep=sleeps.append)
    assert job.request.recipient == "codex"
    assert calls == ["codex"] * 4
    assert sleeps == [POLL_SECONDS] * 3


# F1.8: only the three owning bots, and only for their own jobs.


@pytest.mark.parametrize("bot", ["claude", "reviewer", "grok-bot", "Claude-Bot", "claud-bot"])
def test_wait_refuses_anything_but_an_owning_bot(tmp_path: Path, bot: str) -> None:
    db, queue = make_db(tmp_path)
    queue.enqueue(request(), dedupe_key="a")
    result = run(["wait", bot], db)
    assert result.returncode == 2
    assert "unknown owning bot" in result.stderr
    assert result.stdout == ""


def test_owning_bots_match_the_dispatcher_recipients() -> None:
    assert OWNING_BOTS == {"claude-bot": "claude", "codex-bot": "codex", "cursor-bot": "cursor"}


def test_a_bot_cannot_finish_or_heartbeat_another_bots_job(tmp_path: Path) -> None:
    db, queue = make_db(tmp_path)
    job = queue.enqueue(request(), dedupe_key="a")
    run(["wait", "claude-bot"], db)
    done = run(["job", "done", "codex-bot", job.id, "--handoff", "c-1"], db)
    beat = run(["job", "heartbeat", "cursor-bot", job.id], db)
    unknown = run(["job", "done", "claude", job.id, "--handoff", "c-1"], db)
    assert (done.returncode, beat.returncode, unknown.returncode) == (2, 2, 2)
    assert "belongs to claude-bot" in done.stderr
    assert state_of(db, job.id)["state"] == "running"
    missing = run(["job", "done", "claude-bot", "0" * 32, "--handoff", "c-1"], db)
    assert missing.returncode == 2
    assert "unknown job" in missing.stderr


# orch never execs a harness binary.


def test_wait_and_job_never_exec_a_harness(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "harness-ran"
    for name in HARNESS_BINARIES:
        script = bin_dir / name
        script.write_text(f"#!/bin/sh\necho {name} >> {marker}\n", encoding="utf-8")
        script.chmod(0o755)
    path = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    db, _ = make_db(tmp_path)
    proc = start_wait("cursor-bot", db, PATH=path)
    try:
        wait_until_sleeping(proc)
        job = SQLiteJobQueue(db, clock=time.time).enqueue(
            request(recipient="cursor"), dedupe_key="a"
        )
        stdout, stderr = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == 0, stderr
    assert json.loads(stdout)["id"] == job.id
    assert run(["job", "heartbeat", "cursor-bot", job.id], db, PATH=path).returncode == 0
    assert run(["job", "done", "cursor-bot", job.id, "--handoff", "c"], db, PATH=path).returncode == 0
    assert not marker.exists()
    for name in ("wait.py", "cli.py"):
        assert _exec_calls(ROOT / "lib" / name) == []
    wait_imports = _imports(ROOT / "lib" / "wait.py")
    assert "subprocess" not in wait_imports
    assert "os" not in wait_imports


def test_open_queue_is_f1_04s_sqlite_queue(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    assert isinstance(open_queue(db), SQLiteJobQueue)
    with pytest.raises(WaitError):
        open_queue(tmp_path / "nope.db")
    assert not (ROOT / "lib" / "dispatcher_queue.py").exists()


@pytest.fixture
def restore_signals():
    saved = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
    yield
    for signum, handler in saved.items():
        signal.signal(signum, handler)
    signal.pthread_sigmask(signal.SIG_SETMASK, mask)


class SignalOnClaim:
    """Raises a signal at this process right after a real claim."""

    def __init__(self, queue: SQLiteJobQueue, signum: int):
        self.queue = queue
        self.signum = signum

    def claim(self, recipient: str):
        job = self.queue.claim(recipient)
        os.kill(os.getpid(), self.signum)
        return job

    def finish(self, job_id: str, *, owner: str, handoff_comment_id: str | None):
        return self.queue.finish(job_id, owner=owner, handoff_comment_id=handoff_comment_id)


# F2.2: signals cannot split a claim from its delivery.


class Interrupted(Exception):
    pass


def _raise_interrupted(*_args: object) -> None:
    raise Interrupted


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_a_signal_between_claim_and_print_still_delivers_the_job(
    tmp_path: Path, restore_signals: None, signum: int
) -> None:
    db, queue = make_db(tmp_path)
    job = queue.enqueue(request(), dedupe_key="a")
    signal.signal(signal.SIGTERM, _raise_interrupted)
    signal.signal(signal.SIGINT, _raise_interrupted)
    out = io.StringIO()
    delivered = wait_for_job(SignalOnClaim(queue, signum), "claude", out)
    assert delivered.id == job.id
    assert json.loads(out.getvalue())["id"] == job.id
    assert state_of(db, job.id)["state"] == "running"


def test_ctrl_c_while_printing_does_not_fail_a_delivered_job(
    tmp_path: Path, restore_signals: None
) -> None:
    db, queue = make_db(tmp_path)
    job = queue.enqueue(request(), dedupe_key="a")

    class InterruptingOut(io.StringIO):
        def write(self, text: str) -> int:
            os.kill(os.getpid(), signal.SIGINT)
            return super().write(text)

    signal.signal(signal.SIGINT, _raise_interrupted)
    out = InterruptingOut()
    assert wait_for_job(queue, "claude", out).id == job.id
    assert json.loads(out.getvalue())["id"] == job.id
    assert state_of(db, job.id)["state"] == "running"


def test_signals_are_unblocked_while_sleeping(tmp_path: Path, restore_signals: None) -> None:
    _, queue = make_db(tmp_path)
    masks: list[set[int]] = []

    def sleep(_seconds: float) -> None:
        masks.append(set(signal.pthread_sigmask(signal.SIG_BLOCK, [])))
        if len(masks) == 2:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        wait_for_job(queue, "claude", io.StringIO(), sleep=sleep)
    assert all(signal.SIGINT not in mask and signal.SIGTERM not in mask for mask in masks)


def test_a_signal_during_cli_delivery_exits_zero_without_a_traceback(tmp_path: Path) -> None:
    db, queue = make_db(tmp_path)
    job = queue.enqueue(request(), dedupe_key="a")
    hook = tmp_path / "hook"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text(
        "import os, signal, lib.wait as w\n"
        "real = w.deliver\n"
        "def deliver(queue, job, out):\n"
        "    os.kill(os.getpid(), signal.SIGINT)\n"
        "    os.kill(os.getpid(), signal.SIGTERM)\n"
        "    real(queue, job, out)\n"
        "w.deliver = deliver\n",
        encoding="utf-8",
    )
    result = run(
        ["wait", "claude-bot"],
        db,
        PYTHONPATH=f"{hook}{os.pathsep}{ROOT}",
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout)["id"] == job.id
    assert state_of(db, job.id)["state"] == "running"


# F2.3: storage errors are one line, never a traceback.


def test_an_os_error_on_the_db_path_is_one_line(tmp_path: Path) -> None:
    long_path = tmp_path / ("x" * 300) / "orch.db"
    result = run(["wait", "claude-bot"], long_path)
    assert result.returncode == 2
    assert result.stderr.startswith("orch wait: cannot open dispatcher database")
    assert result.stderr.count("\n") == 1
    blocked = tmp_path / "file"
    blocked.write_text("", encoding="utf-8")
    result = run(["job", "heartbeat", "claude-bot", "0" * 32], blocked / "orch.db")
    assert result.returncode == 2
    assert result.stderr.startswith("orch job heartbeat: cannot open dispatcher database")
    assert result.stderr.count("\n") == 1


def test_a_storage_error_reading_the_job_owner_is_one_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db, queue = make_db(tmp_path)
    job = queue.enqueue(request(), dedupe_key="a")

    def locked(self: SQLiteJobQueue) -> sqlite3.Connection:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ExistingJobQueue, "connect", locked)
    assert cli_main(["job", "done", "claude-bot", job.id, "--handoff", "c", "--db", str(db)]) == 2
    err = capsys.readouterr().err
    assert err == f"orch job done: cannot read job {job.id}: database is locked\n"


def test_a_finish_failure_after_a_failed_delivery_is_one_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    restore_signals: None,
) -> None:
    db, queue = make_db(tmp_path)
    job = queue.enqueue(request(), dedupe_key="a")

    def finish(
        self: SQLiteJobQueue, job_id: str, *, owner: str, handoff_comment_id: str | None
    ):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ExistingJobQueue, "finish", finish)
    monkeypatch.setattr(sys, "stdout", Broken())
    assert cli_main(["wait", "claude-bot", "--db", str(db)]) == 1
    err = capsys.readouterr().err
    assert err.startswith(f"orch wait: could not deliver job {job.id} (bot went away) or fail it")
    assert err.endswith("T6 will time it out\n")
    assert err.count("\n") == 1
    assert state_of(db, job.id)["state"] == "running"


def test_a_broken_stdout_in_the_cli_exits_one_without_a_traceback(tmp_path: Path) -> None:
    db, queue = make_db(tmp_path)
    job = queue.enqueue(request(), dedupe_key="a")
    read_end, write_end = os.pipe()
    os.close(read_end)
    try:
        result = subprocess.run(
            [sys.executable, str(ORCH), "wait", "claude-bot", "--db", str(db)],
            cwd=ROOT,
            stdout=write_end,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
    finally:
        os.close(write_end)
    assert result.returncode == 1
    assert result.stderr.startswith(f"orch wait: could not deliver job {job.id}")
    assert "Traceback" not in result.stderr
    assert result.stderr.count("\n") == 1
    assert state_of(db, job.id)["state"] == "failed"


# F2.4: never create a database or a jobs table.


def test_an_unrelated_database_is_refused_and_left_untouched(tmp_path: Path) -> None:
    other = tmp_path / "other.db"
    with sqlite3.connect(other) as connection:
        connection.execute("CREATE TABLE notes (body TEXT)")
    connection.close()
    result = run(["wait", "claude-bot"], other)
    assert result.returncode == 2
    assert "database has no jobs table" in result.stderr
    with sqlite3.connect(other) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    connection.close()
    assert tables == {"notes"}


def test_a_jobs_table_with_the_wrong_schema_is_refused(tmp_path: Path) -> None:
    other = tmp_path / "other.db"
    with sqlite3.connect(other) as connection:
        connection.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, state TEXT)")
    connection.close()
    result = run(["job", "heartbeat", "claude-bot", "0" * 32], other)
    assert result.returncode == 2
    assert "jobs table is missing" in result.stderr
    assert "recipient" in result.stderr


def test_a_database_removed_while_waiting_is_not_recreated(
    tmp_path: Path, restore_signals: None
) -> None:
    db, _ = make_db(tmp_path)
    queue = open_queue(db)
    for path in tmp_path.glob("orch.db*"):
        path.unlink()

    def sleep(_seconds: float) -> None:
        raise AssertionError("claim should fail, not sleep")

    with pytest.raises(WaitError, match="claim failed"):
        wait_for_job(queue, "claude", io.StringIO(), sleep=sleep)
    assert not db.exists()


def test_every_waiter_connection_is_read_write_only(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    queue = open_queue(db)
    db.unlink()
    with pytest.raises(sqlite3.OperationalError):
        queue.connect()
    assert not db.exists()


# F2.7: competing waiters.


def test_two_waiters_one_job_exactly_one_claims_it(tmp_path: Path) -> None:
    db, queue = make_db(tmp_path)
    first = start_wait("claude-bot", db)
    second = start_wait("claude-bot", db)
    procs = [first, second]
    try:
        for proc in procs:
            wait_until_sleeping(proc)
        job = queue.enqueue(request(), dedupe_key="a")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and all(p.poll() is None for p in procs):
            time.sleep(0.05)
        time.sleep(POLL_SECONDS * 2.5)
        finished = [p for p in procs if p.poll() is not None]
        waiting = [p for p in procs if p.poll() is None]
        assert len(finished) == 1 and len(waiting) == 1
        stdout, stderr = finished[0].communicate(timeout=5)
        assert finished[0].returncode == 0, stderr
        assert json.loads(stdout)["id"] == job.id
        assert _proc_state(waiting[0].pid).startswith("S")
        assert not _readable(waiting[0].stdout)
        later = queue.enqueue(request(action="fix"), dedupe_key="b")
        stdout, stderr = waiting[0].communicate(timeout=10)
        assert waiting[0].returncode == 0, stderr
        assert json.loads(stdout)["id"] == later.id
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()


def test_three_handoff_skills_document_wait_heartbeat_and_done() -> None:
    harness = {"claude-bot": "claude", "codex-bot": "codex", "cursor-bot": "cursor-agent"}
    for bot, binary in harness.items():
        text = (ROOT / "bots" / bot / "SKILL.md").read_text(encoding="utf-8")
        _, _, body = text.partition("## orch jobs")
        paragraph = body.strip()
        assert paragraph
        assert "\n\n" not in paragraph
        assert "\n#" not in paragraph
        assert f"orch wait {bot}" in paragraph
        assert f"orch job heartbeat {bot}" in paragraph
        assert f"orch job done {bot} <id> --handoff" in paragraph
        assert f"orch job done {bot} <id> --failed" in paragraph
        assert f"`{binary}` harness" in paragraph
        assert "no model tokens" in paragraph
        assert "never starts a harness" in paragraph
        assert "at least once every third of the job's `timeout_seconds`" in paragraph
        assert "every few minutes" not in paragraph


def _readable(stream: object) -> bool:
    ready, _, _ = select.select([stream], [], [], 0)
    return bool(ready)


def _proc_state(pid: int) -> str:
    for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
        if line.startswith("State:"):
            return line.split(":", 1)[1].strip()
    return ""


def _child_pids(pid: int) -> list[str]:
    return Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="utf-8").split()


def _socket_fds(pid: int) -> list[str]:
    found: list[str] = []
    for entry in Path(f"/proc/{pid}/fd").iterdir():
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        if target.startswith("socket:"):
            found.append(target)
    return found


def _imports(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module.split(".", 1)[0])
    return modules


def _exec_calls(path: Path) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
            continue
        name = f"{node.value.id}.{node.attr}"
        if node.value.id == "subprocess" or (
            node.value.id == "os"
            and (node.attr in ("system", "popen", "fork") or node.attr.startswith(("exec", "spawn")))
        ):
            found.append(name)
    return found
