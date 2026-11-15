from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from lib import cli, job_run
from lib.device_pool import DevicePool
from lib.dispatcher import Dispatcher
from lib.machines_yml import load_machines_yml
from lib.queue import JobRequest, SQLiteJobQueue
from tests.test_dispatcher import TRUSTED_LOGINS, FakeGrokDelivery, FakeReviewActions

WRITER = (
    "import sys, time\n"
    "f = open(sys.argv[1], 'a')\n"
    "while True:\n"
    "    f.write('x'); f.flush(); time.sleep(0.05)\n"
)
LEAVES_WRITER = (
    "import subprocess, sys, time; "
    "p = subprocess.Popen([sys.executable, '-c', sys.argv[3], sys.argv[2]], "
    "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
    "open(sys.argv[1], 'w').write(str(p.pid)); time.sleep(600)"
)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _still_writes(ticks: Path) -> bool:
    before = ticks.stat().st_size
    time.sleep(0.3)
    return ticks.stat().st_size != before


def test_no_retry_starts_while_process_discovery_fails_after_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    machines = tmp_path / "machines.yml"
    machines.write_text(
        "devices:\n  - name: box\n    local: true\n    slot_max: 1\n"
        "    harnesses: {codex: codex-bot}\n"
    )
    config, findings = load_machines_yml(machines)
    assert config is not None, findings
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=time.time)
    pool = DevicePool(queue, config)
    dispatcher = Dispatcher(
        queue.path, queue, trusted_logins=TRUSTED_LOGINS,
        review_actions=FakeReviewActions(), grok_delivery=FakeGrokDelivery(),
    )
    job_id = queue.enqueue(
        JobRequest(
            repo="maxharris1/factory", ticket_id="72", action="review", recipient="codex",
            source_comment_id="ready-1", timeout_seconds=2, head="a" * 40,
            detail='{"round": "1/1", "action": "review", "pr_number": 72}',
        ),
        dedupe_key="review:72:r1",
    ).id
    claimed = pool.claim("codex")
    assert claimed is not None and claimed[1].device == "box"
    pid_file, ticks = tmp_path / "pid", tmp_path / "ticks"
    ending = threading.Event()
    denied: list[int] = []
    held: list[tuple[str, bool, bool]] = []
    real_run, real_end = subprocess.run, job_run.Tree.end

    def ps_denied_while_ending(args, *rest, **kwargs):
        if args[0] == "ps" and ending.is_set() and len(denied) < 3:
            denied.append(1)
            if len(denied) == 3:
                dispatcher.reconcile_jobs()
                held.append((state(), pool.placement(job_id) is not None, pool.claim("codex") is None))
            raise PermissionError("ps: Operation not permitted")
        return real_run(args, *rest, **kwargs)

    def state() -> str:
        with queue.connect() as connection:
            return connection.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]

    def end(tree: job_run.Tree, log: job_run.Log) -> None:
        ending.set()
        real_end(tree, log)

    monkeypatch.delenv("ORCH_DB", raising=False)
    monkeypatch.setattr(job_run, "_proc_processes", lambda _tag: job_run._ps_processes())
    monkeypatch.setattr(job_run.subprocess, "run", ps_denied_while_ending)
    monkeypatch.setattr(job_run.Tree, "end", end)
    try:
        code = cli.main([
            "job", "run", "codex-bot", job_id, "--db", str(queue.path),
            "--machines", str(machines), "--interval", "0.2", "--",
            sys.executable, "-c", LEAVES_WRITER, str(pid_file), str(ticks), WRITER,
        ])
        assert held == [("running", True, True)], "the job was settled while discovery failed"
        assert code == 128 + signal.SIGTERM
        assert not _alive(int(pid_file.read_text())) and not _still_writes(ticks)
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize(
    "outcome",
    [
        PermissionError("Operation not permitted"),
        subprocess.TimeoutExpired("ps", 30),
        subprocess.CompletedProcess(["ps"], 1, "", "ps: denied"),
    ],
)
def test_a_ps_that_fails_or_exits_nonzero_is_unknown_not_empty(
    monkeypatch: pytest.MonkeyPatch, outcome: object
) -> None:
    def ps(*_args, **_kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(job_run.subprocess, "run", ps)
    with pytest.raises(job_run.ProcessesUnknown):
        job_run._ps_processes()


def test_an_unreadable_proc_is_unknown_not_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(_path: str) -> list[str]:
        raise PermissionError("/proc")

    monkeypatch.setattr(job_run.os, "listdir", denied)
    with pytest.raises(job_run.ProcessesUnknown):
        job_run._proc_processes("tag")


def test_a_command_never_starts_when_processes_cannot_be_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unknown(_tag: str) -> dict[int, job_run.Process]:
        raise job_run.ProcessesUnknown("cannot run ps: denied")

    monkeypatch.setattr(job_run, "processes", unknown)
    ran = tmp_path / "ran"
    lines: list[str] = []
    code = job_run.run(
        [sys.executable, "-c", "import sys; open(sys.argv[1], 'w').close()", str(ran)],
        beat=lambda: None, interval=0.1, log=lines.append,
    )
    assert code == 127 and not ran.exists()
    assert lines == [f"not starting {sys.executable}: cannot run ps: denied"]


def test_cleanup_returns_only_after_a_look_that_succeeds_and_finds_none() -> None:
    looks: list[str] = []

    def scan(_tag: str) -> dict[int, job_run.Process]:
        looks.append("look")
        if len(looks) <= 3:
            raise job_run.ProcessesUnknown("cannot run ps: denied")
        return {}

    lines: list[str] = []
    job_run.Tree(-1, "tag", scan=scan).end(lines.append)
    assert len(looks) == 4
    assert lines == [
        "cannot run ps: denied; holding the job until the run's processes are confirmed gone"
    ]
