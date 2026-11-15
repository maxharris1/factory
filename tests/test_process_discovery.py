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
    real_popen, real_end = subprocess.Popen, job_run.Tree.end

    def ps_denied_while_ending(args, *rest, **kwargs):
        if args[0] == "ps" and ending.is_set() and len(denied) < 3:
            denied.append(1)
            if len(denied) == 3:
                dispatcher.reconcile_jobs()
                held.append((state(), pool.placement(job_id) is not None, pool.claim("codex") is None))
            raise PermissionError("ps: Operation not permitted")
        return real_popen(args, *rest, **kwargs)

    def state() -> str:
        with queue.connect() as connection:
            return connection.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]

    def end(tree: job_run.Tree, log: job_run.Log) -> None:
        ending.set()
        real_end(tree, log)

    monkeypatch.delenv("ORCH_DB", raising=False)
    monkeypatch.setattr(job_run, "_proc_processes", lambda _tag: job_run._ps_processes())
    monkeypatch.setattr(job_run.subprocess, "Popen", ps_denied_while_ending)
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
        1,
    ],
)
def test_a_ps_that_fails_or_exits_nonzero_is_unknown_not_empty(
    monkeypatch: pytest.MonkeyPatch, outcome: object
) -> None:
    class Ps:
        pid = -1
        returncode = 1

        def __init__(self, *_args, **_kwargs):
            if isinstance(outcome, OSError):
                raise outcome

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            if isinstance(outcome, subprocess.TimeoutExpired) and timeout is not None:
                raise outcome
            return "", "ps: denied"

        def kill(self) -> None:
            pass

    monkeypatch.setattr(job_run.subprocess, "Popen", Ps)
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


def test_cleanup_by_ps_never_counts_the_ps_it_ran_as_a_run_process() -> None:
    me = os.getpid()
    before = frozenset(
        (pid, process.start)
        for pid, process in job_run._ps_processes().items()
        if process.ppid == me
    )
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    looks: list[int] = []

    def scan(_tag: str) -> dict[int, job_run.Process]:
        looks.append(1)
        assert len(looks) <= 50, "each look found the ps that made it, so cleanup never settled"
        return job_run._ps_processes()

    try:
        job_run.Tree(child.pid, "tag", scan=scan, adopts=True, others=before).end(lambda _line: None)
        assert child.wait(5) == -signal.SIGKILL
    finally:
        child.kill()
        child.wait()


class FakeProcessTable:
    def __init__(self, *processes: job_run.Process, responsible: dict[int, int]):
        self.listing = {process.pid: process for process in processes}
        self.responsible_for = responsible
        self.killed: list[int] = []

    def scan(self, _tag: str) -> dict[int, job_run.Process]:
        return dict(self.listing)

    def signal(self, pid: int, signum: int) -> None:
        if signum == signal.SIGKILL and self.listing.pop(pid, None) is not None:
            self.killed.append(pid)

    def responsible(self, pid: int) -> int | None:
        return self.responsible_for.get(pid)


ROOT = 4000
BEFORE = job_run.Process(300, 1, "before", False)
STRANGER = job_run.Process(301, 1, "stranger", False)
ORPHAN = job_run.Process(302, 1, "orphan", False)
ORPHANS_CHILD = job_run.Process(303, 302, "orphan's child", False)


def _end(machine: FakeProcessTable, **kwargs: object) -> list[str]:
    lines: list[str] = []
    job_run.Tree(
        ROOT, "tag", scan=machine.scan, signal_pid=machine.signal,
        others=frozenset({(BEFORE.pid, BEFORE.start)}), **kwargs,
    ).end(lines.append)
    return lines


def test_an_orphan_the_root_is_responsible_for_dies_with_the_run_though_never_seen() -> None:
    machine = FakeProcessTable(
        BEFORE, STRANGER, ORPHAN, ORPHANS_CHILD,
        responsible={BEFORE.pid: ROOT, STRANGER.pid: STRANGER.pid, ORPHAN.pid: ROOT,
                     ORPHANS_CHILD.pid: ROOT},
    )
    _end(machine, responsible=machine.responsible)
    assert sorted(machine.killed) == [ORPHAN.pid, ORPHANS_CHILD.pid]
    assert set(machine.listing) == {BEFORE.pid, STRANGER.pid}


def test_without_responsibility_an_unseen_orphan_is_left_alone() -> None:
    machine = FakeProcessTable(ORPHAN, responsible={ORPHAN.pid: ROOT})
    _end(machine)
    assert machine.killed == []


def test_cleanup_holds_while_responsibility_cannot_be_read() -> None:
    machine = FakeProcessTable(ORPHAN, responsible={ORPHAN.pid: ROOT})
    reads: list[int] = []

    def responsible(pid: int) -> int | None:
        reads.append(pid)
        if len(reads) <= 2:
            raise job_run.ProcessesUnknown("cannot read the responsible process of pid 302")
        return machine.responsible(pid)

    lines = _end(machine, responsible=responsible)
    assert machine.killed == [ORPHAN.pid]
    assert lines == [
        "cannot read the responsible process of pid 302; "
        "holding the job until the run's processes are confirmed gone"
    ]


def _libproc_scan() -> dict[int, job_run.Process]:
    import ctypes
    import struct

    lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    ids = (ctypes.c_int * 8192)()
    n = lib.proc_listpids(1, 0, ids, ctypes.sizeof(ids))
    if n <= 0:
        raise job_run.ProcessesUnknown("proc_listpids failed")
    found = {}
    for pid in ids[: n // 4]:
        buf = ctypes.create_string_buffer(136)
        if lib.proc_pidinfo(pid, 3, 0, buf, 136) != 136:
            continue
        fields = struct.unpack_from("5I", buf.raw)
        if fields[1] == 5:
            continue
        start = struct.unpack_from("QQ", buf.raw, 120)
        found[pid] = job_run.Process(pid, fields[4], str(start), False)
    return found


REPARENTED_WRITER = (
    "import os,sys,time; open(sys.argv[1],'w').write(str(os.getpid())); "
    "f=open(sys.argv[2],'a');\n"
    "while True: f.write('x'); f.flush(); time.sleep(.05)"
)
INTERMEDIATE = (
    "import subprocess,sys; "
    "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2],sys.argv[3]],"
    "env={},stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
    "stderr=subprocess.DEVNULL,start_new_session=True)"
)
HARNESS = (
    "import subprocess,sys,time; time.sleep(.2); "
    "subprocess.run([sys.executable,'-c',sys.argv[1],sys.argv[2],sys.argv[3],sys.argv[4]]); "
    "time.sleep(600)"
)
WATCHER = (
    "from pathlib import Path; import sys,time; p=Path(sys.argv[1]); "
    "before=p.stat().st_size; time.sleep(.3); "
    "Path(sys.argv[2]).write_text(f'{before} {p.stat().st_size}')"
)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS responsible processes")
def test_a_macos_writer_reparented_to_init_dies_before_the_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    machines = tmp_path / "machines.yml"
    machines.write_text(
        "devices:\n  - name: box\n    local: true\n    slot_max: 3\n"
        "    harnesses: {codex: codex-bot}\n"
    )
    config, findings = load_machines_yml(machines, allow_placeholders=True)
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
    pid_file, ticks, observed = tmp_path / "pid", tmp_path / "ticks", tmp_path / "observed"

    def state(job: str) -> str:
        with queue.connect() as connection:
            return connection.execute("SELECT state FROM jobs WHERE id = ?", (job,)).fetchone()[0]

    def run(job: str, *command: str) -> int:
        return cli.main([
            "job", "run", "codex-bot", job, "--db", str(queue.path),
            "--machines", str(machines), "--interval", ".2", "--", *command,
        ])

    monkeypatch.delenv("ORCH_DB", raising=False)
    monkeypatch.setattr(job_run, "_ps_processes", _libproc_scan)
    try:
        code = run(
            job_id, sys.executable, "-c", HARNESS, INTERMEDIATE, REPARENTED_WRITER,
            str(pid_file), str(ticks),
        )
        assert code == 128 + signal.SIGTERM
        assert state(job_id) == "failed" and pool.placement(job_id) is None
        dispatcher.reconcile_jobs()
        retry = pool.claim("codex")
        assert retry is not None
        assert run(retry[1].job_id, sys.executable, "-c", WATCHER, str(ticks), str(observed)) == 0
        before, after = map(int, observed.read_text().split())
        print("RETRY", code, "OLD_CHILD_TICKS", before, after)
        assert after == before, "old job child still writes during retry"
        assert not _alive(int(pid_file.read_text()))
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
