"""`orch wait <bot> --autostart` on remote devices (issue #85 B).

The waiter now claims through the whole device pool. A job placed on a
remote device starts over the bot's own SSH alias through `orch job run`
(lib/remote.py, as F2-02 built it), with no `--worktree`. The harness there
cannot reach orch.db, so it ends its output with `ORCH-HANDOFF: <comment-id>`
(or `failed`) and the waiter runs `orch job done` on the box when the run
ends. Every SSH call here goes to tests/fake_ssh.py; nothing reaches a host.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from lib.brief import render_brief
from lib.device_pool import DevicePool
from lib.machines_yml import load_machines_yml
from lib.queue import JobRequest, SQLiteJobQueue
from lib.waiter import HarnessCommand, Waiter, handoff_marker
from tests.test_brief import detail
from tests.test_remote_start import Ssh
from tests.test_waiter import FakeContext, FakeSpawn, enqueue, slots, state

ROOT = Path(__file__).resolve().parents[1]
ORCH = ROOT / "bin" / "orch"


def remote_machines(tmp_path: Path, *, box_slots: int = 1, box_enabled: bool = True) -> Path:
    path = tmp_path / "machines.yml"
    path.write_text(
        "devices:\n"
        f"  - name: box\n    local: true\n    enabled: {str(box_enabled).lower()}\n"
        f"    slot_max: {box_slots}\n"
        "    harnesses: {claude: claude-bot, codex: codex-bot, cursor: cursor-bot}\n"
        "  - name: pc\n    tailscale_name: pc.example.ts.net\n    slot_max: 3\n"
        "    harnesses: {claude: claude-bot, codex: codex-bot, cursor: cursor-bot}\n"
        f"    workdir_root: {tmp_path}/remote/{{bot}}/work\n    ssh_alias: fakepc-{{bot}}\n",
        encoding="utf-8",
    )
    return path


def make_waiter(tmp_path: Path, machines: Path, spawn, *, environ=None, log=None,
                orch=("python3", "/factory/bin/orch"), alive=None):
    db = tmp_path / "orch.db"
    queue = SQLiteJobQueue(db, clock=time.time)
    config, findings = load_machines_yml(machines, allow_placeholders=True)
    assert config is not None, findings
    waiter = Waiter(
        bot="claude-bot",
        queue=queue,
        pool=DevicePool(queue, config),
        harness=HarnessCommand(bot="claude-bot", command=("fake-harness", "-p")),
        context=FakeContext(),
        state_dir=tmp_path / "state",
        db=db,
        machines=machines,
        orch=orch,
        spawn=spawn,
        log=(log if log is not None else []).append,
        environ=environ or {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        **({"alive": alive} if alive is not None else {}),
    )
    return waiter, queue, db


def test_autostart_places_on_a_remote_device_and_runs_without_worktree(tmp_path: Path) -> None:
    machines = remote_machines(tmp_path, box_slots=1)
    spawn = FakeSpawn()
    waiter, queue, db = make_waiter(tmp_path, machines, spawn)
    first = enqueue(queue, "a")
    second = enqueue(queue, "b")

    # Least loaded: the pc (3 free) before the box (1 free).
    assert waiter.step() == first
    assert waiter.step() == second
    assert slots(db) == {first: "pc", second: "pc"}
    for argv, _ in spawn.calls:
        assert argv[:6] == ["python3", "/factory/bin/orch", "job", "run", "claude-bot", argv[5]]
        assert "--worktree" not in argv
        assert argv[-3:] == ["--", "fake-harness", "-p"]
    brief = spawn.stdin[0].decode("utf-8")
    assert "ORCH-HANDOFF: <comment-id>" in brief
    assert "$ORCH_JOB_WORKDIR" in brief
    assert "orch job done claude-bot" not in brief  # there is no orch.db on the device


def test_a_box_placement_still_gets_a_worktree(tmp_path: Path) -> None:
    machines = remote_machines(tmp_path, box_slots=5)
    spawn = FakeSpawn()
    waiter, queue, db = make_waiter(tmp_path, machines, spawn)
    job_id = enqueue(queue, "a")
    assert waiter.step() == job_id
    assert slots(db) == {job_id: "box"}
    assert "--worktree" in spawn.calls[0][0]


def _finish_run(tmp_path: Path, job_id: str, spawn: FakeSpawn, output: str, code: int = 0) -> None:
    log = tmp_path / "state" / "jobs" / job_id / "run.log"
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(output)
    spawn.children[0].returncode = code


def test_the_waiter_runs_orch_job_done_on_the_box_from_the_handoff_marker(tmp_path: Path) -> None:
    machines = remote_machines(tmp_path, box_enabled=False)
    spawn = FakeSpawn()
    log: list[str] = []
    waiter, queue, db = make_waiter(tmp_path, machines, spawn, log=log)
    job_id = enqueue(queue, "a")
    waiter.step()
    _finish_run(tmp_path, job_id, spawn, "working...\nORCH-HANDOFF: not-this\nORCH-HANDOFF: 4242\n")
    waiter.step()
    with sqlite3.connect(db) as connection:
        row = connection.execute(
            "SELECT state, handoff_comment_id FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    assert row == ("done", "4242")
    assert any("orch job done" in line and job_id in line for line in log)


def test_a_remote_run_with_no_marker_is_failed_for_t6(tmp_path: Path) -> None:
    machines = remote_machines(tmp_path, box_enabled=False)
    spawn = FakeSpawn()
    waiter, queue, db = make_waiter(tmp_path, machines, spawn)
    job_id = enqueue(queue, "a")
    waiter.step()
    _finish_run(tmp_path, job_id, spawn, "crashed\n", code=1)
    waiter.step()
    assert state(db, job_id) == "failed"


def test_a_failed_marker_fails_the_job(tmp_path: Path) -> None:
    machines = remote_machines(tmp_path, box_enabled=False)
    spawn = FakeSpawn()
    waiter, queue, db = make_waiter(tmp_path, machines, spawn)
    job_id = enqueue(queue, "a")
    waiter.step()
    _finish_run(tmp_path, job_id, spawn, "ORCH-HANDOFF: failed\n")
    waiter.step()
    assert state(db, job_id) == "failed"


def test_a_job_already_finished_is_left_alone(tmp_path: Path) -> None:
    machines = remote_machines(tmp_path, box_enabled=False)
    spawn = FakeSpawn()
    waiter, queue, db = make_waiter(tmp_path, machines, spawn)
    job_id = enqueue(queue, "a")
    waiter.step()
    queue.finish(job_id, owner="claude", handoff_comment_id="111")
    _finish_run(tmp_path, job_id, spawn, "ORCH-HANDOFF: 4242\n")
    waiter.step()
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT handoff_comment_id FROM jobs WHERE id = ?", (job_id,)
        ).fetchone() == ("111",)


def test_box_jobs_still_finish_themselves(tmp_path: Path) -> None:
    machines = remote_machines(tmp_path, box_slots=5)
    spawn = FakeSpawn()
    waiter, queue, db = make_waiter(tmp_path, machines, spawn)
    job_id = enqueue(queue, "a")
    waiter.step()
    _finish_run(tmp_path, job_id, spawn, "ORCH-HANDOFF: 4242\n")
    waiter.step()
    assert state(db, job_id) == "running"  # the box harness runs orch job done itself


def test_handoff_marker_accepts_only_comment_ids() -> None:
    assert handoff_marker("ORCH-HANDOFF: 123\n") == "123"
    assert handoff_marker("x\nORCH-HANDOFF: 1\nORCH-HANDOFF: 2") == "2"
    assert handoff_marker("ORCH-HANDOFF: 0b8f5c2e-1d2a-4c3b-9e8f-1234567890ab") == (
        "0b8f5c2e-1d2a-4c3b-9e8f-1234567890ab"
    )
    assert handoff_marker("ORCH-HANDOFF: failed") == "failed"
    for text in ("", "ORCH-HANDOFF: $(id)", "ORCH-HANDOFF: 12 34", "ORCH-HANDOFF:",
                 "ORCH-HANDOFF: <comment-id>", "say ORCH-HANDOFF: 5"):
        assert handoff_marker(text) is None, text


def test_the_remote_brief_names_the_workdir_and_the_marker() -> None:
    queue_job = _job("build")
    text = render_brief(queue_job, bot="claude-bot", context=None, remote_device="pc")
    assert "pc" in text and "$ORCH_JOB_WORKDIR" in text
    assert "ORCH-HANDOFF: <comment-id>" in text
    assert "ORCH-HANDOFF: failed" in text
    local = render_brief(queue_job, bot="claude-bot", context=None)
    assert "ORCH-HANDOFF" not in local
    assert "orch job done claude-bot" in local


def _job(action: str):
    from lib.queue import Job

    request = JobRequest(repo="owner/repo", ticket_id="42", action=action, recipient="claude",
                         source_comment_id="c", timeout_seconds=3600, head="3f9c2ab",
                         detail=detail(action=action))
    return Job(id="0123abcd", dedupe_key="k", request=request, state="running",
               created_at=1.0, last_activity_at=1.0, finished_at=None, handoff_comment_id=None)


def test_remote_autostart_end_to_end_over_fake_ssh(tmp_path: Path) -> None:
    """Claim -> orch job run -> ssh (fake) -> harness -> marker -> orch job done on the box."""
    machines = remote_machines(tmp_path, box_enabled=False)
    ssh = Ssh(tmp_path)
    env = {"HOME": str(tmp_path), **ssh.env()}
    harness = tmp_path / "harness.py"
    harness.write_text(
        "import os, sys\n"
        "sys.stdin.read()\n"
        "print('worked in', os.environ.get('ORCH_JOB_WORKDIR'))\n"
        "print('ORCH-HANDOFF: 5150')\n",
        encoding="utf-8",
    )
    db = tmp_path / "orch.db"
    queue = SQLiteJobQueue(db, clock=time.time)
    config, _ = load_machines_yml(machines, allow_placeholders=True)
    log: list[str] = []
    waiter = Waiter(
        bot="claude-bot",
        queue=queue,
        pool=DevicePool(queue, config),
        harness=HarnessCommand(bot="claude-bot", command=(sys.executable, str(harness))),
        context=FakeContext(),
        state_dir=tmp_path / "state",
        db=db,
        machines=machines,
        orch=(sys.executable, str(ORCH)),
        spawn=subprocess.Popen,
        log=log.append,
        environ=env,
    )
    job_id = enqueue(queue, "a")
    assert waiter.step() == job_id
    deadline = time.monotonic() + 30
    while waiter.running() and time.monotonic() < deadline:
        time.sleep(0.1)
        waiter.step()
    assert not waiter.running(), log
    with sqlite3.connect(db) as connection:
        row = connection.execute(
            "SELECT state, handoff_comment_id FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    run_log = (tmp_path / "state" / "jobs" / job_id / "run.log").read_text(encoding="utf-8")
    assert row == ("done", "5150"), run_log
    assert (tmp_path / "state" / "jobs" / job_id / "exit").read_text(encoding="utf-8") == "0\n"
    assert f"{tmp_path}/remote/claude-bot/work/jobs/{job_id}" in run_log
    assert slots(db) == {}  # orch job run released the pc slot once the worker was gone
    calls = ssh.calls()
    assert calls and all(call["user"] == "claude-bot" for call in calls)
    assert all(call["alias"] == "fakepc-claude-bot" for call in calls)


# --- #87 review F1.5: the marker belongs to this run and needs exit 0 -----------


def _done_row(db: Path, job_id: str) -> tuple:
    with sqlite3.connect(db) as connection:
        return connection.execute(
            "SELECT state, handoff_comment_id FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()


def _remote_run(tmp_path: Path):
    machines = remote_machines(tmp_path, box_enabled=False)
    spawn = FakeSpawn()
    waiter, queue, db = make_waiter(tmp_path, machines, spawn)
    job_id = enqueue(queue, "a")
    return waiter, spawn, db, job_id


def test_an_earlier_marker_then_a_failed_exit_is_failed_not_done(tmp_path: Path) -> None:
    waiter, spawn, db, job_id = _remote_run(tmp_path)
    waiter.step()
    _finish_run(tmp_path, job_id, spawn, "ORCH-HANDOFF: 4242\nTraceback: fatal error\n", code=1)
    waiter.step()
    assert _done_row(db, job_id) == ("failed", None)


def test_a_last_line_marker_with_a_nonzero_exit_is_failed(tmp_path: Path) -> None:
    waiter, spawn, db, job_id = _remote_run(tmp_path)
    waiter.step()
    _finish_run(tmp_path, job_id, spawn, "ORCH-HANDOFF: 4242\n", code=2)
    waiter.step()
    assert _done_row(db, job_id) == ("failed", None)


def test_a_malformed_final_marker_after_a_valid_one_is_failed(tmp_path: Path) -> None:
    waiter, spawn, db, job_id = _remote_run(tmp_path)
    waiter.step()
    _finish_run(tmp_path, job_id, spawn, "ORCH-HANDOFF: 4242\nORCH-HANDOFF: <comment-id>\n")
    waiter.step()
    assert _done_row(db, job_id) == ("failed", None)


def test_a_marker_from_an_earlier_run_in_the_log_does_not_count(tmp_path: Path) -> None:
    waiter, spawn, db, job_id = _remote_run(tmp_path)
    job_dir = tmp_path / "state" / "jobs" / job_id
    job_dir.mkdir(parents=True)
    (job_dir / "run.log").write_text("ORCH-HANDOFF: 4242\n", encoding="utf-8")  # an earlier run
    waiter.step()
    _finish_run(tmp_path, job_id, spawn, "lost the connection\n", code=0)
    waiter.step()
    assert _done_row(db, job_id) == ("failed", None)


def test_a_clean_last_line_marker_before_the_runners_own_lines_is_done(tmp_path: Path) -> None:
    waiter, spawn, db, job_id = _remote_run(tmp_path)
    waiter.step()
    _finish_run(tmp_path, job_id, spawn, (
        "working\nORCH-HANDOFF: 4242\n\n"
        f"orch job run: released pc slot of job {job_id}\n"
    ))
    waiter.step()
    assert _done_row(db, job_id) == ("done", "4242")


def test_handoff_marker_is_the_final_harness_line_only() -> None:
    assert handoff_marker("ORCH-HANDOFF: 1\nmore output") is None
    assert handoff_marker("ORCH-HANDOFF: 1\norch job run: released pc slot of job x\n") == "1"
    assert handoff_marker("ORCH-HANDOFF: 1\nORCH-HANDOFF: $(id)") is None


# --- #87 review F1.4: a waiter restart keeps finishing remote runs ---------------


class Liveness:
    def __init__(self) -> None:
        self.running = True
        self.calls: list[int] = []

    def __call__(self, pid: int, start: str | None) -> bool:
        self.calls.append(pid)
        return self.running


def test_a_remote_run_records_what_a_restarted_waiter_needs(tmp_path: Path) -> None:
    waiter, spawn, db, job_id = _remote_run(tmp_path)
    waiter.step()
    job_dir = tmp_path / "state" / "jobs" / job_id
    argv = spawn.calls[0][0]
    status = argv[argv.index("--status-file") + 1]
    assert status == str(job_dir / "exit")
    assert argv.index("--status-file") < argv.index("--")
    record = json.loads((job_dir / "remote.json").read_text(encoding="utf-8"))
    assert record["job_id"] == job_id and record["pid"] == spawn.children[0].pid
    assert record["offset"] == 0


def test_a_restarted_waiter_finishes_a_remote_run_once(tmp_path: Path) -> None:
    machines = remote_machines(tmp_path, box_enabled=False)
    first, queue, db = make_waiter(tmp_path, machines, FakeSpawn())
    job_id = enqueue(queue, "a")
    assert first.step() == job_id
    del first  # the waiter restarts; its orch job run keeps going in its own session

    alive = Liveness()
    log: list[str] = []
    second, _, _ = make_waiter(tmp_path, machines, FakeSpawn(), alive=alive, log=log)
    assert second.step() is None
    assert second.running() == {job_id}
    assert _done_row(db, job_id) == ("running", None)

    job_dir = tmp_path / "state" / "jobs" / job_id
    with open(job_dir / "run.log", "a", encoding="utf-8") as handle:
        handle.write("ORCH-HANDOFF: 4242\n")
    (job_dir / "exit").write_text("0\n", encoding="utf-8")
    alive.running = False
    second.step()
    assert _done_row(db, job_id) == ("done", "4242")
    assert not second.running()
    assert any("orch job done" in line and job_id in line for line in log)

    third_log: list[str] = []
    third, _, _ = make_waiter(tmp_path, machines, FakeSpawn(), alive=Liveness(), log=third_log)
    third.step()
    assert not third.running()
    assert not any("orch job done" in line for line in third_log)  # never finished twice


def test_a_restarted_waiter_fails_a_remote_run_whose_runner_died_without_a_status(
    tmp_path: Path,
) -> None:
    machines = remote_machines(tmp_path, box_enabled=False)
    first, queue, db = make_waiter(tmp_path, machines, FakeSpawn())
    job_id = enqueue(queue, "a")
    first.step()
    alive = Liveness()
    alive.running = False
    job_dir = tmp_path / "state" / "jobs" / job_id
    (job_dir / "run.log").write_text("ORCH-HANDOFF: 4242\n", encoding="utf-8")
    second, _, _ = make_waiter(tmp_path, machines, FakeSpawn(), alive=alive)
    second.step()
    assert _done_row(db, job_id) == ("failed", None)


def test_a_restarted_waiter_leaves_a_job_finished_meanwhile_alone(tmp_path: Path) -> None:
    machines = remote_machines(tmp_path, box_enabled=False)
    first, queue, db = make_waiter(tmp_path, machines, FakeSpawn())
    job_id = enqueue(queue, "a")
    first.step()
    queue.finish(job_id, owner="claude", handoff_comment_id="111")
    second, _, _ = make_waiter(tmp_path, machines, FakeSpawn(), alive=Liveness())
    second.step()
    assert not second.running()
    assert not (tmp_path / "state" / "jobs" / job_id / "remote.json").exists()
    assert _done_row(db, job_id) == ("done", "111")
