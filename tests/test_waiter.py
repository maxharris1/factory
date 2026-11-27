"""`orch wait <bot> --autostart`: the waiter starts claimed jobs itself (#80).

On claim the waiter runs `orch job run <bot> <job-id> --worktree -- <the
bot's own harness command>` with a rendered standard brief on stdin, then
goes back to waiting. No model turn and no bot-to-bot message sits between
the claim and the start. The harness command comes only from the bot's own
config file; nothing from a job row or a comment reaches an argv.
"""

from __future__ import annotations

import ast
import atexit
import base64
import re
import json
import os
import sqlite3
import stat
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from lib import attention, containers
from lib import queue as queue_module
from lib import waiter as waiter_module
from lib.device_pool import DevicePool
from lib.dashboard import collect_snapshot
from lib.dispatcher import Comment
from lib.job_state import RUNNING, derive, read_placements
from lib.machines_yml import load_machines_yml, orch_host
from lib.orch_yml import load_orch_yml
from lib.queue import JobRequest, SQLiteJobQueue
from lib.remote import Called
from lib.stacks import stack_name
from lib.waiter import (
    CleanupStreak,
    ContextResult,
    GhContextSource,
    HarnessCommand,
    Waiter,
    WaiterError,
    cleanup_streak,
    job_run_argv,
    load_harness_command,
    local_only,
)
from tests import fake_runner
from tests.fake_docker import CREATED, Docker
from tests.child_reaper import descendants
from tests.pool_helpers import LOCAL_BOX_MACHINES, poller_adopts
from tests.procs import alive
from tests.test_brief import FACTORY_MD, detail
from tests.test_wait import GO, dispatcher_for, hold_write_lock, tracked

ROOT = Path(__file__).resolve().parents[1]
ORCH = ROOT / "bin" / "orch"
HARNESS_BINARIES = ("claude", "codex", "cursor-agent")


def _local_box_with_factory_root_here() -> Path:
    text = LOCAL_BOX_MACHINES.read_text(encoding="utf-8")
    box_root = "    factory_root: /home/box/factory\n"
    assert text.count(box_root) == 1
    directory = Path(tempfile.mkdtemp(prefix="orch-test-machines-"))
    atexit.register(shutil.rmtree, directory, True)
    path = directory / "machines.yml"
    path.write_text(text.replace(box_root, f"    factory_root: {ROOT}\n"), encoding="utf-8")
    return path


MACHINES = _local_box_with_factory_root_here()


# --- the harness command comes from the bot's own fixed config -------------


def write_config(path: Path, text: str, mode: int = 0o600) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def test_harness_command_is_an_argv_list_for_this_bot(tmp_path: Path) -> None:
    path = write_config(
        tmp_path / "waiter-claude-bot.yml",
        "bot: claude-bot\ncommand:\n  - my-harness\n  - -p\n  - --flag=value with spaces\n",
    )
    loaded = load_harness_command(path, "claude-bot")
    assert loaded == HarnessCommand(
        bot="claude-bot", command=("my-harness", "-p", "--flag=value with spaces")
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("bot: codex-bot\ncommand: [x]\n", "codex-bot"),
        ("bot: claude-bot\ncommand: my-harness -p\n", "list"),
        ("bot: claude-bot\ncommand: []\n", "empty"),
        ("bot: claude-bot\ncommand: [x, 3]\n", "string"),
        ("bot: claude-bot\ncommand: ['', x]\n", "empty"),
        ("bot: claude-bot\ncommand: ['-x']\n", "option"),
        ("bot: claude-bot\ncommand: [\"a\\u0000b\"]\n", "control"),
        ("bot: claude-bot\ncommand: [\"a\\nb\"]\n", "control"),
        ("bot: claude-bot\ncommand: [x]\nshell: true\n", "unknown"),
        ("command: [x]\n", "bot"),
        ("- x\n", "mapping"),
    ],
)
def test_a_bad_harness_config_is_refused(tmp_path: Path, text: str, message: str) -> None:
    path = write_config(tmp_path / "waiter.yml", text)
    with pytest.raises(WaiterError, match=message):
        load_harness_command(path, "claude-bot")


@pytest.mark.parametrize("mode", [0o620, 0o602, 0o666])
def test_a_harness_config_others_can_write_is_refused(tmp_path: Path, mode: int) -> None:
    path = write_config(tmp_path / "waiter.yml", "bot: claude-bot\ncommand: [x]\n", mode)
    with pytest.raises(WaiterError, match="writable"):
        load_harness_command(path, "claude-bot")


def test_a_missing_harness_config_is_refused(tmp_path: Path) -> None:
    with pytest.raises(WaiterError, match="harness config"):
        load_harness_command(tmp_path / "nope.yml", "claude-bot")


def test_a_symlinked_harness_config_is_refused(tmp_path: Path) -> None:
    real = write_config(tmp_path / "real.yml", "bot: claude-bot\ncommand: [x]\n")
    link = tmp_path / "waiter.yml"
    link.symlink_to(real)
    with pytest.raises(WaiterError, match="regular file"):
        load_harness_command(link, "claude-bot")


def test_job_run_argv_is_fixed_except_the_validated_job_id() -> None:
    argv = job_run_argv(
        ("python3", "/f/bin/orch"),
        bot="claude-bot",
        job_id="0123456789abcdef0123456789abcdef",
        db=Path("/s/orch.db"),
        machines=Path("/f/machines.yml"),
        command=("my-harness", "-p"),
    )
    assert argv == [
        "python3", "/f/bin/orch", "job", "run", "claude-bot",
        "0123456789abcdef0123456789abcdef",
        "--db", "/s/orch.db", "--machines", "/f/machines.yml", "--worktree",
        "--", "my-harness", "-p",
    ]


@pytest.mark.parametrize(
    "job_id", ["", "--worktree", "-x", "a b", "a;b", "$(id)", "a/b", "../x", "x\n", "é"]
)
def test_a_job_id_that_is_not_a_plain_token_never_reaches_an_argv(job_id: str) -> None:
    with pytest.raises(WaiterError):
        job_run_argv(
            ("orch",), bot="claude-bot", job_id=job_id, db=Path("/d"),
            machines=Path("/m"), command=("x",),
        )


def test_job_run_argv_refuses_a_bot_that_is_not_a_harness_bot() -> None:
    with pytest.raises(WaiterError):
        job_run_argv(
            ("orch",), bot="grok-bot", job_id="abc", db=Path("/d"),
            machines=Path("/m"), command=("x",),
        )


# --- the waiter loop, with a fake spawn -------------------------------------


class FakeChild:
    def __init__(self, pid: int):
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


class FakeSpawn:
    def __init__(self, error: BaseException | None = None):
        self.calls: list[tuple[list[str], dict]] = []
        self.stdin: list[bytes] = []
        self.children: list[FakeChild] = []
        self.error = error
        self.locks: list[int] = []

    def __call__(self, argv, **kwargs):
        if self.error is not None:
            raise self.error
        self.calls.append((list(argv), kwargs))
        self.stdin.append(kwargs["stdin"].read())
        self.locks.extend(fake_runner.hold(kwargs.get("pass_fds", ())))
        child = FakeChild(10_000 + len(self.children))
        self.children.append(child)
        return child

    def end_runners(self) -> None:
        fake_runner.end(self.locks)
        self.locks.clear()


class FakeContext:
    def __init__(self, result: ContextResult | None = None):
        self.repos: list[str] = []
        self.result = result or ContextResult(None, None, "test: no factory.md")

    def load(self, repo: str) -> ContextResult:
        self.repos.append(repo)
        return self.result


def make_queue(tmp_path: Path) -> tuple[Path, SQLiteJobQueue]:
    db = tmp_path / "orch.db"
    return db, SQLiteJobQueue(db, clock=time.time)


def local_pool(queue: SQLiteJobQueue, machines: Path = MACHINES) -> DevicePool:
    config, findings = load_machines_yml(machines)
    assert config is not None, findings
    poller_adopts(queue, machines)
    return DevicePool(queue, local_only(config), machines_path=machines)


def enqueue(queue: SQLiteJobQueue, key: str, **fields) -> str:
    action = fields.pop("action", "build")
    request = JobRequest(
        repo=fields.pop("repo", "owner/repo"),
        ticket_id=fields.pop("ticket_id", "42"),
        action=action,
        recipient=fields.pop("recipient", "claude"),
        source_comment_id=fields.pop("source_comment_id", "c-1"),
        timeout_seconds=fields.pop("timeout_seconds", 3600),
        head=fields.pop("head", "3f9c2ab"),
        detail=fields.pop("detail", detail(action=action)),
    )
    assert not fields
    return queue.enqueue(request, dedupe_key=key).id


def make_waiter(
    tmp_path: Path,
    queue: SQLiteJobQueue,
    db: Path,
    spawn: FakeSpawn,
    *,
    context: FakeContext | None = None,
    command: tuple[str, ...] = ("fake-harness", "-p"),
    log: list[str] | None = None,
    machines: Path = MACHINES,
) -> Waiter:
    return Waiter(
        bot="claude-bot",
        queue=queue,
        pool=local_pool(queue, machines),
        harness=HarnessCommand(bot="claude-bot", command=command),
        context=context or FakeContext(),
        state_dir=tmp_path / "state",
        db=db,
        machines=machines,
        orch=("python3", "/factory/bin/orch"),
        spawn=spawn,
        log=(log if log is not None else []).append,
        environ={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )


def state(db: Path, job_id: str) -> str:
    with sqlite3.connect(db) as connection:
        return connection.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]


def slots(db: Path) -> dict[str, str]:
    with sqlite3.connect(db) as connection:
        return dict(connection.execute("SELECT job_id, device FROM device_slots").fetchall())


def test_on_claim_the_waiter_starts_orch_job_run_with_the_brief_on_stdin(tmp_path: Path) -> None:
    db, queue = make_queue(tmp_path)
    job_id = enqueue(queue, "a")
    spawn = FakeSpawn()
    context = FakeContext()
    waiter = make_waiter(tmp_path, queue, db, spawn, context=context)

    assert waiter.step() == job_id

    assert len(spawn.calls) == 1
    argv, kwargs = spawn.calls[0]
    assert argv == [
        "python3", "/factory/bin/orch", "job", "run", "claude-bot", job_id,
        "--db", str(db), "--machines", str(MACHINES), "--worktree",
        "--", "fake-harness", "-p",
    ]
    assert kwargs["start_new_session"] is True
    assert kwargs.get("shell", False) is False
    brief = tmp_path / "state" / "jobs" / job_id / "brief.md"
    assert kwargs["env"]["ORCH_JOB_BRIEF"] == str(brief)
    assert spawn.stdin[0] == brief.read_bytes()
    text = brief.read_text(encoding="utf-8")
    assert "# Factory job brief: implement" in text and f"job id: {job_id}" in text
    assert stat.S_IMODE(brief.stat().st_mode) == 0o600
    assert stat.S_IMODE(brief.parent.stat().st_mode) == 0o700
    assert context.repos == ["owner/repo"]
    # The claim holds the box slot; `orch job run` takes it from here.
    assert state(db, job_id) == "running"
    assert slots(db) == {job_id: "box"}


def test_after_a_start_the_waiter_goes_back_to_waiting(tmp_path: Path) -> None:
    db, queue = make_queue(tmp_path)
    first = enqueue(queue, "a")
    spawn = FakeSpawn()
    waiter = make_waiter(tmp_path, queue, db, spawn)
    assert waiter.step() == first
    assert waiter.step() is None  # nothing queued: keeps waiting, starts nothing
    assert len(spawn.calls) == 1
    second = enqueue(queue, "b")
    assert waiter.step() == second
    assert [call[0][5] for call in spawn.calls] == [first, second]


def test_the_waiter_only_takes_its_own_bots_jobs(tmp_path: Path) -> None:
    db, queue = make_queue(tmp_path)
    codex_job = enqueue(queue, "a", recipient="codex", action="review",
                        detail=detail(action="review", round="1/3"))
    spawn = FakeSpawn()
    waiter = make_waiter(tmp_path, queue, db, spawn)
    assert waiter.step() is None
    assert spawn.calls == []
    assert state(db, codex_job) == "queued"


def test_a_finished_run_is_reaped_and_logged(tmp_path: Path) -> None:
    db, queue = make_queue(tmp_path)
    job_id = enqueue(queue, "a")
    spawn = FakeSpawn()
    log: list[str] = []
    waiter = make_waiter(tmp_path, queue, db, spawn, log=log)
    waiter.step()
    assert waiter.running() == {job_id}
    spawn.children[0].returncode = 3
    waiter.step()
    assert waiter.running() == set()
    assert any(job_id in line and "exited 3" in line for line in log)


def test_reaping_a_killed_run_removes_its_job_tmpdir(tmp_path: Path) -> None:
    db, queue = make_queue(tmp_path)
    job_id = enqueue(queue, "a")
    spawn = FakeSpawn()
    waiter = make_waiter(tmp_path, queue, db, spawn)
    waiter.step()
    scratch = tmp_path / "state" / "jobs" / job_id / "tmp"
    (scratch / "venv").mkdir(parents=True)
    (scratch / "venv" / "x").write_text("left by a run whose teardown never ran")
    spawn.children[0].returncode = -9
    waiter.step()
    assert waiter.running() == set()
    assert not scratch.exists()
    assert (scratch.parent / "brief.md").is_file()


def test_reaping_a_run_removes_a_job_tmpdir_its_harness_locked(tmp_path: Path) -> None:
    db, queue = make_queue(tmp_path)
    job_id = enqueue(queue, "a")
    spawn = FakeSpawn()
    waiter = make_waiter(tmp_path, queue, db, spawn)
    waiter.step()
    locked = tmp_path / "state" / "jobs" / job_id / "tmp" / "locked"
    (locked / "deep").mkdir(parents=True)
    (locked / "deep" / "x").write_text("scratch")
    (locked / "deep").chmod(0)
    locked.chmod(0)
    spawn.children[0].returncode = 0
    try:
        waiter.step()
        assert not locked.parent.exists()
    finally:
        if locked.exists():
            locked.chmod(0o700)
            (locked / "deep").chmod(0o700)


def test_a_failed_scratch_removal_is_retried_until_it_succeeds(tmp_path: Path) -> None:
    db, queue = make_queue(tmp_path)
    job_id = enqueue(queue, "a")
    spawn = FakeSpawn()
    log: list[str] = []
    waiter = make_waiter(tmp_path, queue, db, spawn, log=log)
    waiter.step()
    scratch = tmp_path / "state" / "jobs" / job_id / "tmp"
    scratch.mkdir()
    (scratch / "needed").write_text("scratch")
    spawn.children[0].returncode = 0
    scratch.parent.chmod(0o500)
    try:
        waiter._reap()
        waiter._reap()
        assert scratch.exists()
    finally:
        scratch.parent.chmod(0o700)
    assert sum("could not remove" in line for line in log) == 1
    waiter._reap()
    assert not scratch.exists()


def sweeping_waiter(tmp_path: Path, queue: SQLiteJobQueue, db: Path, log: list[str],
                    pool: DevicePool | None = None, *, spawn: FakeSpawn | None = None,
                    cleanup: CleanupSpawn | None = None,
                    cleanup_alert_after: int = waiter_module.CLEANUP_ALERT_AFTER,
                    ) -> tuple[Waiter, list[float]]:
    now = [0.0]
    waiter = Waiter(
        bot="claude-bot", queue=queue, pool=pool or local_pool(queue),
        harness=HarnessCommand(bot="claude-bot", command=("fake-harness", "-p")),
        context=FakeContext(), state_dir=tmp_path / "state", db=db, machines=MACHINES,
        orch=("python3", "/factory/bin/orch"), spawn=spawn or FakeSpawn(), log=log.append,
        environ={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}, clock=lambda: now[0],
        sweep_cleanup=cleanup is not None, cleanup_spawn=cleanup or CleanupSpawn(),
        cleanup_alert_after=cleanup_alert_after,
    )
    return waiter, now


class CleanupSpawn:
    def __init__(self, error: OSError | None = None):
        self.calls: list[tuple[list[str], dict]] = []
        self.children: list[FakeChild] = []
        self.error = error

    def __call__(self, argv, **kwargs):
        if self.error is not None:
            raise self.error
        self.calls.append((list(argv), kwargs))
        self.children.append(FakeChild(20_000 + len(self.children)))
        os.write(kwargs["stdout"].fileno(), f"pass {len(self.children)}\n".encode())
        return self.children[-1]


PASS_STARTED = 1_795_000_000.0
STARTED_LINE = "cleanup run started 2026-11-18T11:06:40+00:00"


def run_passes(tmp_path: Path, count: int, *, exits: int = 0) -> tuple[Path, list[str]]:
    db = tmp_path / "orch.db"
    queue = SQLiteJobQueue(db, clock=lambda: PASS_STARTED)
    cleanup = CleanupSpawn()
    log: list[str] = []
    waiter, now = sweeping_waiter(tmp_path, queue, db, log, cleanup=cleanup)
    for n in range(count):
        now[0] = n * waiter_module.CLEANUP_SWEEP_SECONDS
        waiter.step()
        cleanup.children[-1].returncode = exits + n
    now[0] = count * waiter_module.CLEANUP_SWEEP_SECONDS - 1
    waiter.step()
    return tmp_path / "state", log


def test_each_cleanup_pass_keeps_its_own_log_with_a_start_and_an_exit_line(
    tmp_path: Path
) -> None:
    state_dir, _ = run_passes(tmp_path, 2)

    assert (state_dir / "cleanup.log.1").read_text() == (
        f"{STARTED_LINE}\npass 1\ncleanup run exited 0\n")
    assert (state_dir / "cleanup.log").read_text() == (
        f"{STARTED_LINE}\npass 2\ncleanup run exited 1\n")
    for name in ("cleanup.log", "cleanup.log.1"):
        assert stat.S_IMODE((state_dir / name).stat().st_mode) == 0o600


def test_a_ninth_cleanup_pass_drops_the_oldest_log(tmp_path: Path) -> None:
    state_dir, _ = run_passes(tmp_path, 9)

    names = sorted(path.name for path in state_dir.iterdir() if path.name.startswith("cleanup"))
    assert names == ["cleanup.log", *(f"cleanup.log.{n}" for n in range(1, 8))]
    assert "pass 9\n" in (state_dir / "cleanup.log").read_text()
    assert "pass 2\n" in (state_dir / "cleanup.log.7").read_text()


def finished_pass(code: int, *output: str) -> str:
    return "".join(f"{line}\n" for line in (STARTED_LINE, *output, f"cleanup run exited {code}"))


RUNNING_PASS = f"{STARTED_LINE}\npass 9\n"
NOT_STARTED_PASS = f"{STARTED_LINE}\ncleanup run not started: cannot exec orch\n"


@pytest.mark.parametrize("passes, expected", [
    ([], None),
    ([RUNNING_PASS, NOT_STARTED_PASS], None),
    ([finished_pass(0, "removed 3")], CleanupStreak(0, "")),
    ([finished_pass(1, "boom 4"), finished_pass(1, "boom 3"), finished_pass(0),
      finished_pass(1, "boom 1")], CleanupStreak(2, "boom 4")),
    ([finished_pass(1, "boom 5"), finished_pass(75, "already running"), RUNNING_PASS,
      NOT_STARTED_PASS, finished_pass(1, "boom 1")], CleanupStreak(2, "boom 5")),
    ([finished_pass(75, "already running"), finished_pass(0)], CleanupStreak(0, "")),
    ([finished_pass(-9, "kept 12")], CleanupStreak(1, "kept 12")),
    ([finished_pass(1)], CleanupStreak(1, "")),
    ([finished_pass(1, "real failure", "   ")], CleanupStreak(1, "real failure")),
    ([finished_pass(1, "cleanup run exited 0")], CleanupStreak(1, "cleanup run exited 0")),
    ([f"{STARTED_LINE}\norch cleanup run: kept 2842cleanup run exited -9\n"],
     CleanupStreak(1, "orch cleanup run: kept 2842")),
], ids=[
    "no-passes", "none-finished", "clean", "streak-stops-at-clean", "skips-unfinished-and-75",
    "75-then-clean", "signal", "no-output", "blank-tail", "fake-exit-in-output", "glued-exit",
])
def test_cleanup_streak_reads_the_kept_passes_newest_first(
    passes: list[str], expected: CleanupStreak | None
) -> None:
    assert cleanup_streak(passes) == expected


def test_a_cleanup_pass_that_could_not_start_says_so_in_its_log(tmp_path: Path) -> None:
    db, queue = make_queue(tmp_path)
    log: list[str] = []
    waiter, _ = sweeping_waiter(tmp_path, queue, db, log,
                                cleanup=CleanupSpawn(OSError("cannot exec orch")))

    waiter.step()

    first, last = (tmp_path / "state" / "cleanup.log").read_text().splitlines()
    assert first.startswith("cleanup run started ")
    assert last == "cleanup run not started: cannot exec orch"


def test_a_pass_whose_waiter_died_keeps_its_output_without_an_exit_line(
    tmp_path: Path
) -> None:
    db, queue = make_queue(tmp_path)
    sweeping_waiter(tmp_path, queue, db, [], cleanup=CleanupSpawn())[0].step()
    restarted, _ = sweeping_waiter(tmp_path, queue, db, [], cleanup=CleanupSpawn())

    restarted.step()

    orphan = (tmp_path / "state" / "cleanup.log.1").read_text().splitlines()
    assert orphan[0].startswith("cleanup run started ") and orphan[1:] == ["pass 1"]


def test_a_planted_cleanup_log_symlink_is_moved_aside_not_written_through(
    tmp_path: Path
) -> None:
    db, queue = make_queue(tmp_path)
    target = tmp_path / "target"
    target.write_text("untouched\n")
    (tmp_path / "state").mkdir(mode=0o700)
    (tmp_path / "state" / "cleanup.log").symlink_to(target)
    waiter, _ = sweeping_waiter(tmp_path, queue, db, [], cleanup=CleanupSpawn())

    waiter.step()

    assert target.read_text() == "untouched\n"
    assert (tmp_path / "state" / "cleanup.log.1").is_symlink()
    assert not (tmp_path / "state" / "cleanup.log").is_symlink()
    assert "pass 1\n" in (tmp_path / "state" / "cleanup.log").read_text()


def test_the_waiter_starts_its_bots_cleanup_on_a_cadence_one_at_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", Docker(tmp_path).path())
    db, queue = make_queue(tmp_path)
    cleanup = CleanupSpawn()
    log: list[str] = []
    waiter, now = sweeping_waiter(tmp_path, queue, db, log, cleanup=cleanup)
    every = waiter_module.CLEANUP_SWEEP_SECONDS
    assert every == 900

    def passes_after(at: float) -> int:
        now[0] = at
        assert waiter.step() is None
        return len(cleanup.calls)

    assert passes_after(0) == 1
    argv, kwargs = cleanup.calls[0]
    assert argv == ["python3", "/factory/bin/orch", "cleanup", "run", "--bot", "claude-bot",
                    "--db", str(db), "--machines", str(MACHINES)]
    assert kwargs["start_new_session"] is True
    assert Path(kwargs["stdout"].name) == tmp_path / "state" / "cleanup.log"

    cleanup.children[0].returncode = 0
    assert passes_after(every - 1) == 1
    assert passes_after(every) == 2
    assert passes_after(2 * every) == 2, "a running pass is never joined by another"
    assert passes_after(2 * every + 5) == 2
    cleanup.children[1].returncode = 1
    assert passes_after(2 * every + 6) == 3
    assert [line for line in log if line.startswith("cleanup run exited")] == [
        f"cleanup run exited 0 ({tmp_path / 'state' / 'cleanup.log'})",
        f"cleanup run exited 1 ({tmp_path / 'state' / 'cleanup.log'})",
    ]


@pytest.mark.parametrize("error", [None, OSError("cannot exec orch")])
def test_the_cleanup_pass_does_not_block_the_claim_in_the_same_step(
    tmp_path: Path, error: OSError | None
) -> None:
    db, queue = make_queue(tmp_path)
    job_id = enqueue(queue, "a")
    spawn, cleanup = FakeSpawn(), CleanupSpawn(error)
    log: list[str] = []
    waiter, _ = sweeping_waiter(tmp_path, queue, db, log, spawn=spawn, cleanup=cleanup)

    assert waiter.step() == job_id

    assert state(db, job_id) == "running"
    assert spawn.calls[0][0][2:4] == ["job", "run"]
    assert len(cleanup.calls) == (0 if error else 1)
    failed = [line for line in log if line.startswith("cleanup run not started")]
    assert failed == ([] if error is None else ["cleanup run not started: cannot exec orch"])
    spawn.end_runners()


CLEANUP_ROW = ("orch", "cleanup-claude-bot", attention.CLEANUP_FAILING)
ALERT_LINES = ("cleanup-failing raised:", "cleanup-failing cleared:")


def attention_rows(db: Path) -> list[tuple]:
    with sqlite3.connect(db) as connection:
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'needs_attention'"
        ).fetchone() is None:
            return []
        return connection.execute(
            "SELECT repo, ticket_id, reason, detail, raised_at FROM needs_attention ORDER BY ticket_id"
        ).fetchall()


class CleanupPasses:
    def __init__(self, tmp_path: Path, log: list[str], **options) -> None:
        self.db = tmp_path / "orch.db"
        self.wall = [PASS_STARTED]
        self.queue = SQLiteJobQueue(self.db, clock=lambda: self.wall[0])
        self.cleanup = CleanupSpawn()
        self.waiter, self.now = sweeping_waiter(
            tmp_path, self.queue, self.db, log, cleanup=self.cleanup, **options)
        self.waiter.step()

    def finish(self, *codes: int) -> None:
        for code in codes:
            self.cleanup.children[-1].returncode = code
            self.advance(waiter_module.CLEANUP_SWEEP_SECONDS)

    def advance(self, seconds: float) -> None:
        self.now[0] += seconds
        self.wall[0] += seconds
        self.waiter.step()


def test_four_failed_cleanup_passes_raise_one_row_and_a_clean_pass_clears_it(
    tmp_path: Path
) -> None:
    log: list[str] = []
    passes = CleanupPasses(tmp_path, log)

    passes.finish(1, 1, 1)
    assert attention_rows(passes.db) == [], "three failed passes raise nothing"
    passes.finish(1)
    raised_at = passes.wall[0]
    detail = "the last 4 cleanup passes failed: pass 4"
    assert attention_rows(passes.db) == [(*CLEANUP_ROW, detail, raised_at)], "the fourth raises"
    items = collect_snapshot(passes.db, tmp_path / "none.log", now=raised_at)["attention"]["items"]
    assert [item["detail"] for item in items if item["kind"] == "needs-attention"] == [
        f"cleanup-failing: {detail}"]
    passes.finish(1, 1)
    assert attention_rows(passes.db) == [
        (*CLEANUP_ROW, "the last 6 cleanup passes failed: pass 6", raised_at)
    ], "later failures update the detail and keep raised_at"
    passes.finish(0)
    assert attention_rows(passes.db) == [], "a clean pass deletes the row"
    assert [line for line in log if line.startswith(ALERT_LINES)] == [
        f"cleanup-failing raised: {detail}",
        "cleanup-failing cleared: a cleanup pass exited 0",
    ]


def test_one_bots_clean_pass_leaves_another_bots_cleanup_row(tmp_path: Path) -> None:
    passes = CleanupPasses(tmp_path, [])
    other = ("orch", "cleanup-codex-bot", attention.CLEANUP_FAILING)
    other_detail = "the last 5 cleanup passes failed: boom"
    with passes.queue.connect() as connection:
        attention.raise_flag(connection, other, other_detail, 7.0)

    passes.finish(1, 1, 1, 1)
    assert [row[:3] for row in attention_rows(passes.db)] == [CLEANUP_ROW, other]
    passes.finish(0)

    assert attention_rows(passes.db) == [(*other, other_detail, 7.0)]


def test_a_restarted_waiter_raises_and_keeps_the_cleanup_row_from_the_pass_files(
    tmp_path: Path
) -> None:
    passes = CleanupPasses(tmp_path, [], cleanup_alert_after=99)
    passes.finish(1, 1, 1, 1)
    assert attention_rows(passes.db) == [], "a threshold of 99 raises nothing"
    restarted: list[str] = []

    sweeping_waiter(tmp_path, passes.queue, passes.db, restarted, cleanup=CleanupSpawn())[0].step()
    raised_at = passes.wall[0]
    passes.wall[0] += 60
    sweeping_waiter(tmp_path, passes.queue, passes.db, restarted, cleanup=CleanupSpawn())[0].step()

    assert attention_rows(passes.db) == [
        (*CLEANUP_ROW, "the last 4 cleanup passes failed: pass 4", raised_at)]
    assert [line for line in restarted if line.startswith(ALERT_LINES)] == [
        "cleanup-failing raised: the last 4 cleanup passes failed: pass 4"
    ], "the third waiter neither raises again nor logs it"


def test_a_cleanup_row_write_on_a_locked_database_is_logged_and_made_good(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(queue_module, "BUSY_TIMEOUT_SECONDS", 0.05)
    log: list[str] = []
    passes = CleanupPasses(tmp_path, log)
    passes.finish(1, 1, 1)
    holder = hold_write_lock(passes.db)
    try:
        passes.finish(1)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert attention_rows(passes.db) == [], "a locked write leaves no row"
    assert sum("not written" in line for line in log) == 1

    passes.advance(59)
    assert attention_rows(passes.db) == [], "no retry before 60 seconds"
    passes.advance(1)

    assert [row[:4] for row in attention_rows(passes.db)] == [
        (*CLEANUP_ROW, "the last 4 cleanup passes failed: pass 4")]
    assert sum("not written" in line for line in log) == 1


def test_a_planted_symlink_and_undecodable_bytes_do_not_break_the_cleanup_row(
    tmp_path: Path
) -> None:
    passes = CleanupPasses(tmp_path, [])
    passes.finish(1, 1, 1, 1, 1)
    assert [row[3] for row in attention_rows(passes.db)] == [
        "the last 5 cleanup passes failed: pass 5"]
    state_dir = tmp_path / "state"
    clean = tmp_path / "clean.log"
    clean.write_text(finished_pass(0))
    (state_dir / "cleanup.log.1").unlink()
    (state_dir / "cleanup.log.1").symlink_to(clean)
    (state_dir / "cleanup.log").write_bytes(f"{STARTED_LINE}\n".encode() + b"\xff\xfe broke\n")

    passes.finish(1)

    assert [row[3] for row in attention_rows(passes.db)] == [
        "the last 5 cleanup passes failed: \ufffd\ufffd broke"
    ], "the symlinked clean pass is skipped and the undecodable pass counts"


def set_state(db: Path, job_id: str, job_state: str) -> None:
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE jobs SET state = ? WHERE id = ?", (job_state, job_id))


def test_the_sweep_removes_finished_jobs_containers_and_keeps_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, queue = make_queue(tmp_path)
    docker = Docker(tmp_path)
    monkeypatch.setenv("PATH", docker.path())
    jobs = {}
    for job_state in ("done", "failed", "cancelled", "running", "queued"):
        jobs[job_state] = enqueue(queue, job_state)
        set_state(db, jobs[job_state], job_state)
        docker.start_for(jobs[job_state])
    unknown = "f" * 32
    docker.start_for(unknown)
    docker.start(**{"factory.job": "not a job"})
    docker.start(app="web")
    log: list[str] = []
    waiter, now = sweeping_waiter(tmp_path, queue, db, log)

    waiter._reap()
    assert docker.calls() == []
    now[0] = waiter_module.CONTAINER_SWEEP_SECONDS
    waiter._reap()
    assert sorted(docker.labelled()) == sorted(
        [jobs["running"], jobs["queued"], unknown, "not a job", ""]
    )
    assert sum(f"of finished job {jobs['done']}" in line for line in log) == 1
    assert sum(f"of job {unknown}: not in orch.db" in line for line in log) == 1
    assert sum("'not a job': not a job id" in line for line in log) == 1

    calls = len(docker.calls())
    now[0] += waiter_module.CONTAINER_SWEEP_SECONDS - 1
    waiter._reap()
    assert len(docker.calls()) == calls
    lines = len(log)
    now[0] += 1
    waiter._reap()
    assert len(docker.calls()) > calls
    assert len(log) == lines

    set_state(db, jobs["running"], "done")
    now[0] += waiter_module.CONTAINER_SWEEP_SECONDS
    waiter._reap()
    assert sorted(docker.labelled()) == sorted([jobs["queued"], unknown, "not a job", ""])


def test_a_stopped_docker_daemon_is_logged_once_and_the_step_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, queue = make_queue(tmp_path)
    docker = Docker(tmp_path)
    monkeypatch.setenv("PATH", docker.path())
    job_id = enqueue(queue, "done")
    set_state(db, job_id, "done")
    docker.start_for(job_id)
    docker.down().touch()
    log: list[str] = []
    waiter, now = sweeping_waiter(tmp_path, queue, db, log)
    for _ in range(2):
        now[0] += waiter_module.CONTAINER_SWEEP_SECONDS
        waiter._reap()
    assert sum("could not list factory.job containers on" in line for line in log) == 1
    docker.down().unlink()
    now[0] += waiter_module.CONTAINER_SWEEP_SECONDS
    waiter._reap()
    assert docker.labelled() == []


def test_the_sweep_keeps_a_container_a_requeued_run_starts_after_the_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = Docker(tmp_path)
    monkeypatch.setenv("PATH", docker.path())
    job_id = "a" * 32
    old = [docker.start_for(job_id), docker.start_for(job_id)]
    started: list[str] = []

    def requeued_and_restarted(asked: str) -> str:
        assert asked == job_id
        started.append(docker.start_for(job_id))
        return "failed"

    lines = containers.sweep(containers.local_runner(), requeued_and_restarted, "box")
    assert docker.ids() == set(started)
    assert not set(old) & docker.ids()
    assert lines == [f"removed 2 container(s) on box of finished job {job_id}"]


def test_the_sweep_never_puts_a_listed_id_that_is_not_hex_into_a_script() -> None:
    job_id = "a" * 32
    scripts: list[str] = []

    def runner(script: str) -> Called:
        scripts.append(script)
        return Called(0, f"$(touch\tpwned) {job_id}\n0123456789ab {job_id}\n", "")

    lines = containers.sweep(runner, lambda _: "done", "box")
    assert lines == [
        "kept 1 factory.job container(s) on box: not a container id",
        f"removed 1 container(s) on box of finished job {job_id}",
    ]
    assert scripts[1].endswith("docker rm -f -v 0123456789ab >/dev/null\n")


@pytest.mark.parametrize("enabled", [True, False])
def test_the_sweep_reaches_a_remote_device_over_the_bots_own_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    from tests.test_remote_start import Ssh
    from tests.test_waiter_remote import remote_machines

    ssh = Ssh(tmp_path)
    docker = Docker(tmp_path)
    for name, value in ssh.env().items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", f"{ssh.bin}{os.pathsep}{Docker(tmp_path / 'box').path('/usr/bin:/bin')}")
    monkeypatch.setenv("FAKE_SSH_HOME", str(docker.login_home()))
    db, queue = make_queue(tmp_path)
    config, findings = load_machines_yml(remote_machines(tmp_path, pc=enabled))
    assert config is not None, findings
    done, running = enqueue(queue, "done"), enqueue(queue, "running")
    set_state(db, done, "done")
    set_state(db, running, "running")
    docker.start_for(done)
    docker.start_for(running)
    log: list[str] = []
    waiter, now = sweeping_waiter(tmp_path, queue, db, log, pool=DevicePool(queue, config))
    now[0] = waiter_module.CONTAINER_SWEEP_SECONDS
    waiter._reap()
    assert docker.labelled() == [running]
    assert any(f"on pc of finished job {done}" in line for line in log), log
    assert {(call["alias"], call["user"]) for call in ssh.calls()} == {("fakepc-claude-bot", "claude-bot")}


STACK_REPO = "aut0-code/aut0"
TICKET_FINISHED_AT = 1780272000.0


def ticket_job(db: Path, queue: SQLiteJobQueue, key: str, job_state: str, ticket: str = "AUT-2129") -> str:
    job_id = enqueue(queue, key, repo=STACK_REPO, ticket_id=ticket)
    finished_at = TICKET_FINISHED_AT if job_state in containers.FINISHED_STATES else None
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE jobs SET state = ?, finished_at = ? WHERE id = ?", (job_state, finished_at, job_id)
        )
    return job_id


def remote_stack_sweep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from tests.test_remote_start import Ssh
    from tests.test_waiter_remote import remote_machines

    ssh = Ssh(tmp_path)
    docker = Docker(tmp_path)
    box_docker = Docker(tmp_path / "box")
    for name, value in ssh.env().items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", f"{ssh.bin}{os.pathsep}{box_docker.path('/usr/bin:/bin')}")
    monkeypatch.setenv("FAKE_SSH_HOME", str(docker.login_home()))
    db, queue = make_queue(tmp_path)
    config, findings = load_machines_yml(remote_machines(tmp_path))
    assert config is not None, findings
    log: list[str] = []
    waiter, now = sweeping_waiter(tmp_path, queue, db, log, pool=DevicePool(queue, config))
    now[0] = waiter_module.CONTAINER_SWEEP_SECONDS
    return docker, db, queue, waiter, log


def box_stack_sweep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db, queue = make_queue(tmp_path)
    docker = Docker(tmp_path)
    monkeypatch.setenv("PATH", docker.path())
    log: list[str] = []
    waiter, now = sweeping_waiter(tmp_path, queue, db, log)
    now[0] = waiter_module.CONTAINER_SWEEP_SECONDS
    return docker, db, queue, waiter, log, now


def test_an_unlabelled_stack_of_a_finished_ticket_is_removed_on_its_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker, db, queue, waiter, log = remote_stack_sweep(tmp_path, monkeypatch)
    ticket_job(db, queue, "built", "done")
    ticket_job(db, queue, "reviewed", "failed")
    docker.start_stack("aut0-aut2129-pc")

    waiter._reap()

    assert (docker.ids(), docker.networks(), docker.volumes()) == (set(), [], [])
    assert sum("removed compose project aut0-aut2129-pc on pc" in line for line in log) == 1, log


@pytest.mark.parametrize(
    ("name", "jobs", "created", "kept_because"),
    [
        pytest.param("aut0-aut2129-pc", {"AUT-2129": ("done", "running")}, CREATED,
                     "AUT-2129 has an unfinished job", id="a job of its ticket runs"),
        pytest.param("aut0-aut2130-pc", {"AUT-2129": ("done",), "AUT-2130": ("done", "queued")}, CREATED,
                     "AUT-2130 has an unfinished job", id="its ticket has a queued job"),
        pytest.param("aut0-aut2129-mac", {"AUT-2129": ("done",)}, CREATED, None, id="named for another device"),
        pytest.param("aut0-aut2129-pc", {"AUT-2129": ("done",)}, "2026-07-01 00:00:00 +0000 UTC",
                     "newer than AUT-2129's last finish", id="started after its ticket finished"),
    ],
)
def test_an_unlabelled_stack_something_could_still_use_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name, jobs, created, kept_because
) -> None:
    docker, db, queue, waiter, log = remote_stack_sweep(tmp_path, monkeypatch)
    for ticket, states in jobs.items():
        for index, job_state in enumerate(states):
            ticket_job(db, queue, f"{ticket}-{index}", job_state, ticket)
    started = docker.start_stack(name, created=created)

    waiter._reap()

    assert (docker.ids(), docker.networks(), docker.volumes()) == ({started}, [f"{name}_default"], [f"{name}_db"])
    stack_lines = [line for line in log if f"project {name}" in line]
    assert stack_lines == ([f"kept compose project {name} on pc: {kept_because}"] if kept_because else []), log


def test_a_labelled_stack_container_stays_with_the_job_label_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker, db, queue, waiter, log, _ = box_stack_sweep(tmp_path, monkeypatch)
    box = orch_host(waiter.pool.config)
    done = ticket_job(db, queue, "built", "done")
    docker.start_stack(stack_name(STACK_REPO, "AUT-2129", box), **{"factory.job": done})

    waiter._reap()

    assert docker.ids() == set()
    assert sum(f"removed 1 container(s) on {box} of finished job {done}" in line for line in log) == 1, log
    assert not any("compose project" in line for line in log), log


def test_a_supabase_stack_of_a_finished_ticket_is_removed_on_the_box(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker, db, queue, waiter, log, _ = box_stack_sweep(tmp_path, monkeypatch)
    box = orch_host(waiter.pool.config)
    ticket_job(db, queue, "built", "done")
    name = stack_name(STACK_REPO, "AUT-2129", box)
    docker.start_stack(name, "supabase")

    waiter._reap()

    assert (docker.ids(), docker.networks(), docker.volumes()) == (set(), [], [])
    assert [line for line in log if f"project {name}" in line] == [
        f"removed supabase project {name} on {box} (ticket AUT-2129 finished)"
    ], log


def test_an_unreadable_orch_db_keeps_every_stack_and_says_so_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker, db, queue, waiter, log, now = box_stack_sweep(tmp_path, monkeypatch)
    box = orch_host(waiter.pool.config)
    ticket_job(db, queue, "built", "done")
    name = stack_name(STACK_REPO, "AUT-2129", box)
    started = docker.start_stack(name)
    with sqlite3.connect(db) as connection:
        connection.execute("ALTER TABLE jobs RENAME TO jobs_elsewhere")

    waiter._reap()
    now[0] += waiter_module.CONTAINER_SWEEP_SECONDS
    waiter._reap()

    assert (docker.ids(), docker.networks(), docker.volumes()) == ({started}, [f"{name}_default"], [f"{name}_db"])
    assert sum("orch.db is unreadable" in line for line in log) == 1, log


@pytest.mark.parametrize("replaced", ["never", "after-scratch", "before-scratch"])
def test_a_killed_runner_leaves_the_scratch_of_its_live_harness_until_it_ends(
    tmp_path: Path, replaced: str
) -> None:
    from tests.test_job_run import _orch

    db, queue = make_queue(tmp_path)
    job_id = enqueue(queue, "live-scratch", head=None)
    waiter = make_waiter(tmp_path, queue, db, FakeSpawn())
    assert waiter.pool.claim("claude")[0].id == job_id
    job_dir = tmp_path / "state" / "jobs" / job_id
    ready, release, result = (tmp_path / name for name in ("ready", "release", "result"))
    if replaced == "before-scratch":
        waiter = make_waiter(tmp_path, queue, db, FakeSpawn())
        waiter.step()
    harness = (
        "import os, pathlib, time\n"
        "scratch = pathlib.Path(os.environ['TMPDIR'])\n"
        "(scratch / 'needed').write_text('scratch')\n"
        f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid()))\n"
        f"while not pathlib.Path({str(release)!r}).exists(): time.sleep(0.02)\n"
        f"pathlib.Path({str(result)!r}).write_text((scratch / 'needed').read_text())\n"
    )
    runner = _orch("job", "run", "claude-bot", job_id, "--db", str(db), "--machines", str(MACHINES),
                   "--", sys.executable, "-c", harness,
                   env={"ORCH_JOB_STATE_DIR": str(job_dir), "ORCH_TELEMETRY": "off"})
    worker = None
    try:
        _wait_for(ready.exists, 10, runner)
        worker = int(ready.read_text())
        if replaced != "before-scratch":
            waiter._children[job_id] = (runner, None)
        runner.kill()
        runner.wait(5)
        waiter.step()
        assert alive(worker)
        assert state(db, job_id) == "running"
        assert (job_dir / "tmp" / "needed").is_file()
        if replaced == "after-scratch":
            waiter = make_waiter(tmp_path, queue, db, FakeSpawn())
            waiter.step()
            assert (job_dir / "tmp" / "needed").is_file()
        release.touch()
        _wait_for(lambda: result.exists() and not alive(worker), 10)
        waiter.step()
        assert result.read_text() == "scratch"
        assert not (job_dir / "tmp").exists()
    finally:
        release.touch()
        if runner.poll() is None:
            runner.kill()
            runner.wait(5)
        if worker and alive(worker):
            os.kill(worker, signal.SIGKILL)


def test_a_job_with_no_standard_brief_is_failed_for_t6_and_its_slot_freed(
    tmp_path: Path,
) -> None:
    db, queue = make_queue(tmp_path)
    job_id = enqueue(queue, "a", action="clarify", detail=None)
    spawn = FakeSpawn()
    log: list[str] = []
    waiter = make_waiter(tmp_path, queue, db, spawn, log=log)
    assert waiter.step() is None
    assert spawn.calls == []
    assert state(db, job_id) == "failed"
    assert slots(db) == {}
    assert any("clarify" in line for line in log)


def test_a_spawn_that_fails_fails_the_job_for_t6_and_frees_its_slot(tmp_path: Path) -> None:
    db, queue = make_queue(tmp_path)
    job_id = enqueue(queue, "a")
    spawn = FakeSpawn(error=OSError("no such file"))
    waiter = make_waiter(tmp_path, queue, db, spawn)
    assert waiter.step() is None
    assert state(db, job_id) == "failed"
    assert slots(db) == {}


def test_factory_md_context_reaches_the_brief(tmp_path: Path) -> None:
    from lib.factory_md import parse_factory_context

    parsed, _ = parse_factory_context(FACTORY_MD)
    db, queue = make_queue(tmp_path)
    enqueue(queue, "a")
    spawn = FakeSpawn()
    waiter = make_waiter(
        tmp_path, queue, db, spawn, context=FakeContext(ContextResult(parsed, "dev", None))
    )
    waiter.step()
    text = spawn.stdin[0].decode("utf-8")
    assert "owner/repo factory.md at dev" in text
    assert "Never apply migrations" in text


def test_the_autostart_waiter_claims_remote_slots_too(tmp_path: Path) -> None:
    # Issue #85: a remote job's harness ends with an ORCH-HANDOFF line and the
    # waiter runs `orch job done` on the box, so remote devices are claimed
    # too (tests/test_waiter_remote.py covers the remote run itself).
    machines = tmp_path / "machines.yml"
    machines.write_text(
        "devices:\n"
        "  - name: box\n    local: true\n    slot_max: 1\n"
        "    harnesses: {claude: claude-bot}\n"
        "  - name: pc\n    tailscale_name: pc.example.ts.net\n    slot_max: 3\n"
        "    harnesses: {claude: claude-bot}\n"
        "    workdir_root: /home/{bot}/factory-work\n    ssh_alias: pc-{bot}\n"
        "    factory_root: /home/{bot}/factory\n",
        encoding="utf-8",
    )
    db, queue = make_queue(tmp_path)
    first = enqueue(queue, "a")
    second = enqueue(queue, "b")
    spawn = FakeSpawn()
    config, findings = load_machines_yml(machines)
    assert config is not None, findings
    waiter = make_waiter(tmp_path, queue, db, spawn, machines=machines)
    waiter.pool = DevicePool(queue, config)
    assert waiter.step() == first
    assert waiter.step() == second
    # Lowest utilization, ties off-box (issue #223): pc on the 0% tie, then
    # the box at 0/1 beats the pc at 1/3.
    assert slots(db) == {first: "pc", second: "box"}


def test_serve_runs_until_stopped_and_leaves_running_jobs_alone(tmp_path: Path) -> None:
    db, queue = make_queue(tmp_path)
    job_id = enqueue(queue, "a")
    spawn = FakeSpawn()
    log: list[str] = []
    waiter = make_waiter(tmp_path, queue, db, spawn, log=log)
    stop = threading.Event()
    thread = threading.Thread(target=waiter.serve, args=(stop,), kwargs={"poll": 0.01})
    thread.start()
    deadline = time.monotonic() + 5
    while not spawn.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    later = enqueue(queue, "b")
    while len(spawn.calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    thread.join(5)
    assert not thread.is_alive()
    assert [call[0][5] for call in spawn.calls] == [job_id, later]
    assert any("leaving 2 running" in line for line in log)


# --- no command injection from job fields -----------------------------------

HOSTILE = [
    "$(touch /tmp/factory-pwned)",
    "`touch /tmp/factory-pwned`",
    "; touch /tmp/factory-pwned #",
    "| sh",
    "--worktree",
    "--help",
    "&& id",
    "a\nb",
    "' \" \\",
    "${IFS}",
]


def test_no_job_field_reaches_the_argv_or_the_environment(tmp_path: Path) -> None:
    db, queue = make_queue(tmp_path)
    spawn = FakeSpawn()
    waiter = make_waiter(tmp_path, queue, db, spawn)
    for index, value in enumerate(HOSTILE):
        enqueue(
            queue,
            f"k{index}",
            repo=f"owner/repo{index}",
            ticket_id=value,
            source_comment_id=value,
            head=value,
            detail=json.dumps({"brief": value, "instruction": value, "pr_number": value,
                               "risk": value, "round": value, "base_branch": value}),
        )
        started = waiter.step()
        assert started is not None
        # The harness runs `orch job done`, then `orch job run` frees the box
        # slot once its command ends. (A running job with no slot would still
        # count on the box: issue #90.)
        queue.finish(started, owner="claude", handoff_comment_id="1")
        waiter.pool.release(started, owner="claude")
    assert len(spawn.calls) == len(HOSTILE)
    for (argv, kwargs), value in zip(spawn.calls, HOSTILE):
        assert argv[:5] == ["python3", "/factory/bin/orch", "job", "run", "claude-bot"]
        assert argv[6:] == ["--db", str(db), "--machines", str(MACHINES), "--worktree",
                            "--", "fake-harness", "-p"]
        assert re.fullmatch(r"[0-9a-f]{32}", argv[5])  # the job id, a plain token
        assert all(value not in item for item in kwargs["env"].values())
        assert kwargs.get("shell", False) is False
    # The text is in the brief, as data.
    assert any(HOSTILE[0].encode() in data for data in spawn.stdin)
    assert not Path("/tmp/factory-pwned").exists()


def test_a_job_row_with_a_hostile_id_is_failed_and_never_started(tmp_path: Path) -> None:
    db, queue = make_queue(tmp_path)
    job_id = enqueue(queue, "a")
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE jobs SET id = '--worktree' WHERE id = ?", (job_id,))
    spawn = FakeSpawn()
    waiter = make_waiter(tmp_path, queue, db, spawn)
    assert waiter.step() is None
    assert spawn.calls == []
    assert state(db, "--worktree") == "failed"


def test_waiter_and_brief_name_no_harness_and_use_no_shell() -> None:
    sources = [ROOT / "lib" / "waiter.py", *sorted((ROOT / "lib" / "brief").glob("*.py"))]
    assert len(sources) > 1
    for path in sources:
        name = path.name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert not literals & set(HARNESS_BINARIES), name
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "shell":
                assert isinstance(node.value, ast.Constant) and node.value.value is False
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                assert f"{node.value.id}.{node.attr}" not in ("os.system", "os.popen"), name
    for path in sources[1:]:
        assert "subprocess" not in _imports(path), path.name


# --- factory.md is read from the trusted base branch, without tokens --------


class FakeRun:
    def __init__(self, stdout: str = "", returncode: int = 0, error: Exception | None = None):
        self.calls: list[tuple[list[str], dict]] = []
        self.stdout = stdout
        self.returncode = returncode
        self.error = error

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, "")


def orch_config(tmp_path: Path):
    path = tmp_path / "orch.yml"
    path.write_text(
        "poll_seconds: 60\n"
        "trusted_logins:\n  maxharris1: [grok-bot, claude-bot, codex-bot, cursor-bot]\n"
        "projects:\n  - repo: owner/repo\n    factory_ref: dev\n",
        encoding="utf-8",
    )
    config, findings = load_orch_yml(path)
    assert config is not None, findings
    return config


def contents(text: str) -> str:
    return json.dumps(
        {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}
    )


def test_factory_md_is_read_from_factory_ref_with_gh_tokens_unset(tmp_path: Path) -> None:
    run = FakeRun(stdout=contents(FACTORY_MD))
    environ = {"PATH": "/usr/bin", "GH_TOKEN": "x", "GITHUB_TOKEN": "y",
               "GH_ENTERPRISE_TOKEN": "z", "GITHUB_ENTERPRISE_TOKEN": "w", "HOME": "/h"}
    source = GhContextSource(orch_config(tmp_path), run=run, environ=environ)
    result = source.load("owner/repo")
    assert result.context is not None and result.ref == "dev" and result.note is None
    assert "Never apply migrations" in result.context.body
    argv, kwargs = run.calls[0]
    assert argv == ["gh", "api", "-X", "GET", "repos/owner/repo/contents/factory.md",
                    "-f", "ref=dev"]
    assert kwargs.get("shell", False) is False
    assert not {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN",
                "GITHUB_ENTERPRISE_TOKEN"} & set(kwargs["env"])
    assert kwargs["timeout"] > 0


def test_factory_md_lookup_is_case_insensitive_on_the_repo(tmp_path: Path) -> None:
    run = FakeRun(stdout=contents(FACTORY_MD))
    result = GhContextSource(orch_config(tmp_path), run=run, environ={}).load("Owner/Repo")
    assert result.context is not None


def test_an_unknown_factory_md_field_still_reaches_the_brief(tmp_path: Path) -> None:
    text = FACTORY_MD.replace("base_branch: dev", "base_branch: dev\nnew_field: [x]")
    assert "new_field" in text
    run = FakeRun(stdout=contents(text))
    result = GhContextSource(orch_config(tmp_path), run=run, environ={}).load("owner/repo")
    assert result.context is not None and result.note is None
    assert "Never apply migrations" in result.context.body


@pytest.mark.parametrize(
    ("run", "repo", "note"),
    [
        (FakeRun(stdout=contents(FACTORY_MD)), "other/repo", "not in orch.yml"),
        (FakeRun(returncode=1), "owner/repo", "gh api exited 1"),
        (FakeRun(error=subprocess.TimeoutExpired(["gh"], 30)), "owner/repo", "TimeoutExpired"),
        (FakeRun(error=FileNotFoundError("gh")), "owner/repo", "FileNotFoundError"),
        (FakeRun(stdout="not json"), "owner/repo", "not base64"),
        (FakeRun(stdout=contents("no front matter")), "owner/repo", "front matter"),
        (FakeRun(stdout=contents(FACTORY_MD.replace("base_branch: dev", "base_branch: main"))),
         "owner/repo", "base_branch"),
    ],
)
def test_a_factory_md_that_cannot_be_read_gives_a_note_not_a_crash(
    tmp_path: Path, run: FakeRun, repo: str, note: str
) -> None:
    result = GhContextSource(orch_config(tmp_path), run=run, environ={}).load(repo)
    assert result.context is None
    assert note in (result.note or "")


@pytest.mark.parametrize("repo", ["owner", "owner/repo/x", "-x/y", "o/$(id)", "../x", "o/r?ref=x"])
def test_an_odd_repo_name_is_never_sent_to_gh(tmp_path: Path, repo: str) -> None:
    run = FakeRun(stdout=contents(FACTORY_MD))
    result = GhContextSource(orch_config(tmp_path), run=run, environ={}).load(repo)
    assert result.context is None
    assert run.calls == []


# --- the dispatcher and `orch poll` still never exec a harness --------------


def _imports(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def test_dispatcher_and_poller_modules_do_not_import_the_waiter_or_job_run() -> None:
    for name in ("dispatcher.py", "poll_source.py", "device_monitor.py", "job_state.py",
                 "grok_delivery.py", "linear_webhook.py"):
        imports = _imports(ROOT / "lib" / name)
        assert not {"lib.waiter", "lib.job_run", "lib.brief", "lib.placed_run"} & imports, name
        assert "lib.waiter" not in imports
    dispatcher_imports = _imports(ROOT / "lib" / "dispatcher.py")
    assert "subprocess" not in dispatcher_imports


def test_orch_poll_never_reaches_the_waiter() -> None:
    tree = ast.parse((ROOT / "lib" / "cli.py").read_text(encoding="utf-8"))
    functions = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    for name in ("_poll", "_runtime", "_init_db"):
        names = {
            n.id for n in ast.walk(functions[name]) if isinstance(n, ast.Name)
        } | {
            n.attr for n in ast.walk(functions[name]) if isinstance(n, ast.Attribute)
        }
        assert not {"Waiter", "waiter", "job_run", "serve_autostart"} & names, name


def test_the_dispatcher_queues_a_harness_job_and_starts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "harness-ran"
    for name in (*HARNESS_BINARIES, "orch"):
        script = bin_dir / name
        script.write_text(f"#!/bin/sh\necho {name} >> {marker}\n", encoding="utf-8")
        script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    started: list[list[str]] = []
    real_popen = subprocess.Popen

    def recording_popen(argv, *args, **kwargs):
        started.append(list(argv) if not isinstance(argv, str) else [argv])
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", recording_popen)
    monkeypatch.setattr(os, "execvp", lambda *a: started.append(list(a)))
    monkeypatch.setattr(os, "execv", lambda *a: started.append(list(a)))
    db = tmp_path / "orch.db"
    SQLiteJobQueue(db, clock=time.time)
    dispatcher = dispatcher_for(db)
    dispatcher.process(tracked(Comment(id="go-1", author="grok-login", body=GO, created_at=1)))
    dispatcher.reconcile_jobs()
    with sqlite3.connect(db) as connection:
        rows = connection.execute("SELECT recipient, action, state FROM jobs").fetchall()
    assert ("claude", "build", "queued") in rows
    assert started == []
    assert not marker.exists()


# --- end to end: `orch wait <bot> --autostart`, a fake harness, no messages --

FAKE_HARNESS = f"#!{sys.executable}\n" + """# Stands in for the bot's harness: records what it was given, then waits for
# the test to let it finish the job the way the brief says.
import json, os, subprocess, sys, time
from pathlib import Path

if sys.argv[1:] == ["--version"]:
    print("fake-harness 1.0.0")
    sys.exit(0)
RECORDED_ENV = ("ORCH_JOB_PROOF", "ORCH_JOB_ID", "ORCH_JOB_BOT", "ORCH_DB",
                "ORCH_JOB_BRIEF", "ORCH_JOB_WORKTREE")
out = Path(os.environ["FAKE_HARNESS_DIR"])
job = os.environ["ORCH_JOB_ID"]
(out / f"{job}.json").write_text(json.dumps({
    "argv": sys.argv[1:],
    "stdin": sys.stdin.read(),
    "brief_env": os.environ.get("ORCH_JOB_BRIEF"),
    "cwd": os.getcwd(),
    "job_bot": os.environ.get("ORCH_JOB_BOT"),
    "db": os.environ.get("ORCH_DB"),
    # Only what the run hands the harness, never the whole environment.
    "env": {name: value for name, value in os.environ.items() if name in RECORDED_ENV},
}))
go = out / f"{job}.go"
crash = out / f"{job}.crash"
deadline = time.time() + 60
while not go.exists() and not crash.exists() and time.time() < deadline:
    time.sleep(0.05)
if crash.exists():
    sys.exit(1)  # a harness that dies before `orch job done`
linger = out / f"{job}.linger"
if linger.exists():
    time.sleep(float(linger.read_text()))
subprocess.run([sys.executable, os.environ["FAKE_ORCH"], "job", "done",
                os.environ["ORCH_JOB_BOT"], job, "--handoff", subprocess.check_output([sys.executable, os.environ["FAKE_POST_HANDOFF"], os.environ["ORCH_DB"], job], text=True).strip()], check=True)
"""

FAKE_GH = f"#!{sys.executable}\n" + """import base64, json, os, sys
from pathlib import Path
if not any(part.endswith("/contents/factory.md") for part in sys.argv[1:]):
    os.execv(sys.executable, [sys.executable, os.environ["FAKE_GH_HANDOFFS"], *sys.argv[1:]])
log = Path(os.environ["FAKE_GH_LOG"])
with log.open("a") as handle:
    handle.write(json.dumps({"argv": sys.argv[1:],
                             "token": bool(os.environ.get("GH_TOKEN"))}) + "\\n")
text = Path(os.environ["FAKE_GH_FACTORY"]).read_text()
print(json.dumps({"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}))
"""


def _write_exe(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def _git_remote(tmp_path: Path) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    work = tmp_path / "src"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True, env=env)
    (work / "README").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True, env=env)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", "init"], check=True, env=env)
    bare = tmp_path / "remotes" / "owner" / "repo.git"
    bare.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True, env=env)
    return f"file://{tmp_path}/remotes/{{repo}}.git"


def _wait_for(predicate, seconds: float = 20.0, proc: subprocess.Popen | None = None):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        if proc is not None and proc.poll() is not None:
            raise AssertionError(f"waiter exited {proc.returncode}: {proc.stderr.read()}")
        time.sleep(0.05)
    raise AssertionError("timed out")


@pytest.fixture
def autostart(tmp_path: Path):
    harness_dir = tmp_path / "harness"
    harness_dir.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    harness = _write_exe(bin_dir / "fake-harness", FAKE_HARNESS)
    _write_exe(bin_dir / "gh", FAKE_GH)
    factory_md = tmp_path / "factory.md"
    factory_md.write_text(FACTORY_MD.replace("base_branch: dev", "base_branch: main"),
                          encoding="utf-8")
    orch_yml = tmp_path / "orch.yml"
    orch_yml.write_text(
        "poll_seconds: 60\n"
        "trusted_logins:\n  maxharris1: [grok-bot, claude-bot, codex-bot, cursor-bot]\n"
        "projects:\n  - repo: owner/repo\n    factory_ref: main\n",
        encoding="utf-8",
    )
    config = write_config(
        tmp_path / "waiter-claude-bot.yml",
        f"bot: claude-bot\ncommand:\n  - {harness}\n  - --from-config\n",
    )
    db = tmp_path / "orch.db"
    queue = SQLiteJobQueue(db, clock=time.time)
    poller_adopts(queue, MACHINES)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "PYTHONUNBUFFERED": "1",
        "FAKE_HARNESS_DIR": str(harness_dir),
        "FAKE_ORCH": str(ORCH),
        "FAKE_GH_LOG": str(tmp_path / "gh.log"),
        "FAKE_GH_FACTORY": str(factory_md),
        "GH_TOKEN": "must-not-reach-gh",
        "ORCH_WORK_ROOT": str(tmp_path / "work"),
        "ORCH_GIT_URL_TEMPLATE": _git_remote(tmp_path),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    env.pop("ORCH_DB", None)
    before = descendants(os.getpid())
    proc = subprocess.Popen(
        [sys.executable, str(ORCH), "wait", "claude-bot", "--autostart",
         "--db", str(db), "--machines", str(MACHINES), "--orch", str(orch_yml),
         "--harness-config", str(config), "--state-dir", str(tmp_path / "state")],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        yield {"proc": proc, "db": db, "queue": queue, "harness_dir": harness_dir,
               "gh_log": tmp_path / "gh.log", "harness": harness}
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
        runs = descendants(os.getpid()) - before
        deadline = time.monotonic() + 60
        while any(alive(pid) for pid in runs) and time.monotonic() < deadline:
            for go in harness_dir.glob("*.json"):
                (harness_dir / (go.stem + ".go")).touch()
            time.sleep(0.05)


def _running(db: Path, job_id: str) -> bool:
    with sqlite3.connect(db) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        placements = read_placements(connection)
    placement = placements.get(job_id)
    if row is None or row["state"] != "running" or placement is None:
        return False
    if placement.runner_pid is None:
        return False
    return derive(
        last_activity_at=float(row["last_activity_at"]),
        now=time.time(),
        placement=placement,
        stale_seconds=120,
    ) == RUNNING and float(row["last_activity_at"]) > placement.placed_at


def test_a_queued_job_goes_from_claimed_to_running_with_a_heartbeat_and_no_message(
    autostart: dict,
) -> None:
    proc, db, queue = autostart["proc"], autostart["db"], autostart["queue"]
    started_at = time.monotonic()
    job_id = enqueue(queue, "a", head=None)

    _wait_for(lambda: _running(db, job_id), 20, proc)
    assert time.monotonic() - started_at < 20

    record = json.loads(
        _wait_for(lambda: (autostart["harness_dir"] / f"{job_id}.json").exists()
                  and (autostart["harness_dir"] / f"{job_id}.json").read_text())
    )
    # Exactly the configured command, the brief on stdin and as a file.
    assert record["argv"] == ["--from-config"]
    assert "# Factory job brief: implement" in record["stdin"]
    assert "owner/repo factory.md at main" in record["stdin"]
    assert "Never apply migrations" in record["stdin"]
    assert Path(record["brief_env"]).read_text(encoding="utf-8") == record["stdin"]
    assert record["job_bot"] == "claude-bot"
    assert record["db"] == str(db.absolute())
    assert "/work/" in record["cwd"]  # --worktree: a job worktree on the box

    # No bot was messaged: the only job is this one, and gh was only asked
    # for factory.md (never to post), with no token.
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    calls = [json.loads(line) for line in autostart["gh_log"].read_text().splitlines()]
    assert calls == [{"argv": ["api", "-X", "GET", "repos/owner/repo/contents/factory.md",
                               "-f", "ref=main"], "token": False}]

    # The waiter is back to waiting while the job runs: a second job starts too.
    second = enqueue(queue, "b", head=None)
    _wait_for(lambda: _running(db, second), 20, proc)
    assert proc.poll() is None

    # The harness finishes its job the way the brief says.
    (autostart["harness_dir"] / f"{job_id}.go").touch()
    _wait_for(lambda: state(db, job_id) == "done", 20, proc)
    _wait_for(lambda: job_id not in slots(db), 20, proc)


def test_the_autostart_waiter_exits_cleanly_on_sigterm_and_leaves_its_job_running(
    autostart: dict,
) -> None:
    proc, db, queue = autostart["proc"], autostart["db"], autostart["queue"]
    job_id = enqueue(queue, "a", head=None)
    _wait_for(lambda: _running(db, job_id), 20, proc)
    proc.terminate()
    assert proc.wait(10) == 0
    # `orch job run` is in its own session: the job keeps its heartbeat.
    time.sleep(0.5)
    assert state(db, job_id) == "running"
    (autostart["harness_dir"] / f"{job_id}.go").touch()
    _wait_for(lambda: state(db, job_id) == "done", 20)


def test_the_autostart_fixture_outwaits_a_run_still_finishing_at_teardown(autostart: dict) -> None:
    proc, db, queue = autostart["proc"], autostart["db"], autostart["queue"]
    job_id = enqueue(queue, "a", head=None)
    _wait_for(lambda: _running(db, job_id), 20, proc)
    (autostart["harness_dir"] / f"{job_id}.linger").write_text("5", encoding="utf-8")


def test_autostart_refuses_to_start_without_a_valid_harness_config(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    SQLiteJobQueue(db, clock=time.time)
    env = {k: v for k, v in os.environ.items() if k != "ORCH_DB"}
    result = subprocess.run(
        [sys.executable, str(ORCH), "wait", "claude-bot", "--autostart", "--db", str(db),
         "--harness-config", str(tmp_path / "missing.yml")],
        env=env, text=True, capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 2
    assert "harness config" in result.stderr
    assert result.stdout == ""


def test_plain_wait_still_prints_and_exits_without_starting_anything(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    queue = SQLiteJobQueue(db, clock=time.time)
    poller_adopts(queue, ROOT / "machines.yml")
    job_id = enqueue(queue, "a")
    env = {k: v for k, v in os.environ.items() if k != "ORCH_DB"}
    result = subprocess.run(
        [sys.executable, str(ORCH), "wait", "claude-bot", "--db", str(db)],
        env=env, text=True, capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["id"] == job_id
    with sqlite3.connect(db) as connection:
        runner = connection.execute(
            "SELECT runner_pid FROM device_slots WHERE job_id = ?", (job_id,)
        ).fetchone()[0]
    assert runner is None


def test_waiter_module_spawns_only_orch_job_run(tmp_path: Path) -> None:
    # Every process the waiter module starts is `orch job run` or `orch
    # cleanup run` (and the gh read of factory.md): the waiter never execs a harness directly.
    tree = ast.parse((ROOT / "lib" / "waiter.py").read_text(encoding="utf-8"))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        and node.value.id in ("subprocess", "os")
        and (node.attr.startswith(("exec", "spawn", "Popen", "run", "call", "check_"))
             or node.attr in ("system", "popen", "fork"))
    ]
    assert sorted(f"{c.value.id}.{c.attr}" for c in calls) == ["subprocess.Popen", "subprocess.run"]
    assert waiter_module.GH_ARGV_PREFIX == ("gh", "api", "-X", "GET")


def test_spec_states_the_new_harness_rule_and_keeps_the_poller_rule() -> None:
    spec = (ROOT / "docs" / "SPEC.md").read_text(encoding="utf-8")
    row = next(line for line in spec.splitlines()
               if line.startswith("| Only the owning harness bot starts that harness |"))
    assert "The dispatcher and `orch poll` exec no harness" in row
    assert "running as the owning bot's own user and login, may start that bot's harness" in row
    assert "under `orch job run` from the standard brief plus `factory.md`" in row
    assert "no job, PR or comment text reaches a command line" in row
    security = spec[spec.index("## 10. Security"):spec.index("## 11.")]
    assert "The dispatcher and `orch poll` never exec `claude`, `codex`, or `cursor-agent`" in security
    assert "`orch wait <bot> --autostart`" in security
    assert "The dispatcher, `orch poll` and `orch wait` never exec" not in spec
    assert "Let the waiter start the harness itself".lower() in spec.lower()
    plan = (ROOT / "docs" / "PLAN.md").read_text(encoding="utf-8")
    assert "The dispatcher and `orch poll` exec no harness" in plan


def test_autostart_runs_with_only_remote_devices(tmp_path: Path) -> None:
    # Issue #85: the box is no longer required; a remote-only pool is served.
    db = tmp_path / "orch.db"
    SQLiteJobQueue(db, clock=time.time)
    config = write_config(tmp_path / "waiter.yml", "bot: claude-bot\ncommand: [x]\n")
    machines = tmp_path / "machines.yml"
    machines.write_text(
        "orch_host: pc\ndevices:\n"
        "  - name: pc\n    tailscale_name: pc.example.ts.net\n    slot_max: 3\n"
        "    harnesses: {claude: claude-bot}\n"
        "    workdir_root: /home/{bot}/factory-work\n    ssh_alias: pc-{bot}\n"
        "    factory_root: /home/{bot}/factory\n",
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if k != "ORCH_DB"}
    proc = subprocess.Popen(
        [sys.executable, str(ORCH), "wait", "claude-bot", "--autostart", "--db", str(db),
         "--machines", str(machines), "--harness-config", str(config),
         "--state-dir", str(tmp_path / "state"), "--orch", str(ROOT / "orch.yml")],
        env=env, text=True, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL,
    )
    try:
        seen = ""
        deadline = time.monotonic() + 20
        while "waiting for claude-bot jobs" not in seen and time.monotonic() < deadline:
            line = proc.stderr.readline()
            if not line:
                break
            seen += line
        assert "waiting for claude-bot jobs" in seen, seen
    finally:
        proc.terminate()
        proc.wait(timeout=20)
    assert proc.returncode == 0


def test_autostart_only_flags_need_autostart(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    SQLiteJobQueue(db, clock=time.time)
    env = {k: v for k, v in os.environ.items() if k != "ORCH_DB"}
    result = subprocess.run(
        [sys.executable, str(ORCH), "wait", "claude-bot", "--db", str(db),
         "--harness-config", str(tmp_path / "x.yml")],
        env=env, text=True, capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 2
    assert "need --autostart" in result.stderr


# --- Codex PR #82 r1 F1.1: one fd, checked with fstat; a safe directory ------


@pytest.mark.parametrize("mode", [0o770, 0o707, 0o777])
def test_a_harness_config_in_a_directory_others_can_write_is_refused(
    tmp_path: Path, mode: int
) -> None:
    folder = tmp_path / "factory"
    folder.mkdir()
    path = write_config(folder / "waiter.yml", "bot: claude-bot\ncommand: [x]\n")
    folder.chmod(mode)
    try:
        with pytest.raises(WaiterError, match="directory .* writable by group or others"):
            load_harness_command(path, "claude-bot")
    finally:
        folder.chmod(0o700)


def test_a_harness_config_in_a_private_directory_loads(tmp_path: Path) -> None:
    folder = tmp_path / "factory"
    folder.mkdir(mode=0o700)
    path = write_config(folder / "waiter.yml", "bot: claude-bot\ncommand: [x, -p]\n")
    assert load_harness_command(path, "claude-bot").command == ("x", "-p")


def test_a_fifo_harness_config_is_refused_without_blocking(tmp_path: Path) -> None:
    path = tmp_path / "waiter.yml"
    os.mkfifo(path, 0o600)
    with pytest.raises(WaiterError, match="regular file"):
        load_harness_command(path, "claude-bot")


def test_the_harness_config_is_checked_and_read_through_one_nofollow_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(tmp_path / "waiter.yml", "bot: claude-bot\ncommand: [x]\n")
    opened: list[tuple[str, int, int]] = []
    checked: list[int] = []
    real_open, real_fstat = os.open, os.fstat

    def spy_open(target, flags, *args, **kwargs):
        fd = real_open(target, flags, *args, **kwargs)
        opened.append((str(target), flags, fd))
        return fd

    def spy_fstat(fd):
        checked.append(fd)
        return real_fstat(fd)

    def no_path_read(*_args, **_kwargs):
        raise AssertionError("the config must be read from the checked fd")

    monkeypatch.setattr(waiter_module.os, "open", spy_open)
    monkeypatch.setattr(waiter_module.os, "fstat", spy_fstat)
    monkeypatch.setattr(waiter_module.os, "lstat", no_path_read)
    monkeypatch.setattr(Path, "read_text", no_path_read)
    monkeypatch.setattr(Path, "read_bytes", no_path_read)
    assert load_harness_command(path, "claude-bot").command == ("x",)
    [(target, flags, fd)] = [item for item in opened if item[0] == str(path)]
    assert flags & os.O_NOFOLLOW
    assert checked == [fd]


def test_a_harness_config_swapped_for_a_symlink_is_refused_as_a_symlink(tmp_path: Path) -> None:
    real = write_config(tmp_path / "other.yml", "bot: claude-bot\ncommand: [evil]\n")
    path = tmp_path / "waiter.yml"
    path.symlink_to(real)
    with pytest.raises(WaiterError, match="symlink"):
        load_harness_command(path, "claude-bot")


# --- Codex PR #82 r1 F1.5: a run that ends unfinished goes to T6 at once ----


def _slot(db: Path, job_id: str) -> sqlite3.Row | None:
    with sqlite3.connect(db) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            "SELECT * FROM device_slots WHERE job_id = ?", (job_id,)
        ).fetchone()


def test_a_run_that_exits_without_job_done_fails_the_job_within_one_step(
    tmp_path: Path,
) -> None:
    db, queue = make_queue(tmp_path)
    job_id = enqueue(queue, "a")
    spawn = FakeSpawn()
    log: list[str] = []
    waiter = make_waiter(tmp_path, queue, db, spawn, log=log)
    assert waiter.step() == job_id
    spawn.children[0].returncode = 1  # the harness died before `orch job done`

    waiter.step()

    assert state(db, job_id) == "failed"
    with sqlite3.connect(db) as connection:
        handled = connection.execute(
            "SELECT failure_handled, handoff_comment_id FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    assert handled == (0, None)  # left for the dispatcher's T6
    assert _slot(db, job_id) is None
    assert any("without orch job done" in line and "T6" in line for line in log)


def test_a_run_whose_harness_finished_the_job_is_left_alone(tmp_path: Path) -> None:
    db, queue = make_queue(tmp_path)
    job_id = enqueue(queue, "a")
    spawn = FakeSpawn()
    waiter = make_waiter(tmp_path, queue, db, spawn)
    waiter.step()
    queue.finish(job_id, owner="claude", handoff_comment_id="c-77")
    spawn.children[0].returncode = 0
    waiter.step()
    with sqlite3.connect(db) as connection:
        row = connection.execute(
            "SELECT state, handoff_comment_id FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    assert row == ("done", "c-77")


def test_a_job_another_live_runner_holds_is_not_failed(tmp_path: Path) -> None:
    db, queue = make_queue(tmp_path)
    job_id = enqueue(queue, "a")
    spawn = FakeSpawn()
    waiter = Waiter(
        bot="claude-bot", queue=queue, pool=local_pool(queue),
        harness=HarnessCommand(bot="claude-bot", command=("fake-harness",)),
        context=FakeContext(), state_dir=tmp_path / "state", db=db, machines=MACHINES,
        orch=("python3", "/factory/bin/orch"), spawn=spawn, log=[].append,
        environ={}, alive=lambda pid, _start: pid == 4242,
    )
    waiter.step()
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE device_slots SET runner_pid = 4242 WHERE job_id = ?", (job_id,))
    spawn.children[0].returncode = 2  # this run was refused; pid 4242 runs it
    waiter.step()
    assert state(db, job_id) == "running"
    assert _slot(db, job_id) is not None


def test_r2_f2_1_a_runner_killed_while_its_harness_lives_gets_no_attempt_2_until_both_are_gone(
    tmp_path: Path,
) -> None:
    """Codex PR #82 r2 F2.1: SIGKILL hits only `orch job run`; the harness lives.

    The waiter must not fail the job then: T6 would queue attempt 2 and,
    with a second free box slot, run it beside the live harness. The job
    stays running until the runner and the worker are both gone; then the
    poller's DEAD path fails it and T6 queues exactly one attempt 2.
    """
    from tests.test_job_state import Poller

    poller = Poller(tmp_path / "poller", box_slots=3)
    job_id = enqueue(poller.queue, "f21", timeout_seconds=3600)
    spawn = FakeSpawn()
    log: list[str] = []
    waiter = Waiter(
        bot="claude-bot", queue=poller.queue,
        pool=DevicePool(poller.queue, local_only(poller.config)),
        harness=HarnessCommand(bot="claude-bot", command=("fake-harness",)),
        context=FakeContext(), state_dir=tmp_path / "state", db=poller.path,
        machines=poller.machines, orch=("python3", "/factory/bin/orch"), spawn=spawn,
        log=log.append, environ={}, alive=lambda pid, _start: pid in poller.live,
    )
    assert waiter.step() == job_id
    runner, worker = spawn.children[0].pid, 20_001
    # `orch job run` beats, records itself, then starts the harness.
    poller.queue.heartbeat(job_id, owner="claude")
    poller.pool.attach_runner(job_id, owner="claude", pid=runner, start=None,
                              alive=lambda *_: False)
    poller.pool.attach_worker(job_id, owner="claude", pid=worker, start=None)
    poller.live |= {runner, worker}

    # SIGKILL (or the OOM killer) takes only the runner.
    poller.live.discard(runner)
    spawn.children[0].returncode = -9
    assert waiter.step() is None  # reaps the runner; nothing else to claim

    assert state(poller.path, job_id) == "running"
    assert _slot(poller.path, job_id) is not None
    poller.at(30)  # the poller reconciles: the worker still runs
    poller.at(60)
    assert poller.jobs() == [(job_id, "running", 1)]  # no failed row, no attempt 2
    assert waiter.step() is None  # nothing for a second box slot to start
    assert len(spawn.children) == 1
    assert any("still runs" in line and str(worker) in line for line in log)

    # The harness ends too: now the job is retryable, exactly once.
    poller.live.discard(worker)
    poller.at(90)
    jobs = poller.jobs()
    assert (job_id, "failed", 1) in jobs
    retries = [job for job in jobs if job[2] == 2]
    assert len(retries) == 1 and retries[0][1] == "queued"
    poller.at(120)
    assert len([job for job in poller.jobs() if job[2] == 2]) == 1
    assert waiter.step() == retries[0][0]  # attempt 2 starts only now
    assert len(spawn.children) == 2


def test_r2_f2_1_a_runner_killed_after_its_harness_ended_fails_the_job_at_once(
    tmp_path: Path,
) -> None:
    """Both gone when the waiter reaps the runner: fail it now, free the slot."""
    db, queue = make_queue(tmp_path)
    job_id = enqueue(queue, "a")
    spawn = FakeSpawn()
    live: set[int] = set()
    waiter = Waiter(
        bot="claude-bot", queue=queue, pool=local_pool(queue),
        harness=HarnessCommand(bot="claude-bot", command=("fake-harness",)),
        context=FakeContext(), state_dir=tmp_path / "state", db=db, machines=MACHINES,
        orch=("python3", "/factory/bin/orch"), spawn=spawn, log=[].append,
        environ={}, alive=lambda pid, _start: pid in live,
    )
    waiter.step()
    runner = spawn.children[0].pid
    queue.heartbeat(job_id, owner="claude")
    waiter.pool.attach_runner(job_id, owner="claude", pid=runner, start=None,
                              alive=lambda *_: False)
    waiter.pool.attach_worker(job_id, owner="claude", pid=20_002, start=None)
    spawn.children[0].returncode = -9  # neither pid is in `live`
    waiter.step()
    assert state(db, job_id) == "failed"
    assert _slot(db, job_id) is None


def test_end_to_end_a_harness_that_exits_1_without_done_fails_the_job_at_once(
    autostart: dict,
) -> None:
    proc, db, queue = autostart["proc"], autostart["db"], autostart["queue"]
    job_id = enqueue(queue, "a", head=None, timeout_seconds=3600)
    _wait_for(lambda: _running(db, job_id), 20, proc)
    started = time.monotonic()
    (autostart["harness_dir"] / f"{job_id}.crash").touch()
    _wait_for(lambda: state(db, job_id) == "failed", 20, proc)
    assert time.monotonic() - started < 20  # not the 60-minute timeout
    _wait_for(lambda: job_id not in slots(db), 20, proc)
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT handoff_comment_id, failure_handled FROM jobs WHERE id = ?", (job_id,)
        ).fetchone() == (None, 0)
    assert proc.poll() is None  # the waiter keeps waiting


# --- Codex PR #82 r1 F1.6: one waiter per bot, for its whole life -----------


def _second_waiter(autostart: dict, tmp_path: Path, *extra: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "ORCH_DB"}
    config = write_config(
        tmp_path / "second.yml", f"bot: claude-bot\ncommand: [{autostart['harness']}]\n"
    )
    return subprocess.run(
        [sys.executable, str(ORCH), "wait", "claude-bot", "--db", str(autostart["db"]),
         "--machines", str(MACHINES), *extra, "--harness-config", str(config)]
        if "--autostart" in extra else
        [sys.executable, str(ORCH), "wait", "claude-bot", "--db", str(autostart["db"]),
         "--machines", str(MACHINES)],
        env=env, text=True, capture_output=True, timeout=30, check=False,
    )


def test_a_second_autostart_waiter_for_the_bot_is_refused(
    autostart: dict, tmp_path: Path
) -> None:
    proc, db = autostart["proc"], autostart["db"]
    lock = waiter_module.lock_path(db, "claude-bot")
    _wait_for(lambda: lock.exists() and lock.read_text().strip() == str(proc.pid), 20, proc)
    second = _second_waiter(autostart, tmp_path, "--autostart",
                            "--state-dir", str(tmp_path / "state2"))
    assert second.returncode == 2
    assert f"another autostart waiter for claude-bot (pid {proc.pid})" in second.stderr
    assert proc.poll() is None
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600


def test_the_waiter_lock_frees_when_the_waiter_dies_even_with_runs_alive(
    autostart: dict, tmp_path: Path
) -> None:
    # `orch job run` children never inherit the lock (close-on-exec), so a
    # killed waiter's restart is not blocked by the jobs it left running.
    proc, db, queue = autostart["proc"], autostart["db"], autostart["queue"]
    job_id = enqueue(queue, "a", head=None)
    _wait_for(lambda: _running(db, job_id), 20, proc)
    runs = descendants(proc.pid)
    proc.kill()
    proc.wait(10)
    fd = waiter_module.hold_waiter_lock(db, "claude-bot")
    os.close(fd)
    assert state(db, job_id) == "running"
    (autostart["harness_dir"] / f"{job_id}.crash").touch()
    _wait_for(lambda: not any(alive(pid) for pid in runs), 20)


def test_hold_waiter_lock_refuses_a_second_holder_in_process(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    fd = waiter_module.hold_waiter_lock(db, "claude-bot")
    try:
        with pytest.raises(WaiterError, match="another autostart waiter"):
            waiter_module.hold_waiter_lock(db, "claude-bot")
        # Per bot: codex-bot's waiter is unaffected.
        os.close(waiter_module.hold_waiter_lock(db, "codex-bot"))
    finally:
        os.close(fd)
    os.close(waiter_module.hold_waiter_lock(db, "claude-bot"))
