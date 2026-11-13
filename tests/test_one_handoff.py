"""Exactly one verdict or handoff per job (#80, Max via Factory on PR #82).

Once an autostart waiter has claimed a job, its harness run is the only
thing that runs or finishes that job:

* a second claim is refused: a plain `orch wait` for the bot is refused
  while the bot's autostart waiter runs, and a running job is never handed
  out again;
* a second `orch job run` of a running job is refused, and a bot turn's
  `orch job run` of an autostart job is refused, nothing started;
* `orch job done` is idempotent (the same outcome again is a no-op success),
  a conflicting second call is refused without changing state, and a bot
  turn outside the autostart run cannot finish the job at all;
* a duplicate verdict comment at the same head triggers no second action:
  no second job, no second ship, no wake.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lib import waiter as waiter_module
from lib.dispatcher import CIResult
from lib.queue import SQLiteJobQueue
from tests.test_dispatcher import (
    VERDICT,
    FakeReviewActions,
    comment,
    seed_review_job,
    setup,
    ticket,
)
from tests.test_waiter import (  # noqa: F401  (autostart is a fixture)
    MACHINES,
    ORCH,
    _running,
    _wait_for,
    autostart,
    enqueue,
    local_pool,
    make_queue,
    state,
)


def orch(
    *args: str,
    cwd: Path | None = None,
    extra_env: dict[str, str] | None = None,
    prefix: tuple[str, ...] = (),
) -> subprocess.CompletedProcess:
    env = {
        k: v for k, v in os.environ.items()
        if k not in ("ORCH_DB", "ORCH_AUTOSTART", "ORCH_JOB_PROOF")
    }
    env.update(extra_env or {})
    return subprocess.run(
        [*prefix, sys.executable, str(ORCH), *args],
        env=env, cwd=cwd, text=True, capture_output=True, timeout=60, check=False,
    )


def job_row(db: Path, job_id: str) -> tuple[str, str | None]:
    with sqlite3.connect(db) as connection:
        return connection.execute(
            "SELECT state, handoff_comment_id FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()


def claimed(tmp_path: Path) -> tuple[Path, SQLiteJobQueue, str]:
    """A job a plain (non-autostart) claim took through the device pool."""
    db, queue = make_queue(tmp_path)
    job_id = enqueue(queue, "a")
    taken = local_pool(queue).claim("claude")
    assert taken is not None and taken[0].id == job_id
    return db, queue, job_id


# --- a second claim is refused -----------------------------------------------


def test_plain_wait_is_refused_while_the_bots_autostart_waiter_runs(autostart: dict) -> None:
    proc, db, queue = autostart["proc"], autostart["db"], autostart["queue"]
    lock = waiter_module.lock_path(db, "claude-bot")
    _wait_for(lambda: lock.exists() and lock.read_text().strip() == str(proc.pid), 20, proc)
    queued = enqueue(queue, "plain", recipient="claude", head=None)
    # Let the autostart waiter take it first, then a bot turn tries to claim.
    _wait_for(lambda: _running(db, queued), 20, proc)
    result = orch("wait", "claude-bot", "--db", str(db), "--machines", str(MACHINES))
    assert result.returncode == 2
    assert f"claude-bot's autostart waiter (pid {proc.pid})" in result.stderr
    assert "second claim is refused" in result.stderr
    assert result.stdout == ""


def test_a_running_job_is_never_claimed_again(tmp_path: Path) -> None:
    db, queue, job_id = claimed(tmp_path)
    assert local_pool(queue).claim("claude") is None
    assert queue.claim("claude") is None
    assert state(db, job_id) == "running"


def test_an_autostart_waiter_will_not_start_beside_a_plain_wait(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    plain = waiter_module.share_waiter_lock(db, "claude-bot")
    try:
        with pytest.raises(waiter_module.WaiterError, match="a plain orch wait for claude-bot"):
            waiter_module.hold_waiter_lock(db, "claude-bot")
    finally:
        os.close(plain)


# --- a second run is refused ---------------------------------------------------


def test_a_bot_turn_cannot_run_a_job_the_autostart_waiter_claimed(
    autostart: dict, tmp_path: Path
) -> None:
    proc, db, queue = autostart["proc"], autostart["db"], autostart["queue"]
    job_id = enqueue(queue, "a", head=None)
    _wait_for(lambda: _running(db, job_id), 20, proc)
    marker = tmp_path / "bot-turn-ran"
    result = orch("job", "run", "claude-bot", job_id, "--db", str(db),
                  "--machines", str(MACHINES), "--", "touch", str(marker))
    assert result.returncode == 2
    assert "was claimed by claude-bot's autostart waiter" in result.stderr
    assert "nothing was started" in result.stderr
    time.sleep(0.3)
    assert not marker.exists()
    assert state(db, job_id) == "running"
    # The harness run still finishes it, once.
    (autostart["harness_dir"] / f"{job_id}.go").touch()
    _wait_for(lambda: state(db, job_id) == "done", 20, proc)


def _dead_waiter() -> tuple[int, str | None]:
    """A process that was the claiming waiter and has exited."""
    from lib.worktrees import process_start

    gone = subprocess.Popen(["sleep", "30"])
    start = process_start(gone.pid)
    gone.kill()
    gone.wait()
    return gone.pid, start


def test_r2_f2_2_a_run_orphaned_before_prepare_leaves_the_claim_releasable(
    tmp_path: Path,
) -> None:
    """Codex PR #82 r2 F2.2: the waiter exits between spawn and `prepare`.

    The reparented `orch job run` must be refused before its first
    heartbeat, so the job stays claimed (no heartbeat since the claim, no
    runner) and the poller's claimed-job release requeues it at once rather
    than after the full timeout.
    """
    from lib.dispatcher import Dispatcher
    from lib.job_state import CLAIMED, JobStateConfig, derive
    from tests.test_wait import TRUSTED_LOGINS
    from unittest.mock import Mock

    db, queue = make_queue(tmp_path)
    job_id = enqueue(queue, "a")
    pool = local_pool(queue)
    taken = pool.claim("claude", autostart=_dead_waiter())
    assert taken is not None and taken[0].id == job_id
    with sqlite3.connect(db) as connection:
        before = connection.execute(
            "SELECT last_activity_at FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()[0]

    marker = tmp_path / "ran"
    result = orch("job", "run", "claude-bot", job_id, "--db", str(db),
                  "--machines", str(MACHINES), "--", "touch", str(marker))
    assert result.returncode == 2, result.stderr
    assert "autostart waiter" in result.stderr and "nothing was started" in result.stderr
    assert not marker.exists()

    with sqlite3.connect(db) as connection:
        after = connection.execute(
            "SELECT state, last_activity_at FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    assert after == ("running", before)  # no heartbeat was written
    placement = pool.placement(job_id)
    assert placement is not None and placement.runner_pid is None
    assert derive(
        last_activity_at=before, now=time.time(), placement=placement, stale_seconds=120
    ) == CLAIMED

    dispatcher = Dispatcher(
        db, SQLiteJobQueue(db, clock=time.time), trusted_logins=TRUSTED_LOGINS,
        review_actions=Mock(), grok_delivery=Mock(deliver=Mock(return_value="d")),
        job_states=JobStateConfig(release_claimed_seconds=0.0),
    )
    dispatcher.reconcile_jobs()
    assert state(db, job_id) == "queued"  # the same job row, back in line
    assert pool.placement(job_id) is None
    again = pool.claim("claude")
    assert again is not None and again[0].id == job_id


def test_r2_f2_2_a_run_checked_before_its_waiter_exited_still_attaches(tmp_path: Path) -> None:
    """Checked while the waiter lived, reparented before `prepare`: it runs.

    prepare does not look at the parent again; it only requires that the
    slot still names the waiter the check saw.
    """
    from lib import placed_run
    from tests.test_remote_start import FakeTransport

    db, queue = make_queue(tmp_path)
    job_id = enqueue(queue, "a")
    pool = local_pool(queue)
    waiter_pid, waiter_start = os.getpid(), waiter_module.process_start(os.getpid())
    pool.claim("claude", autostart=(waiter_pid, waiter_start))
    checked = placed_run.check_autostart(
        queue, job_id=job_id, bot="claude-bot", parent_pid=waiter_pid
    )
    assert checked == (waiter_pid, waiter_start)
    from tests.test_waiter import load_machines_yml, local_only

    machines, _ = load_machines_yml(MACHINES, allow_placeholders=True)
    assert machines is not None
    placed_run.prepare(
        pool, local_only(machines), job_id=job_id, bot="claude-bot", owner="claude",
        worktree=False, log=[].append, transport=FakeTransport(), pid=os.getpid(),
        parent_pid=1, checked_autostart=checked,
    )
    placement = pool.placement(job_id)
    assert placement is not None and placement.runner_pid == os.getpid()

    # A claim made again by another waiter after the check is refused.
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE device_slots SET autostart_pid = ?, runner_pid = NULL WHERE job_id = ?",
            (waiter_pid + 1, job_id),
        )
    with pytest.raises(placed_run.PlacementError, match="claimed again"):
        placed_run.prepare(
            pool, local_only(machines), job_id=job_id, bot="claude-bot", owner="claude",
            worktree=False, log=[].append, transport=FakeTransport(), pid=os.getpid(),
            parent_pid=waiter_pid, checked_autostart=checked,
        )


def test_a_second_run_of_a_running_job_is_refused(tmp_path: Path) -> None:
    db, _queue, job_id = claimed(tmp_path)
    first = subprocess.Popen(["sleep", "30"])
    try:
        from lib.worktrees import process_start

        with sqlite3.connect(db) as connection:
            connection.execute(
                "UPDATE device_slots SET runner_pid = ?, runner_start = ? WHERE job_id = ?",
                (first.pid, process_start(first.pid), job_id),
            )
        marker = tmp_path / "second-ran"
        result = orch("job", "run", "claude-bot", job_id, "--db", str(db),
                      "--machines", str(MACHINES), "--", "touch", str(marker))
        assert result.returncode == 2
        assert f"already run by pid {first.pid}" in result.stderr
        assert "second run of a running job is refused" in result.stderr
        assert not marker.exists()
        assert state(db, job_id) == "running"
    finally:
        first.kill()
        first.wait()


# --- orch job done is idempotent, and only the job's run may call it ----------


def test_job_done_with_the_same_handoff_again_is_a_no_op_success(tmp_path: Path) -> None:
    db, _queue, job_id = claimed(tmp_path)
    first = orch("job", "done", "claude-bot", job_id, "--db", str(db), "--handoff", "c-1")
    assert first.returncode == 0, first.stderr
    assert first.stdout.strip() == f"done {job_id}"
    again = orch("job", "done", "claude-bot", job_id, "--db", str(db), "--handoff", "c-1")
    assert again.returncode == 0, again.stderr
    assert again.stdout.startswith(f"done {job_id}")
    assert "nothing changed" in again.stdout
    assert job_row(db, job_id) == ("done", "c-1")


@pytest.mark.parametrize("second", [["--handoff", "c-2"], ["--failed"]])
def test_a_conflicting_second_job_done_is_refused_and_changes_nothing(
    tmp_path: Path, second: list[str]
) -> None:
    db, _queue, job_id = claimed(tmp_path)
    assert orch("job", "done", "claude-bot", job_id, "--db", str(db),
                "--handoff", "c-1").returncode == 0
    with sqlite3.connect(db) as connection:
        before = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    result = orch("job", "done", "claude-bot", job_id, "--db", str(db), *second)
    assert result.returncode == 1
    assert f"job {job_id} is already done with handoff c-1" in result.stderr
    assert "nothing changed" in result.stderr
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone() == before


def test_a_repeated_failed_is_a_no_op_and_a_handoff_after_it_is_refused(tmp_path: Path) -> None:
    db, _queue, job_id = claimed(tmp_path)
    assert orch("job", "done", "claude-bot", job_id, "--db", str(db), "--failed").returncode == 0
    again = orch("job", "done", "claude-bot", job_id, "--db", str(db), "--failed")
    assert again.returncode == 0 and "nothing changed" in again.stdout
    late = orch("job", "done", "claude-bot", job_id, "--db", str(db), "--handoff", "c-9")
    assert late.returncode == 1
    assert "already failed" in late.stderr
    assert job_row(db, job_id) == ("failed", None)


# Codex PR #82 r2 F2.4: the slots table as round 1 (origin/main) left it,
# before the autostart columns; `orch job done` never opens a DevicePool, so
# nothing migrates it on this path.
ROUND_1_SLOTS = """
CREATE TABLE device_slots (
    job_id TEXT PRIMARY KEY,
    device TEXT NOT NULL,
    harness TEXT NOT NULL,
    os_user TEXT NOT NULL,
    placed_at REAL NOT NULL,
    runner_pid INTEGER,
    runner_start TEXT,
    worker_pid INTEGER,
    worker_start TEXT,
    remote_started INTEGER NOT NULL DEFAULT 0,
    worker_tag TEXT
)
"""


@pytest.mark.parametrize("outcome", [["--handoff", "c-1"], ["--failed"]])
def test_job_done_tolerates_a_round_1_slots_table(tmp_path: Path, outcome: list[str]) -> None:
    db, queue = make_queue(tmp_path)
    job_id = enqueue(queue, "a")
    taken = queue.claim("claude")
    assert taken is not None and taken.id == job_id
    with sqlite3.connect(db) as connection:
        connection.execute(ROUND_1_SLOTS)
        connection.execute(
            "INSERT INTO device_slots (job_id, device, harness, os_user, placed_at, "
            "runner_pid, runner_start) VALUES (?, 'box', 'claude', 'claude-bot', ?, ?, NULL)",
            (job_id, time.time(), os.getpid()),
        )
    result = orch("job", "done", "claude-bot", job_id, "--db", str(db), *outcome)
    assert result.returncode == 0, result.stderr
    expected = ("done", "c-1") if outcome[0] == "--handoff" else ("failed", None)
    assert job_row(db, job_id) == expected
    with sqlite3.connect(db) as connection:  # read only: nothing was migrated
        columns = {row[1] for row in connection.execute("PRAGMA table_info(device_slots)")}
    assert "autostart_pid" not in columns
    again = orch("job", "done", "claude-bot", job_id, "--db", str(db), *outcome)
    assert again.returncode == 0 and "nothing changed" in again.stdout


@pytest.mark.parametrize("outcome", [["--handoff", "c-bot-turn"], ["--failed"]])
def test_a_bot_turn_cannot_finish_a_job_the_autostart_waiter_runs(
    autostart: dict, outcome: list[str]
) -> None:
    proc, db, queue = autostart["proc"], autostart["db"], autostart["queue"]
    job_id = enqueue(queue, "a", head=None)
    _wait_for(lambda: _running(db, job_id), 20, proc)
    result = orch("job", "done", "claude-bot", job_id, "--db", str(db), *outcome)
    assert result.returncode == 2
    assert "claimed by its autostart waiter" in result.stderr
    assert "this process has no ORCH_JOB_PROOF" in result.stderr
    assert "Nothing changed" in result.stderr
    assert job_row(db, job_id) == ("running", None)
    # The harness inside the run finishes it with its own handoff.
    (autostart["harness_dir"] / f"{job_id}.go").touch()
    _wait_for(lambda: state(db, job_id) == "done", 20, proc)
    assert job_row(db, job_id) == ("done", "c-" + job_id[:6])
    # Afterwards the same handoff again is a no-op; a bot turn's is refused.
    same = orch("job", "done", "claude-bot", job_id, "--db", str(db),
                "--handoff", "c-" + job_id[:6])
    assert same.returncode == 0 and "nothing changed" in same.stdout
    other = orch("job", "done", "claude-bot", job_id, "--db", str(db), "--handoff", "c-other")
    assert other.returncode == 1
    assert job_row(db, job_id) == ("done", "c-" + job_id[:6])


# --- #96: the run's token, not its process tree, proves it is the run ---------


def _harness_record(autostart: dict, job_id: str) -> dict:
    path = autostart["harness_dir"] / f"{job_id}.json"
    return json.loads(_wait_for(lambda: path.exists() and path.read_text(), 20, autostart["proc"]))


def _slot(db: Path, job_id: str) -> sqlite3.Row:
    with sqlite3.connect(db) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            "SELECT * FROM device_slots WHERE job_id = ?", (job_id,)
        ).fetchone()


def _released(db: Path, job_id: str) -> bool:
    return _slot(db, job_id) is None


def _new_pid_namespace() -> tuple[str, ...]:
    """`unshare` into a new user and PID namespace, as Codex's sandbox does."""
    prefix = ("unshare", "--user", "--map-root-user", "--pid", "--fork", "--mount-proc")
    try:
        probe = subprocess.run([*prefix, "sh", "-c", "echo $$"], capture_output=True,
                               text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("unshare is not available")
    if probe.returncode != 0 or probe.stdout.strip() != "1":
        pytest.skip("this host cannot make an unprivileged PID namespace")
    return prefix


PROOF = "ORCH_JOB_PROOF"


def _proof(record: dict) -> str:
    return record["env"][PROOF]


def _codex_shell_env(env: dict[str, str]) -> dict[str, str]:
    """What Codex's default shell_environment_policy passes on: no *KEY*, *SECRET*, *TOKEN*."""
    return {
        name: value for name, value in env.items()
        if not any(word in name.upper() for word in ("KEY", "SECRET", "TOKEN"))
    }


@pytest.mark.parametrize("outcome", [["--handoff", "c-sandbox"], ["--failed"]])
@pytest.mark.parametrize("proof", ["env", "codex-filtered-env", "pid-namespace"])
def test_the_run_finishes_its_job_with_its_token_whatever_the_process_tree(
    autostart: dict, outcome: list[str], proof: str
) -> None:
    from lib.worktrees import descends_from

    proc, db, queue = autostart["proc"], autostart["db"], autostart["queue"]
    job_id = enqueue(queue, "a", head=None)
    _wait_for(lambda: _running(db, job_id), 20, proc)
    record = _harness_record(autostart, job_id)
    assert _proof(record)
    # This test process is not in the run's process tree, nor is its child.
    assert not descends_from(os.getpid(), int(_slot(db, job_id)["runner_pid"]))
    extra, prefix = {PROOF: _proof(record)}, ()
    if proof == "codex-filtered-env":
        extra = _codex_shell_env(record["env"])
        assert extra[PROOF] == _proof(record)
    elif proof == "pid-namespace":
        prefix = _new_pid_namespace()
    result = orch("job", "done", "claude-bot", job_id, "--db", str(db), *outcome,
                  extra_env=extra, prefix=prefix)
    assert result.returncode == 0, result.stderr
    expected = ("done", "c-sandbox") if outcome[0] == "--handoff" else ("failed", None)
    assert job_row(db, job_id) == expected
    (autostart["harness_dir"] / f"{job_id}.crash").touch()
    _wait_for(lambda: _released(db, job_id), 20, proc)
    assert job_row(db, job_id) == expected


@pytest.mark.parametrize("outcome", [["--handoff", "c-sibling"], ["--failed"]])
def test_a_sibling_session_without_the_runs_environment_cannot_finish_the_job(
    autostart: dict, tmp_path: Path, outcome: list[str]
) -> None:
    """A same-user session that was not handed the token finds nothing on disk to present."""
    proc, db, queue = autostart["proc"], autostart["db"], autostart["queue"]
    job_id = enqueue(queue, "a", head=None)
    _wait_for(lambda: _running(db, job_id), 20, proc)
    record = _harness_record(autostart, job_id)
    token = _proof(record)
    job_dir = Path(record["brief_env"]).parent
    found = sorted(
        path for path in tmp_path.rglob("*")
        if path.is_file() and autostart["harness_dir"] not in path.parents
        and token.encode() in path.read_bytes()
    )
    assert found == []
    for path in sorted(job_dir.iterdir()):
        for name in ("ORCH_JOB_PROOF_FILE", PROOF):
            value = str(path) if name.endswith("_FILE") else path.read_text(errors="replace")
            result = orch("job", "done", "claude-bot", job_id, "--db", str(db), *outcome,
                          extra_env={name: value})
            assert result.returncode == 2, (path, name, result.stdout)
            assert job_row(db, job_id) == ("running", None)
    (autostart["harness_dir"] / f"{job_id}.go").touch()
    _wait_for(lambda: state(db, job_id) == "done", 20, proc)


@pytest.mark.parametrize("outcome", [["--handoff", "c-forged"], ["--failed"]])
def test_a_wrong_token_is_refused_and_changes_nothing(
    autostart: dict, outcome: list[str]
) -> None:
    proc, db, queue = autostart["proc"], autostart["db"], autostart["queue"]
    job_id = enqueue(queue, "a", head=None)
    _wait_for(lambda: _running(db, job_id), 20, proc)
    token = _proof(_harness_record(autostart, job_id))
    forged = token[:-1] + ("A" if token[-1] != "A" else "B")
    result = orch("job", "done", "claude-bot", job_id, "--db", str(db), *outcome,
                  extra_env={PROOF: forged})
    assert result.returncode == 2
    assert "is not that run's" in result.stderr
    assert "Nothing changed" in result.stderr
    assert forged not in result.stderr and token not in result.stderr
    assert job_row(db, job_id) == ("running", None)


def test_the_token_is_kept_only_as_its_hash_and_never_written_out(
    autostart: dict, tmp_path: Path
) -> None:
    from lib import job_token

    proc, db, queue = autostart["proc"], autostart["db"], autostart["queue"]
    job_id = enqueue(queue, "a", head=None)
    _wait_for(lambda: _running(db, job_id), 20, proc)
    record = _harness_record(autostart, job_id)
    token = _proof(record)
    assert not any(word in PROOF for word in ("KEY", "SECRET", "TOKEN"))
    slot = _slot(db, job_id)
    assert slot["run_token_hash"] == job_token.digest(token)
    assert token not in [str(value) for value in tuple(slot)]
    status = orch("status", "--db", str(db), "--machines", str(MACHINES))
    assert token not in status.stdout + status.stderr
    (autostart["harness_dir"] / f"{job_id}.go").touch()
    _wait_for(lambda: _released(db, job_id), 20, proc)
    assert job_row(db, job_id) == ("done", "c-" + job_id[:6])
    for path in tmp_path.rglob("*"):
        if path.is_file() and autostart["harness_dir"] not in path.parents:
            assert token.encode() not in path.read_bytes(), path
    with sqlite3.connect(db) as connection:
        dump = "\n".join(connection.iterdump())
    assert token not in dump


PROOF_IN_COMMANDS = f"#!{sys.executable}\n" + """# A harness whose shell commands carry its proof.
import json, os, shlex, subprocess, sys, time
from pathlib import Path

proof, job = os.environ["ORCH_JOB_PROOF"], os.environ["ORCH_JOB_ID"]
chunks = "''".join(proof[i:i + 8] for i in range(0, len(proof), 8))
commands = [
    "env ORCH_JOB_PROOF=" + proof + " orch job done",
    "env ORCH_JOB_PROOF=" + chunks + " orch job done",
    "bash -lc " + shlex.quote("ORCH_JOB_PROOF=" + json.dumps(proof) + " orch job done"),
]
for n, command in enumerate(commands):
    print(json.dumps({"type": "assistant", "message": {"id": f"m{n}", "content": [
        {"type": "tool_use", "id": f"t{n}", "name": "Bash", "input": {"command": command}}]}}),
        flush=True)
out = Path(os.environ["FAKE_HARNESS_DIR"])
(out / f"{job}.proof").write_text(proof)
go = out / f"{job}.go"
deadline = time.time() + 60
while not go.exists() and time.time() < deadline:
    time.sleep(0.05)
subprocess.run([sys.executable, os.environ["FAKE_ORCH"], "job", "done",
                os.environ["ORCH_JOB_BOT"], job, "--handoff", "c-" + job[:6]], check=True)
"""


def _telemetry_texts(db: Path, job_id: str) -> list[str]:
    with sqlite3.connect(db) as connection:
        row = connection.execute("SELECT * FROM job_telemetry WHERE job_id = ?", (job_id,)).fetchone()
    return [] if row is None else [value for value in row if isinstance(value, str)]


def _command_titles(db: Path, job_id: str) -> list[str]:
    with sqlite3.connect(db) as connection:
        row = connection.execute("SELECT steps FROM job_telemetry WHERE job_id = ?", (job_id,)).fetchone()
    steps = json.loads(row[0]) if row is not None else []
    return [step["title"] for step in steps if step["kind"] == "command"]


def test_the_runs_proof_never_reaches_its_persisted_or_published_summary(
    autostart: dict, tmp_path: Path
) -> None:
    """#100 r8: the summary's scrubber knows the proof the run handed its harness."""
    from lib.dashboard import collect_snapshot, write_snapshot_pages
    from tests.test_telemetry import readable

    proc, db, queue = autostart["proc"], autostart["db"], autostart["queue"]
    autostart["harness"].write_text(PROOF_IN_COMMANDS, encoding="utf-8")
    job_id = enqueue(queue, "a", head=None, timeout_seconds=6)
    proof_file = autostart["harness_dir"] / f"{job_id}.proof"
    proof = _wait_for(lambda: proof_file.exists() and proof_file.read_text(), 20, proc)
    titles = _wait_for(lambda: len(_command_titles(db, job_id)) == 3 and _command_titles(db, job_id),
                       20, proc)
    assert all("[redacted-env]" in title for title in titles), titles
    poller_log = tmp_path / "poller.log"
    poller_log.touch()
    status = write_snapshot_pages(tmp_path / "public", collect_snapshot(db, poller_log, now=time.time()))[0]
    published = json.loads(status.read_text())
    assert any(row.get("telemetry") for row in published["running"])
    for text in [*_telemetry_texts(db, job_id), status.read_text(), *titles]:
        assert not readable(proof, text), text
    (autostart["harness_dir"] / f"{job_id}.go").touch()
    _wait_for(lambda: _released(db, job_id), 20, proc)
    assert job_row(db, job_id) == ("done", "c-" + job_id[:6])
    for text in _telemetry_texts(db, job_id):
        assert not readable(proof, text), text


def test_a_token_minted_for_one_run_does_not_reach_the_next_job(autostart: dict) -> None:
    proc, db, queue = autostart["proc"], autostart["db"], autostart["queue"]
    first = enqueue(queue, "a", head=None)
    second = enqueue(queue, "b", head=None)
    _wait_for(lambda: _running(db, first) and _running(db, second), 20, proc)
    one, two = _harness_record(autostart, first), _harness_record(autostart, second)
    assert _proof(one) != _proof(two)
    result = orch("job", "done", "claude-bot", second, "--db", str(db), "--failed",
                  extra_env={PROOF: _proof(one)})
    assert result.returncode == 2
    assert job_row(db, second) == ("running", None)


def test_an_autostart_claim_with_no_recorded_token_is_refused(tmp_path: Path) -> None:
    from lib import job_token

    db, queue = make_queue(tmp_path)
    job_id = enqueue(queue, "a")
    taken = local_pool(queue).claim("claude", autostart=(os.getpid(), None))
    assert taken is not None and taken[0].id == job_id
    token, _hash = job_token.mint()
    result = orch("job", "done", "claude-bot", job_id, "--db", str(db), "--failed",
                  extra_env={PROOF: token})
    assert result.returncode == 2
    assert "its claim has no run token" in result.stderr
    assert job_row(db, job_id) == ("running", None)


def test_job_token_matching() -> None:
    from lib import job_token

    token, stored = job_token.mint()
    assert job_token.matches(token, stored)
    assert not job_token.matches(token + "x", stored)
    assert not job_token.matches(None, stored)
    assert not job_token.matches(token, None)
    assert not job_token.matches(token, "")
    assert job_token.presented({}) is None
    assert job_token.presented({PROOF: token}) == token
    assert job_token.presented({PROOF: " "}) is None


def test_descends_from_walks_the_parent_chain() -> None:
    from lib.worktrees import descends_from

    parents = {50: 40, 40: 30, 30: 1}
    assert descends_from(50, 30, parent=parents.get)
    assert descends_from(50, 50, parent=parents.get)
    assert not descends_from(50, 99, parent=parents.get)
    assert not descends_from(50, 30, parent=lambda _pid: None)
    assert descends_from(os.getpid(), os.getppid())


# --- a duplicate verdict at the same head triggers no second action -----------


def _jobs(queue: SQLiteJobQueue) -> list[tuple[str, str, str]]:
    with queue.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT action, recipient, dedupe_key FROM jobs ORDER BY created_at, rowid"
            )
        ]


def _outcomes(queue: SQLiteJobQueue) -> dict[str, str]:
    with queue.connect() as connection:
        return dict(
            connection.execute("SELECT comment_id, outcome FROM handled_comments").fetchall()
        )


def test_a_duplicate_revise_at_the_same_head_starts_no_second_job(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    head = "a" * 40
    seed_review_job(dispatcher, queue, head)
    revise = VERDICT.format(round="1/3", head=head, verdict="REVISE", risk="high")
    first = comment("v-1", revise, author="codex-login", created_at=1)
    dispatcher.process(ticket(first, head=head))
    after_first = _jobs(queue)
    assert [job[0] for job in after_first].count("fix") == 1

    duplicate = comment("v-2", revise, author="codex-login", created_at=2)
    dispatcher.process(ticket(first, duplicate, head=head))
    dispatcher.process(ticket(first, duplicate, head=head))  # and on a later poll

    assert _jobs(queue) == after_first  # no second fix, no Grok wake either
    assert _outcomes(queue)["v-2"] == "duplicate-verdict"


def test_a_duplicate_approve_at_the_same_head_does_not_ship_twice(tmp_path: Path) -> None:
    actions = FakeReviewActions()
    dispatcher, queue = setup(tmp_path, review_actions=actions)
    head = "b" * 40
    seed_review_job(dispatcher, queue, head)
    approve = VERDICT.format(round="1/3", head=head, verdict="APPROVE", risk="high")
    green = {head: CIResult("success")}
    first = comment("a-1", approve, author="codex-login", created_at=1)
    duplicate = comment("a-2", approve, author="codex-login", created_at=2)

    dispatcher.process(ticket(first, duplicate, head=head, ci=green))
    jobs = _jobs(queue)
    dispatcher.process(ticket(first, duplicate, head=head, ci=green))

    assert actions.merges == [(True, head)]
    assert _jobs(queue) == jobs
    assert [job for job in jobs if job[1] != "grok"] == [
        job for job in _jobs(queue) if job[1] != "grok"
    ]
    outcomes = _outcomes(queue)
    assert outcomes["a-1"] == "merged"
    assert outcomes["a-2"] == "duplicate-verdict"
    # Only the first verdict's done wake exists.
    assert not [job for job in jobs if job[2].endswith(":a-2")]


def test_a_duplicate_approve_on_a_later_poll_does_not_ship_twice(tmp_path: Path) -> None:
    actions = FakeReviewActions()
    dispatcher, queue = setup(tmp_path, review_actions=actions)
    head = "c" * 40
    seed_review_job(dispatcher, queue, head)
    approve = VERDICT.format(round="1/3", head=head, verdict="APPROVE", risk="high")
    green = {head: CIResult("success")}
    first = comment("a-1", approve, author="codex-login", created_at=1)
    dispatcher.process(ticket(first, head=head, ci=green))
    jobs = _jobs(queue)
    duplicate = comment("a-2", approve, author="codex-login", created_at=2)
    dispatcher.process(ticket(first, duplicate, head=head, ci=green))
    assert actions.merges == [(True, head)]
    assert _jobs(queue) == jobs
    assert _outcomes(queue)["a-2"] == "duplicate-verdict"


def test_a_different_verdict_at_the_same_head_is_not_a_duplicate(tmp_path: Path) -> None:
    actions = FakeReviewActions()
    dispatcher, queue = setup(tmp_path, review_actions=actions)
    head = "d" * 40
    seed_review_job(dispatcher, queue, head)
    revise = comment(
        "v-1", VERDICT.format(round="1/3", head=head, verdict="REVISE", risk="high"),
        author="codex-login", created_at=1,
    )
    approve = comment(
        "v-2", VERDICT.format(round="1/3", head=head, verdict="APPROVE", risk="high"),
        author="codex-login", created_at=2,
    )
    dispatcher.process(ticket(revise, approve, head=head, ci={head: CIResult("success")}))
    assert actions.merges == []
    outcomes = _outcomes(queue)
    assert outcomes["v-1"] == "queued-review-fix"
    assert outcomes["v-2"] == "stale-approve"  # the existing rule, unchanged


# --- Codex PR #82 r2 F2.3: only an acted-on verdict makes a duplicate ----------


def test_a_rejected_wrong_role_revise_does_not_suppress_its_correction(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    head = "e" * 40
    seed_review_job(dispatcher, queue, head)
    revise = VERDICT.format(round="1/3", head=head, verdict="REVISE", risk="high")
    wrong = comment(
        "v-wrong", revise.replace("role=reviewer", "role=builder"),
        author="codex-login", created_at=1,
    )
    dispatcher.process(ticket(wrong, head=head))
    assert _outcomes(queue)["v-wrong"] == "rejected-revise-author"
    assert [job[0] for job in _jobs(queue)].count("fix") == 0

    corrected = comment("v-right", revise, author="codex-login", created_at=2)
    dispatcher.process(ticket(wrong, corrected, head=head))
    dispatcher.process(ticket(wrong, corrected, head=head))  # and on a later poll
    assert _outcomes(queue)["v-right"] == "queued-review-fix"
    assert [job[0] for job in _jobs(queue)].count("fix") == 1

    # A repeat of the accepted correction is still a duplicate (one job).
    again = comment("v-again", revise, author="codex-login", created_at=3)
    dispatcher.process(ticket(wrong, corrected, again, head=head))
    assert _outcomes(queue)["v-again"] == "duplicate-verdict"
    assert [job[0] for job in _jobs(queue)].count("fix") == 1


def test_a_stale_verdict_does_not_make_a_later_one_a_duplicate(tmp_path: Path) -> None:
    """A verdict that took no action (stale, CI red) is no dedupe anchor."""
    actions = FakeReviewActions()
    dispatcher, queue = setup(tmp_path, review_actions=actions)
    head = "f" * 40
    seed_review_job(dispatcher, queue, head)
    approve = VERDICT.format(round="1/3", head=head, verdict="APPROVE", risk="high")
    first = comment("a-red", approve, author="codex-login", created_at=1)
    dispatcher.process(ticket(first, head=head, ci={head: CIResult("failure")}))
    assert _outcomes(queue)["a-red"] == "approve-ci-red"
    assert actions.merges == []

    second = comment("a-green", approve, author="codex-login", created_at=2)
    green = {head: CIResult("success")}
    dispatcher.process(ticket(first, second, head=head, ci=green))
    dispatcher.process(ticket(first, second, head=head, ci=green))
    assert _outcomes(queue)["a-green"] == "merged"
    assert actions.merges == [(True, head)]
