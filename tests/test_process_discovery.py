from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from lib import cli, job_run, process_tree
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
    real_popen, real_end = subprocess.Popen, process_tree.Tree.end

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

    def end(tree: process_tree.Tree, log: process_tree.Log) -> None:
        ending.set()
        real_end(tree, log)

    monkeypatch.delenv("ORCH_DB", raising=False)
    monkeypatch.setattr(process_tree, "_proc_processes", lambda _tag: process_tree._ps_processes())
    monkeypatch.setattr(process_tree.subprocess, "Popen", ps_denied_while_ending)
    monkeypatch.setattr(process_tree.Tree, "end", end)
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

    monkeypatch.setattr(process_tree.subprocess, "Popen", Ps)
    with pytest.raises(process_tree.ProcessesUnknown):
        process_tree._ps_processes()


def test_an_unreadable_proc_is_unknown_not_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(_path: str) -> list[str]:
        raise PermissionError("/proc")

    monkeypatch.setattr(process_tree.os, "listdir", denied)
    with pytest.raises(process_tree.ProcessesUnknown):
        process_tree._proc_processes("tag")


def test_a_command_never_starts_when_processes_cannot_be_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unknown(_tag: str) -> dict[int, process_tree.Process]:
        raise process_tree.ProcessesUnknown("cannot run ps: denied")

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

    def scan(_tag: str) -> dict[int, process_tree.Process]:
        looks.append("look")
        if len(looks) <= 3:
            raise process_tree.ProcessesUnknown("cannot run ps: denied")
        return {}

    lines: list[str] = []
    process_tree.Tree(-1, "tag", scan=scan).end(lines.append)
    assert len(looks) == 4
    assert lines == [
        "cannot run ps: denied; holding the job until the run's processes are confirmed gone"
    ]


def test_cleanup_by_ps_never_counts_the_ps_it_ran_as_a_run_process() -> None:
    me = os.getpid()
    before = frozenset(
        (pid, process.start)
        for pid, process in process_tree._ps_processes().items()
        if process.ppid == me
    )
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    looks: list[int] = []

    def scan(_tag: str) -> dict[int, process_tree.Process]:
        looks.append(1)
        assert len(looks) <= 50, "each look found the ps that made it, so cleanup never settled"
        return process_tree._ps_processes()

    try:
        process_tree.Tree(child.pid, "tag", scan=scan, adopts=True, others=before).end(lambda _line: None)
        assert child.wait(5) == -signal.SIGKILL
    finally:
        child.kill()
        child.wait()


class FakeProcessTable:
    def __init__(self, *processes: process_tree.Process, responsible: dict[int, int]):
        self.listing = {process.pid: process for process in processes}
        self.responsible_for = responsible
        self.killed: list[int] = []

    def scan(self, _tag: str) -> dict[int, process_tree.Process]:
        return dict(self.listing)

    def signal(self, pid: int, signum: int) -> None:
        if signum == signal.SIGKILL and self.listing.pop(pid, None) is not None:
            self.killed.append(pid)

    def responsible(self, pid: int) -> int | None:
        return self.responsible_for.get(pid)


ROOT = 4000
BEFORE = process_tree.Process(300, 1, "before", False)
STRANGER = process_tree.Process(301, 1, "stranger", False)
ORPHAN = process_tree.Process(302, 1, "orphan", False)
ORPHANS_CHILD = process_tree.Process(303, 302, "orphan's child", False)


def _end(machine: FakeProcessTable, **kwargs: object) -> list[str]:
    lines: list[str] = []
    process_tree.Tree(
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


ORPHANS_ITS_WRITER = (
    "import os, subprocess, sys; "
    "p = subprocess.Popen([sys.executable, '-c', sys.argv[3], sys.argv[2]], "
    "env={'PATH': os.environ['PATH']}, stdin=subprocess.DEVNULL, "
    "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
    "q = subprocess.Popen(['sleep', '120'], stdin=subprocess.DEVNULL); "
    "open(sys.argv[1], 'w').write(f'{p.pid} {q.pid}')"
)


def test_an_untagged_orphan_left_in_the_runs_session_dies_only_beside_a_member(
    tmp_path: Path,
) -> None:
    ticks, other = tmp_path / "ticks", tmp_path / "other"
    pid_file = tmp_path / "writer.pid"
    leader = subprocess.Popen(
        [sys.executable, "-c", ORPHANS_ITS_WRITER, str(pid_file), str(ticks), WRITER],
        start_new_session=True,
    )
    assert leader.wait(timeout=10) == 0
    writer, member = map(int, pid_file.read_text().split())
    unrelated = subprocess.Popen(
        [sys.executable, "-c", WRITER, str(other)], start_new_session=True
    )
    try:
        deadline = time.monotonic() + 10
        while not (ticks.exists() and other.exists()):
            assert time.monotonic() < deadline, "the writers started"
            time.sleep(0.02)
        assert _still_writes(ticks)

        process_tree.Tree(leader.pid, None, known={}).end(lambda _line: None)
        assert _still_writes(ticks), "without the session nothing names the orphan"

        process_tree.Tree(leader.pid, None, known={}, session=leader.pid).end(
            lambda _line: None
        )
        assert _still_writes(ticks), "a session with no member of the run in it may be reused"

        listing = process_tree.processes(None)
        process_tree.Tree(
            leader.pid, None, known={member: listing[member].start}, session=leader.pid
        ).end(lambda _line: None)
        assert not _alive(writer) or not _still_writes(ticks), "the orphan outlived the run"
        assert _still_writes(other), "a process outside the run is never signalled"
    finally:
        for pid in (writer, member, unrelated.pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        unrelated.wait()


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="reads /proc")
def test_a_process_scope_is_the_last_component_of_its_cgroup_v2_path() -> None:
    lines = Path("/proc/self/cgroup").read_text().splitlines()
    unified = [line[3:] for line in lines if line.startswith("0::")]
    if not unified or unified[0].rstrip("/") == "":
        pytest.skip("no cgroup v2 path for this process")
    expected = unified[0].rstrip("/").rsplit("/", 1)[-1]

    assert process_tree.processes(None)[os.getpid()].scope == expected
    assert process_tree.scope_of(os.getpid()) == expected


def test_cleanup_holds_while_responsibility_cannot_be_read() -> None:
    machine = FakeProcessTable(ORPHAN, responsible={ORPHAN.pid: ROOT})
    reads: list[int] = []

    def responsible(pid: int) -> int | None:
        reads.append(pid)
        if len(reads) <= 2:
            raise process_tree.ProcessesUnknown("cannot read the responsible process of pid 302")
        return machine.responsible(pid)

    lines = _end(machine, responsible=responsible)
    assert machine.killed == [ORPHAN.pid]
    assert lines == [
        "cannot read the responsible process of pid 302; "
        "holding the job until the run's processes are confirmed gone"
    ]


def _libproc_scan() -> dict[int, process_tree.Process]:
    import ctypes
    import struct

    lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    ids = (ctypes.c_int * 8192)()
    n = lib.proc_listpids(1, 0, ids, ctypes.sizeof(ids))
    if n <= 0:
        raise process_tree.ProcessesUnknown("proc_listpids failed")
    found = {}
    for pid in ids[: n // 4]:
        buf = ctypes.create_string_buffer(136)
        if lib.proc_pidinfo(pid, 3, 0, buf, 136) != 136:
            continue
        fields = struct.unpack_from("5I", buf.raw)
        if fields[1] == 5:
            continue
        start = struct.unpack_from("QQ", buf.raw, 120)
        found[pid] = process_tree.Process(pid, fields[4], str(start), False)
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
    monkeypatch.setattr(process_tree, "_ps_processes", _libproc_scan)
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


RESPONSIBILITY_PROBE = """\
import os, subprocess, sys, time
role, here = sys.argv[1], sys.argv[2]

def spawn_leaf():
    subprocess.Popen([sys.executable, __file__, "leaf", here],
        env={"PATH": os.environ.get("PATH", "")}, start_new_session=True)

if role == "leaf":
    open(os.path.join(here, "leaf.tmp"), "w").write(str(os.getpid()))
    os.replace(os.path.join(here, "leaf.tmp"), os.path.join(here, "leaf.pid"))
    time.sleep(60)
elif role == "middle":
    spawn_leaf()
else:
    open(os.path.join(here, "command.pid"), "w").write(str(os.getpid()))
    if sys.argv[3] == "anchor":
        subprocess.run([sys.executable, __file__, "middle", here])
    else:
        spawn_leaf()
    sys.stdin.read()
"""


def _ppid(pid: int) -> int | None:
    out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True)
    return int(out.stdout) if out.stdout.strip().isdigit() else None


def _start_probe_worker(script: Path, here: Path, mode: str, gated: bool) -> subprocess.Popen[bytes]:
    command = [sys.executable, str(script), "worker", str(here), mode]
    if not gated:
        return subprocess.Popen(command, stdin=subprocess.PIPE)
    read_end, write_end = os.pipe()
    report_read, report_write = os.pipe()
    worker = subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", job_run._GATE, str(read_end), str(report_write), *command],
        stdin=subprocess.PIPE, pass_fds=(read_end, report_write),
    )
    os.close(read_end)
    os.close(report_write)
    os.write(write_end, b"go\n")
    os.close(write_end)
    os.close(report_read)
    return worker


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS responsible processes")
@pytest.mark.parametrize(
    ("gated", "mode"),
    [(True, "exit"), (True, "anchor"), (False, "exit")],
    ids=["disclaimed", "disclaimed-anchor", "undisclaimed"],
)
def test_responsibility_probe_a_setsid_grandchild_names_its_disclaimed_ancestor(
    tmp_path: Path, gated: bool, mode: str
) -> None:
    script = tmp_path / "probe.py"
    script.write_text(RESPONSIBILITY_PROBE)
    leaf_file = tmp_path / "leaf.pid"
    worker = _start_probe_worker(script, tmp_path, mode, gated)
    leaf = None
    try:
        deadline = time.monotonic() + 10
        while not leaf_file.exists():
            assert time.monotonic() < deadline and worker.poll() is None
            time.sleep(0.02)
        leaf = int(leaf_file.read_text())
        w, command = worker.pid, int((tmp_path / "command.pid").read_text())
        resp_w = process_tree._responsible_pid(w)
        live = process_tree._responsible_pid(leaf)
        assert worker.stdin is not None
        worker.stdin.close()
        if not gated:
            worker.wait(timeout=10)
        while _alive(command):
            assert time.monotonic() < deadline + 10, "the command did not exit"
            time.sleep(0.02)
        after = process_tree._responsible_pid(leaf)
        if worker.poll() is None:
            worker.kill()
            worker.wait()
        time.sleep(0.2)
        gone = process_tree._responsible_pid(leaf)
        print(
            f"PROBE gated={gated} mode={mode} test={os.getpid()} W={w} command={command} "
            f"L={leaf} resp(W)={resp_w} resp(L): command-alive={live} command-gone={after} "
            f"W-gone={gone} ppid(L)={_ppid(leaf)}"
        )
        assert _alive(leaf)
        if gated:
            assert (resp_w, live, after) == (w, w, w), "the leaf names the anchor while it lives"
            assert gone == leaf, "the name collapses once the anchor exits"
        else:
            assert w not in (live, after, gone)
    finally:
        if leaf is not None:
            try:
                os.kill(leaf, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if worker.poll() is None:
            worker.kill()
            worker.wait()
