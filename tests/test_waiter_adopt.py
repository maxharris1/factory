"""A restarted waiter follows the remote runs it adopts until they end (#318)."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from lib.worktrees import process_start
from tests.handoff_store import serve_job
from tests.test_waiter import FakeSpawn, enqueue
from tests.test_waiter_remote import make_waiter, remote_machines, silence


def _row(db: Path, job_id: str) -> tuple:
    with sqlite3.connect(db) as connection:
        return connection.execute(
            "SELECT state, handoff_comment_id FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()


def _restart(tmp_path: Path):
    machines = remote_machines(tmp_path, box_enabled=False)
    first, queue, db = make_waiter(tmp_path, machines, FakeSpawn())
    job_id = enqueue(queue, "a")
    assert first.step() == job_id
    return machines, queue, db, job_id, tmp_path / "state" / "jobs" / job_id


def _end_run(job_dir: Path, comment_id: str) -> None:
    with open(job_dir / "run.log", "a", encoding="utf-8") as handle:
        handle.write(f"ORCH-HANDOFF: {comment_id}\norch job run: released pc slot\n")
    (job_dir / "exit").write_text("0\n", encoding="utf-8")


def test_an_adopted_run_that_still_beats_is_not_ended_by_a_pid_misread(tmp_path: Path) -> None:
    machines, queue, db, job_id, job_dir = _restart(tmp_path)
    log: list[str] = []
    second, _, _ = make_waiter(tmp_path, machines, FakeSpawn(), alive=lambda *_: False, log=log)

    queue.heartbeat(job_id, owner="claude")
    second.step()
    queue.heartbeat(job_id, owner="claude")
    second.step()
    assert second.running() == {job_id}
    assert _row(db, job_id) == ("running", None)
    assert (job_dir / "remote.json").exists()
    assert not any("exited" in line for line in log)

    serve_job(db, job_id, "4242")
    _end_run(job_dir, "4242")
    second.step()
    assert _row(db, job_id) == ("done", "4242")
    assert not second.running()
    assert not any("255" in line for line in log)
    assert [line for line in log if "orch job done" in line] == [
        f"job {job_id}: ran orch job done on the box: done, handoff 4242"
    ]

    third_log: list[str] = []
    third, _, _ = make_waiter(tmp_path, machines, FakeSpawn(), alive=lambda *_: True, log=third_log)
    third.step()
    assert not third.running()
    assert not any("orch job done" in line for line in third_log)


def test_an_adopted_live_process_is_followed_until_it_exits_with_a_handoff(tmp_path: Path) -> None:
    machines, queue, db, job_id, job_dir = _restart(tmp_path)
    go = tmp_path / "go"
    run = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import pathlib, sys, time\n"
            "go, job_dir = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])\n"
            "while not go.exists(): time.sleep(0.02)\n"
            "with open(job_dir / 'run.log', 'a') as log: log.write('ORCH-HANDOFF: 4242\\n')\n"
            "(job_dir / 'exit').write_text('0\\n')\n",
            str(go),
            str(job_dir),
        ]
    )
    try:
        record = json.loads((job_dir / "remote.json").read_text(encoding="utf-8"))
        record.update(pid=run.pid, start=process_start(run.pid))
        (job_dir / "remote.json").write_text(json.dumps(record), encoding="utf-8")
        silence(db, job_id)

        log: list[str] = []
        second, _, _ = make_waiter(tmp_path, machines, FakeSpawn(), log=log)
        second.step()
        second.step()
        assert second.running() == {job_id}
        assert _row(db, job_id) == ("running", None)

        serve_job(db, job_id, "4242")
        go.touch()
        assert run.wait(timeout=30) == 0
        second.step()
    finally:
        run.kill()
        run.wait()
    assert _row(db, job_id) == ("done", "4242")
    assert not any("255" in line for line in log)


def test_an_adopted_run_gone_and_silent_without_a_status_is_failed(tmp_path: Path) -> None:
    machines, queue, db, job_id, job_dir = _restart(tmp_path)
    silence(db, job_id)
    second, _, _ = make_waiter(tmp_path, machines, FakeSpawn(), alive=lambda *_: False)
    second.step()
    assert _row(db, job_id) == ("failed", None)
    assert not (job_dir / "remote.json").exists()
