"""`orch cleanup` (issue #928): finished job checkouts go, logs and live jobs stay.

The end-to-end tests send the real ssh script through a transport that runs
it locally under `sh -c`, as sshd would on the device, so `orch cleanup
device` runs from this checkout's bin/orch against a fixture work root.
"""

from __future__ import annotations

import fcntl
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from types import MappingProxyType

import pytest

from lib import job_cleanup, job_leftovers, remote, worktrees
from lib.job_cleanup import Plan
from lib.machines_yml import Device, MachinesConfig
from lib.queue import SQLiteJobQueue
from lib.remote import SSH_UNREACHABLE, Called, RemoteTarget

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000.0
OLD = NOW - 3 * 86400
HOUR = 3600.0

pytestmark = pytest.mark.serial

FLOCK = """#!{python}
import fcntl, sys, time
args = sys.argv[1:]
wait = float(args[1]) if args[:1] == ["-w"] else None
fd = int(args[-1])
end = None if wait is None else time.monotonic() + wait
while True:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        break
    except BlockingIOError:
        if end is not None and time.monotonic() > end:
            sys.exit(1)
        time.sleep(0.02)
"""


@pytest.fixture(autouse=True)
def flock_on_path(tmp_path_factory, monkeypatch) -> None:
    """Linux has flock(1); a Mac without it gets this stand-in for `flock [-w s] fd`."""
    if shutil.which("flock"):
        return
    shim = tmp_path_factory.mktemp("flock-bin")
    (shim / "flock").write_text(FLOCK.format(python=sys.executable))
    (shim / "flock").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}:{os.environ['PATH']}")


@pytest.fixture(autouse=True)
def no_ambient_git_identity(tmp_path_factory, monkeypatch) -> None:
    config = tmp_path_factory.mktemp("gitconfig") / "config"
    config.write_text("[user]\n\tuseConfigOnly = true\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                 "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(name, raising=False)


def _this_test_and_its_descendants() -> set[str]:
    parent_of = {}
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            parent_of[stat.parent.name] = stat.read_text().rsplit(")", 1)[1].split()[1]
        except OSError:
            continue
    family = {str(os.getpid())}
    for pid in parent_of:
        chain = []
        while pid in parent_of and pid not in family:
            chain.append(pid)
            pid = parent_of[pid]
        if pid in family:
            family.update(chain)
    return family


@pytest.fixture(autouse=True)
def _only_this_tests_processes(monkeypatch) -> None:
    real_mine = job_cleanup._mine

    def mine(process: Path) -> bool | None:
        found = real_mine(process)
        if job_cleanup.PROC != Path("/proc") or process.name in _this_test_and_its_descendants():
            return found
        return None if found is None else False

    monkeypatch.setattr(job_cleanup, "_mine", mine)


def device_path() -> str:
    return (f"{Path(sys.executable).parent}:{Path(shutil.which('flock')).parent}"
            ":/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")


DEVICE_ORCH = """import os, sys
from pathlib import Path
sys.path.insert(0, os.getcwd())
from lib import job_cleanup
from lib.cli import main
job_cleanup.PROC = Path(os.environ["FAKE_PROC"])
sys.exit(main())
"""


class ShellTransport:
    def __init__(self, home: Path):
        self.home = home
        self.offline: set[str] = set()
        device = home / "shell-device"
        self.proc = fake_proc(device, {"1": ("0", device)})
        self.orch = device / "orch.py"
        self.orch.write_text(DEVICE_ORCH)

    def call(self, argv, *, timeout: float, input: bytes | None = None) -> Called:
        alias, script = argv[-2], argv[-1]
        if alias in self.offline:
            return Called(SSH_UNREACHABLE, "", "")
        assert script.count("./bin/orch") == 1, script
        done = subprocess.run(
            ["sh", "-c", script.replace("./bin/orch", str(self.orch))], input=input or b"",
            capture_output=True, timeout=timeout,
            env={"HOME": str(self.home), "PATH": device_path(), "FAKE_PROC": str(self.proc)},
        )
        return Called(done.returncode, done.stdout.decode(), done.stderr.decode())


class InProcessTransport:
    def __init__(self, root: Path, scan=job_cleanup.jobs_held_open):
        self.root = root
        self.scan = scan

    def call(self, argv, *, timeout: float, input: bytes | None = None) -> Called:
        report = job_cleanup.device_step(self.root, Plan.parse(input.decode()), scan=self.scan)
        return Called(0, f"{job_cleanup.MARK} {report.to_json()}\n", "")


def device_pass(root: Path, plan: Plan, scan=job_cleanup.jobs_held_open) -> job_cleanup.Report:
    hold = job_cleanup.device_step(root, plan, scan=scan)
    done = job_cleanup.device_step(
        root, Plan(frozenset(hold.held), plan.cutoff, False, job_cleanup.FINISH), scan=scan)
    return job_cleanup.Report(done.removed, {**hold.kept, **done.kept}, False)


def machines(tmp_path: Path, *names: str) -> MachinesConfig:
    return MachinesConfig(devices=tuple(
        Device(
            name=name,
            slot_max=2,
            harnesses=MappingProxyType({"claude": "claude-bot"}),
            tailscale_name=f"{name}.example.ts.net",
            local=False,
            enabled=True,
            workdir_root=f"{tmp_path}/{name}/{{bot}}/work",
            ssh_alias=f"{name}-{{bot}}",
            factory_root=str(ROOT),
        )
        for name in names
    ))


def work_root(tmp_path: Path, name: str = "pc") -> Path:
    return tmp_path / name / "claude-bot" / "work"


def database(tmp_path: Path, rows: list[tuple[str, str, str, float | None]],
             unhandled: frozenset[str] = frozenset()) -> Path:
    db = tmp_path / "orch.db"
    SQLiteJobQueue(db, clock=lambda: NOW)
    with SQLiteJobQueue(db, clock=lambda: NOW).connect() as connection:
        for job_id, ticket, state, finished_at in rows:
            connection.execute(
                """
                INSERT INTO jobs (id, dedupe_key, repo, ticket_id, action, recipient,
                    source_comment_id, timeout_seconds, generation, attempt, state,
                    created_at, last_activity_at, finished_at, failure_handled)
                VALUES (?, ?, 'Owner/Repo', ?, 'build', 'claude', 'c1', 60, 1, 1, ?, ?, ?, ?, ?)
                """,
                (job_id, job_id, ticket, state, OLD, OLD, finished_at,
                 0 if job_id in unhandled else 1),
            )
    return db


def age(path: Path, when: float) -> None:
    for root, directories, files in os.walk(path):
        for name in (*directories, *files):
            os.utime(os.path.join(root, name), (when, when), follow_symlinks=False)
    os.utime(path, (when, when), follow_symlinks=False)


def job_folder(root: Path, job_id: str, *, size: int = 64 * 1024, when: float = OLD) -> Path:
    jobs = root / "jobs"
    (root.parent / "outside").mkdir(parents=True, exist_ok=True)
    (root.parent / "outside" / "keep").write_text("outside the work root\n")
    folder = jobs / job_id
    (folder / "repo" / ".git").mkdir(parents=True)
    (folder / "repo" / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (folder / "repo" / "node_modules" / "pkg").mkdir(parents=True)
    (folder / "repo" / "node_modules" / "pkg" / "blob").write_bytes(os.urandom(size))
    (folder / "repo" / "README").write_text("checkout\n")
    (folder / "repo" / "link").symlink_to(root.parent / "outside")
    readonly = folder / "repo" / "readonly"
    readonly.mkdir()
    (readonly / "f").write_text("x")
    readonly.chmod(0o500)
    (folder / "top-file").write_text("loose file\n")
    (folder / "top-link").symlink_to(root.parent / "outside")
    (folder / ".orch-worker.pid").write_text("123\n")
    (folder / ".orch-worker-abc.tree.pid").write_text("123 124\n")
    run = jobs / f"{job_id}.run" / "tag"
    run.mkdir(parents=True)
    (run / "log").write_text(f"log of {job_id}\n")
    (run / "handoff").write_text("ORCH-HANDOFF: 42\n")
    (run / "exit").write_text("0\n")
    (jobs / f"{job_id}.launched").write_text("launched\n")
    (jobs / f"{job_id}.brief.md").write_text("# brief\n")
    for path in (folder, jobs / f"{job_id}.run", jobs / f"{job_id}.launched",
                 jobs / f"{job_id}.brief.md"):
        age(path, when)
    return folder


def kept_files(root: Path, job_id: str) -> dict[str, bytes]:
    jobs = root / "jobs"
    paths = [jobs / f"{job_id}.launched", jobs / f"{job_id}.brief.md",
             *sorted((jobs / f"{job_id}.run").rglob("*"))]
    return {str(path): path.read_bytes() for path in paths if path.is_file()}


def du(path: Path) -> int:
    return int(subprocess.run(["du", "-s", "-B1", str(path)], capture_output=True, text=True,
                              check=True).stdout.split()[0])


def cleanup(argv: list[str], transport, capsys) -> tuple[int, str]:
    code = job_cleanup.main(argv, clock=lambda: NOW, transport=transport)
    return code, capsys.readouterr().out


def test_finished_checkout_goes_and_its_logs_and_records_stay(tmp_path: Path) -> None:
    root = work_root(tmp_path)
    folder = job_folder(root, "done1")
    jobs = root / "jobs"
    before = kept_files(root, "done1")
    records = {path.name: path.read_bytes() for path in folder.glob(".orch-worker*")}
    db = database(tmp_path, [("done1", "t1", "done", OLD)])
    outcomes = job_cleanup.run(db, machines(tmp_path, "pc"), ShellTransport(tmp_path),
                               now=NOW, grace_hours=6, dry_run=False)

    [(_, report)] = outcomes
    assert not isinstance(report, str), report
    assert set(report.removed) == {"done1"} and report.kept == {}
    assert not folder.exists()
    kept = root / "jobs-kept" / "done1"
    assert {path: (kept / Path(path).relative_to(jobs)).read_bytes() for path in before} == before
    assert {name: (kept / "done1" / name).read_bytes() for name in records} == records
    assert len(before) == 5 and len(records) == 2
    assert (root.parent / "outside" / "keep").exists()


def test_cli_backlog_pass_prints_one_line_per_target(tmp_path: Path, capsys, monkeypatch) -> None:
    root = work_root(tmp_path)
    job_folder(root, "done1")
    db = database(tmp_path, [("done1", "t1", "done", OLD)])
    monkeypatch.setattr(job_cleanup.health, "load_machines", lambda _: machines(tmp_path, "pc"))

    code, out = cleanup(["run", "--backlog", "--db", str(db)], ShellTransport(tmp_path), capsys)

    assert code == 0
    assert out.startswith(f"pc claude-bot {root}: removed 1 checkouts (")
    assert out.rstrip().endswith("kept 0")


def test_run_without_backlog_is_refused(tmp_path: Path, capsys) -> None:
    db = database(tmp_path, [])
    code = job_cleanup.main(["run", "--db", str(db)], clock=lambda: NOW)
    assert code == 2
    assert "--backlog" in capsys.readouterr().err


def test_device_refuses_a_plan_that_names_a_path(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(
        '{"finished": ["../../etc"], "cutoff": 0, "dry_run": false}'))
    assert job_cleanup.main(["device", "--root", str(tmp_path)]) == 2
    assert "bad plan" in capsys.readouterr().err


LIVE_CASES = {
    "queued": ([("j", "t1", "queued", None)], "live"),
    "claimed": ([("j", "t1", "running", None)], "live"),
    "running": ([("j", "t1", "running", None)], "live"),
    "retry-held by a queued retry": (
        [("j", "t1", "failed", OLD), ("retry", "t1", "queued", None)], "retry-held"),
    "retry-held by a running retry": (
        [("j", "t1", "failed", OLD), ("retry", "t1", "running", None)], "retry-held"),
    "cancelled then requeued": ([("j", "t1", "queued", None)], "live"),
    "failed, not yet handled by T6": ([("j", "t1", "failed", OLD)], "retry-held"),
}


@pytest.mark.parametrize("case", LIVE_CASES, ids=list(LIVE_CASES))
def test_live_or_retry_held_folder_is_never_removed(tmp_path: Path, case: str) -> None:
    rows, reason = LIVE_CASES[case]
    unhandled = frozenset({"j"}) if case.startswith("failed, not yet") else frozenset()
    root = work_root(tmp_path)
    folder = job_folder(root, "j")
    if case == "claimed":
        for record in folder.glob(".orch-worker*"):
            record.unlink()
        age(folder, OLD)
    contents = sorted(path.relative_to(folder) for path in folder.rglob("*"))
    db = database(tmp_path, rows, unhandled)

    [(_, report)] = job_cleanup.run(db, machines(tmp_path, "pc"), InProcessTransport(root),
                                    now=NOW, grace_hours=6, dry_run=False)

    assert report.removed == {}
    assert report.kept == {"j": reason}
    assert sorted(path.relative_to(folder) for path in folder.rglob("*")) == contents


def test_failed_job_is_released_once_its_ticket_has_no_live_job(tmp_path: Path) -> None:
    root = work_root(tmp_path)
    job_folder(root, "j")
    db = database(tmp_path, [("j", "t1", "failed", OLD), ("retry", "t1", "done", OLD),
                             ("other", "t2", "queued", None)])

    [(_, report)] = job_cleanup.run(db, machines(tmp_path, "pc"), InProcessTransport(root),
                                    now=NOW, grace_hours=6, dry_run=False)

    assert set(report.removed) == {"j"}


def test_device_guards_keep_recent_held_and_unscannable_folders(tmp_path: Path) -> None:
    root = work_root(tmp_path)
    for job_id in ("launched", "held", "free"):
        job_folder(root, job_id)
    jobs = root / "jobs"
    os.utime(jobs / "launched.launched", (NOW, NOW))
    plan = Plan(frozenset({"launched", "held", "free"}), NOW - 6 * HOUR, False)
    sleeper = subprocess.Popen(["sleep", "60"], cwd=jobs / "held" / "repo")
    try:
        report = device_pass(root, plan)
        assert (jobs / "held" / "repo" / "README").exists()
    finally:
        sleeper.kill()
        sleeper.wait()

    assert report.kept == {"launched": "claimed-since", "held": "held-open"}
    assert set(report.removed) == {"free"}
    job_folder(root, "free2")
    blind = device_pass(root, Plan(frozenset({"free2"}), NOW - 6 * HOUR, False),
                        scan=lambda _: None)
    assert blind.kept["free2"] == "cannot-scan" and blind.removed == {}
    assert (jobs / "free2" / "repo" / "README").exists()


def test_held_open_scan_sees_through_a_symlinked_work_root(tmp_path: Path) -> None:
    real = work_root(tmp_path)
    job_folder(real, "held")
    linked = tmp_path / "linked-work"
    linked.symlink_to(real)
    sleeper = subprocess.Popen(["sleep", "60"], cwd=real / "jobs" / "held" / "repo")
    try:
        report = device_pass(linked, Plan(frozenset({"held"}), NOW - 6 * HOUR, False))
    finally:
        sleeper.kill()
        sleeper.wait()

    assert report.kept == {"held": "held-open"} and report.removed == {}


def test_backlog_sweep_of_mixed_folders_removes_only_finished_checkouts(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    root = work_root(tmp_path)
    jobs = root / "jobs"
    finished = {"done1": 300_000, "failed1": 200_000, "cancelled1": 100_000}
    for job_id, size in finished.items():
        job_folder(root, job_id, size=size)
    for job_id in ("running1", "held1", "stray1", "wt1", "recent1"):
        job_folder(root, job_id)
    (jobs / "wt1.json").write_text("not json")
    (jobs / "notes.txt").write_text("not a job folder")
    db = database(tmp_path, [
        ("done1", "t1", "done", OLD),
        ("failed1", "t2", "failed", OLD),
        ("cancelled1", "t3", "cancelled", OLD),
        ("running1", "t4", "running", None),
        ("held1", "t5", "failed", OLD),
        ("held1-retry", "t5", "queued", None),
        ("wt1", "t6", "done", OLD),
        ("recent1", "t7", "done", NOW - HOUR),
    ])
    snapshot = sorted(str(path.relative_to(jobs)) for path in jobs.rglob("*"))
    sizes = {job_id: du(jobs / job_id) for job_id in finished}
    config = machines(tmp_path, "pc")
    transport = ShellTransport(tmp_path)

    [(_, dry)] = job_cleanup.run(db, config, transport, now=NOW, grace_hours=6, dry_run=True)
    assert sorted(str(path.relative_to(jobs)) for path in jobs.rglob("*")) == snapshot

    [(target, report)] = job_cleanup.run(db, config, transport, now=NOW, grace_hours=6,
                                         dry_run=False)

    assert set(report.removed) == set(finished) == set(dry.removed)
    assert report.kept == dry.kept == {
        "running1": "live", "held1": "retry-held", "stray1": "unknown",
        "wt1": "worktree-record", "recent1": "recent",
    }
    assert not any((jobs / job_id).exists() for job_id in finished)
    freed = {job_id: sizes[job_id] - sum(
        path.lstat().st_blocks * 512 for path in (root / "jobs-kept" / job_id / job_id).rglob("*")
        if path.is_file()) for job_id in finished}
    assert report.removed == freed == dry.removed
    assert report.bytes_freed == sum(freed.values()) > sum(finished.values())
    for job_id in ("running1", "held1", "stray1", "wt1", "recent1"):
        assert (jobs / job_id / "repo" / "README").exists()
    assert (jobs / "notes.txt").exists()
    line = job_cleanup.summary(target, report)
    assert line.startswith(f"pc claude-bot {root}: removed 3 checkouts (")
    assert line.endswith("kept 5 (live 1, recent 1, retry-held 1, unknown 1, worktree-record 1)")
    assert "would remove 3" in job_cleanup.summary(target, dry)


def test_a_failing_target_does_not_stop_the_others(tmp_path: Path, capsys, monkeypatch) -> None:
    for name in ("pc", "mac"):
        job_folder(work_root(tmp_path, name), "done1")
    db = database(tmp_path, [("done1", "t1", "done", OLD)])
    monkeypatch.setattr(job_cleanup.health, "load_machines",
                        lambda _: machines(tmp_path, "pc", "mac"))
    transport = ShellTransport(tmp_path)
    transport.offline.add("pc-claude-bot")

    code, out = cleanup(["run", "--backlog", "--db", str(db)], transport, capsys)

    assert code == 1
    lines = out.splitlines()
    assert f"pc claude-bot {work_root(tmp_path, 'pc')}: failed: ssh failed" in lines
    assert any(line.startswith("mac claude-bot") and "removed 1" in line for line in lines)
    assert (work_root(tmp_path, "pc") / "jobs" / "done1" / "repo").exists()
    assert not (work_root(tmp_path, "mac") / "jobs" / "done1" / "repo").exists()


class RequeueAndClaim:
    def __init__(self, root: Path, db: Path, job_id: str, *, when: str):
        self.device = InProcessTransport(root)
        self.queue = SQLiteJobQueue(db, clock=lambda: NOW)
        self.job_id = job_id
        self.when = when
        with self.queue.connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS job_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "job_id TEXT NOT NULL, repo TEXT NOT NULL, ticket_id TEXT NOT NULL, "
                "action TEXT NOT NULL, recipient TEXT NOT NULL, event TEXT NOT NULL, "
                "detail TEXT NOT NULL, recorded_at REAL NOT NULL)")

    def claim(self) -> None:
        with self.queue.connect() as connection:
            assert self.queue.requeue(self.job_id, "answer-rejected:c1", connection=connection)
        claimed = self.queue.claim("claude")
        assert (claimed.id, claimed.state) == (self.job_id, "running")

    def call(self, argv, *, timeout: float, input: bytes | None = None) -> Called:
        hold = Plan.parse(input.decode()).step == job_cleanup.HOLD
        if hold and self.when == "before the hold":
            self.claim()
        said = self.device.call(argv, timeout=timeout, input=input)
        if hold and self.when == "after the hold":
            self.claim()
        return said


@pytest.mark.parametrize("when", ["before the hold", "after the hold"])
def test_a_job_claimed_after_the_host_read_keeps_its_folder(tmp_path: Path, when: str) -> None:
    root = work_root(tmp_path)
    folder = job_folder(root, "j")
    contents = sorted(path.relative_to(folder) for path in folder.rglob("*"))
    db = database(tmp_path, [("j", "t1", "cancelled", OLD)])
    transport = RequeueAndClaim(root, db, "j", when=when)

    [(_, report)] = job_cleanup.run(db, machines(tmp_path, "pc"), transport,
                                    now=NOW, grace_hours=6, dry_run=False)

    assert job_cleanup.read_jobs(db)[0].state == "running"
    assert report.removed == {} and report.kept == {"j": "live"}
    assert sorted(path.relative_to(folder) for path in folder.rglob("*")) == contents
    assert not (root / "jobs" / "j.cleanup").exists()


def test_a_launch_after_the_confirming_read_gets_a_fresh_folder_the_finish_leaves(
    tmp_path: Path,
) -> None:
    root = work_root(tmp_path)
    folder = job_folder(root, "j")
    jobs = root / "jobs"
    contents = sorted(path.relative_to(folder) for path in folder.rglob("*"))
    plan = Plan(frozenset({"j"}), NOW - 6 * HOUR, False)
    hold = job_cleanup.device_step(root, plan)
    assert hold.held == ("j",) and not (jobs / "j").exists()
    (jobs / "j").mkdir()
    (jobs / "j" / "new-run").write_text("work of the next run\n")

    done = job_cleanup.device_step(
        root, Plan(frozenset({"j"}), plan.cutoff, False, job_cleanup.FINISH))

    assert (jobs / "j" / "new-run").read_text() == "work of the next run\n"
    assert done.removed == {} and done.kept["j"].startswith("error: j was made again")
    aside = jobs / "j.cleanup"
    assert sorted(path.relative_to(aside) for path in aside.rglob("*")) == contents
    assert not (jobs / "j.gone").exists() and not (root / "jobs-kept").exists()


def test_a_folder_left_aside_by_an_interrupted_pass_is_settled_by_the_next(tmp_path: Path) -> None:
    root = work_root(tmp_path)
    job_folder(root, "gone")
    job_folder(root, "back")
    jobs = root / "jobs"
    job_cleanup.device_step(root, Plan(frozenset({"gone", "back"}), NOW - 6 * HOUR, False))
    assert sorted(path.name for path in jobs.glob("*.cleanup")) == ["back.cleanup", "gone.cleanup"]
    db = database(tmp_path, [("gone", "t1", "done", OLD), ("back", "t2", "queued", None)])

    [(_, report)] = job_cleanup.run(db, machines(tmp_path, "pc"), InProcessTransport(root),
                                    now=NOW, grace_hours=6, dry_run=False)

    assert set(report.removed) == {"gone"} and report.kept == {"back": "live"}
    assert (jobs / "back" / "repo" / "README").exists()
    assert not (jobs / "gone" / "repo").exists() and not list(jobs.glob("*.cleanup"))


NON_DUMPABLE = (
    "import ctypes, sys, time; "
    "assert ctypes.CDLL(None).prctl(4, 0, 0, 0, 0) == 0; "
    "print('ready', flush=True); time.sleep(60)"
)


def add_process(proc: Path, pid: str, parent: str, cwd: Path | None, *, name: str = "p",
                uid: int | None = None) -> Path:
    uid = os.getuid() if uid is None else uid
    process = proc / pid
    (process / "fd").mkdir(parents=True)
    (process / "cwd").symlink_to(cwd or proc)
    (process / "status").write_text(f"Name:\t{name}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\nVmSize:\t1 kB\n")
    (process / "stat").write_text(f"{pid} ({name}) S {parent} 1 1 0\n")
    if cwd is None:
        (process / "fd").chmod(0)
    return process


def fake_proc(tmp_path: Path, processes: dict[str, tuple[str, Path | None]]) -> Path:
    proc = tmp_path / "proc"
    for pid, (parent, cwd) in processes.items():
        add_process(proc, pid, parent, cwd)
    (proc / "self").symlink_to(proc / "1")
    return proc


ROOT_UID = 0 if os.getuid() else 1


def roots_init(tmp_path: Path) -> Path:
    proc = tmp_path / "proc"
    add_process(proc, "1", "0", tmp_path, name="systemd", uid=ROOT_UID)
    (proc / "self").symlink_to(proc / "1")
    return proc


def hidden_cwd(monkeypatch: pytest.MonkeyPatch, proc: Path) -> None:
    real_readlink = os.readlink

    def readlink(path, *args, **kwargs):
        link = Path(path)
        if link.name == "cwd" and not os.access(link.parent / "fd", os.R_OK):
            raise PermissionError("non-dumpable")
        return real_readlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "readlink", readlink)
    monkeypatch.setattr(job_cleanup, "PROC", proc)
    monkeypatch.setattr(job_cleanup, "runner_locks", lambda: True)


def unhide(proc: Path) -> None:
    for fd in proc.glob("*/fd"):
        fd.chmod(0o700)


def test_a_hidden_orphan_of_roots_init_keeps_every_folder_through_hold_and_finish(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = work_root(tmp_path)
    for job_id in ("held", "free"):
        job_folder(root, job_id)
    jobs = root / "jobs"
    proc = roots_init(tmp_path)
    hidden_cwd(monkeypatch, proc)
    named = frozenset({"held", "free"})
    try:
        add_process(proc, "11", "1", jobs / "held" / "repo")
        (proc / "11" / "fd").chmod(0)
        if os.access(proc / "11" / "fd", os.R_OK):
            pytest.skip("this user can read a directory with no permissions (root)")
        hold = job_cleanup.device_step(root, Plan(named, NOW - 6 * HOUR, False))
        assert hold.held == () and hold.kept == {"held": "held-open", "free": "held-open"}

        unhide(proc)
        shutil.rmtree(proc / "11")
        hold = job_cleanup.device_step(root, Plan(named, NOW - 6 * HOUR, False))
        assert hold.held == ("free", "held")
        add_process(proc, "11", "1", jobs / "held.cleanup" / "repo")
        (proc / "11" / "fd").chmod(0)
        done = job_cleanup.device_step(
            root, Plan(frozenset(hold.held), NOW - 6 * HOUR, False, job_cleanup.FINISH))
    finally:
        unhide(proc)

    assert done.removed == {} and done.kept == {"held": "held-open", "free": "held-open"}
    assert (jobs / "held" / "repo" / "README").exists()
    assert (jobs / "free" / "repo" / "README").exists()


@pytest.mark.parametrize("session", ["sshd", "sshd-session"])
def test_the_ssh_session_running_cleanup_does_not_hold_every_folder(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session: str) -> None:
    root = work_root(tmp_path)
    for job_id in ("held", "free"):
        job_folder(root, job_id)
    jobs = root / "jobs"
    proc = roots_init(tmp_path)
    add_process(proc, "5", "1", tmp_path, name="sshd", uid=ROOT_UID)
    add_process(proc, "20", "5", None, name=session)
    add_process(proc, "21", "20", tmp_path, name="sh")
    add_process(proc, "22", "21", jobs / "held" / "repo", name="python3")
    add_process(proc, "30", "1", None, name="systemd")
    add_process(proc, "31", "30", None, name="(sd-pam)")
    hidden_cwd(monkeypatch, proc)
    try:
        if os.access(proc / "20" / "fd", os.R_OK):
            pytest.skip("this user can read a directory with no permissions (root)")
        assert job_cleanup.jobs_held_open(jobs) == {"held"}
        report = device_pass(root, Plan(frozenset({"held", "free"}), NOW - 6 * HOUR, False))
    finally:
        unhide(proc)

    assert set(report.removed) == {"free"} and report.kept == {"held": "held-open"}
    assert (jobs / "held" / "repo" / "README").exists()


def test_a_hidden_process_keeps_every_folder_and_does_not_stop_the_scan(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = work_root(tmp_path)
    for job_id in ("held", "free"):
        job_folder(root, job_id)
    jobs = root / "jobs"
    proc = fake_proc(tmp_path, {
        "1": ("0", tmp_path),
        "10": ("1", jobs / "held" / "repo"),
        "11": ("10", None),
        "12": ("1", None),
    })
    if os.access(proc / "11" / "fd", os.R_OK):
        pytest.skip("this user can read a directory with no permissions (root)")
    monkeypatch.setattr(job_cleanup, "PROC", proc)
    try:
        assert job_cleanup.jobs_held_open(jobs) == set(os.listdir(jobs))
        report = device_pass(root, Plan(frozenset({"held", "free"}), NOW - 6 * HOUR, False))
    finally:
        for pid in ("11", "12"):
            (proc / pid / "fd").chmod(0o700)

    assert report.removed == {} and report.kept == {"held": "held-open", "free": "held-open"}
    assert (jobs / "held" / "repo" / "README").exists()


@pytest.mark.parametrize("parents", [
    pytest.param({"1": "0", "10": "1", "11": "10"}, id="parent-outside-the-jobs-root"),
    pytest.param({"1": "0", "11": "1"}, id="parent-exited"),
])
def test_a_hidden_process_keeps_the_folder_no_readable_process_holds(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parents: dict[str, str]) -> None:
    root = work_root(tmp_path)
    folder = job_folder(root, "held")
    proc = fake_proc(tmp_path, {
        pid: (parent, folder / "repo" if pid == "11" else tmp_path) for pid, parent in parents.items()
    })
    fd = proc / "11" / "fd"
    fd.chmod(0)
    real_readlink = os.readlink

    def readlink(path, *args, **kwargs):
        if Path(path) == proc / "11" / "cwd":
            raise PermissionError("non-dumpable")
        return real_readlink(path, *args, **kwargs)

    try:
        if os.access(fd, os.R_OK):
            pytest.skip("this user can read a directory with no permissions (root)")
        monkeypatch.setattr(job_cleanup, "PROC", proc)
        monkeypatch.setattr(job_cleanup, "runner_locks", lambda: True)
        monkeypatch.setattr(os, "readlink", readlink)
        report = device_pass(root, Plan(frozenset({"held"}), NOW - 6 * HOUR, False))
    finally:
        fd.chmod(0o700)

    assert report.removed == {} and report.kept == {"held": "held-open"}
    assert (folder / "repo" / "README").exists()


@pytest.mark.skipif(not Path("/proc/self/cwd").exists(), reason="needs Linux /proc")
def test_a_live_hidden_process_keeps_every_folder(tmp_path: Path) -> None:
    root = work_root(tmp_path)
    for job_id in ("held", "free"):
        job_folder(root, job_id)
    jobs = root / "jobs"
    hidden = subprocess.Popen([sys.executable, "-c", NON_DUMPABLE], cwd=tmp_path,
                              stdout=subprocess.PIPE, text=True)
    try:
        assert hidden.stdout.readline().strip() == "ready"
        try:
            os.readlink(f"/proc/{hidden.pid}/cwd")
            pytest.skip("this user can read a non-dumpable process's cwd (root or CAP_SYS_PTRACE)")
        except PermissionError:
            pass
        assert job_cleanup.jobs_held_open(jobs) == set(os.listdir(jobs))
        report = device_pass(root, Plan(frozenset({"held", "free"}), NOW - 6 * HOUR, False))
        assert hidden.poll() is None
    finally:
        hidden.kill()
        hidden.wait()

    assert report.removed == {} and report.kept == {"held": "held-open", "free": "held-open"}
    assert (jobs / "held" / "repo" / "README").exists()
    assert job_cleanup.jobs_held_open(jobs) == set()


@pytest.mark.parametrize("name, exits_in, reads_first", [
    ("python3", "_status", 1),
    ("sshd-session", "_parent", 0),
])
def test_a_process_that_exits_mid_scan_does_not_hold_every_folder(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, exits_in: str,
        reads_first: int) -> None:
    root = work_root(tmp_path)
    job_folder(root, "free")
    proc = roots_init(tmp_path)
    add_process(proc, "21", "1", tmp_path, name="sh")
    add_process(proc, "22", "21", None, name=name)
    hidden_cwd(monkeypatch, proc)
    if os.access(proc / "22" / "fd", os.R_OK):
        pytest.skip("this user can read a directory with no permissions (root)")
    gone = proc / "22"
    step = getattr(job_cleanup, exits_in)
    reads = 0

    def exits_before(process: Path):
        nonlocal reads
        if process == gone and gone.exists():
            reads += 1
            if reads > reads_first:
                unhide(proc)
                shutil.rmtree(gone)
        return step(process)

    monkeypatch.setattr(job_cleanup, exits_in, exits_before)
    assert job_cleanup.jobs_held_open(root / "jobs") == set()
    assert not gone.exists()


GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}
TAG = "0123456789abcdef" * 2


def wall_clock_days_ago(days: float = 3) -> float:
    return time.time() - days * 86400
WORKER = (
    'printf "%s %s\\n" "$(test -d .git && echo True || echo False)" '
    '"$(test -f README && echo True || echo False)" > "$1"'
)


class Runner:
    def __init__(self, tmp_path: Path, root: Path, job_id: str):
        self.tmp = tmp_path
        self.target = RemoteTarget("pc", "pc-claude-bot", "claude-bot", str(root), job_id, TAG)
        self.report = tmp_path / "worker-saw"
        origin = tmp_path / "origin.git"
        seed = tmp_path / "seed"
        for argv in (["init", "-q", "--bare", "-b", "main", str(origin)],
                     ["init", "-q", "-b", "main", str(seed)]):
            self.git(*argv)
        (seed / "README").write_text("checkout\n")
        self.git("-C", str(seed), "add", "README")
        self.git("-C", str(seed), "commit", "-q", "-m", "one")
        self.git("-C", str(seed), "push", "-q", str(origin), "HEAD:main")
        self.url = f"file://{origin}"

    def git(self, *args: str) -> None:
        subprocess.run(["git", *args], env={**os.environ, **GIT_ENV}, check=True,
                       capture_output=True)

    def sh(self, script: str, **options) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sh", "-c", script], capture_output=True, text=True, timeout=60,
            env={"HOME": str(self.tmp), "PATH": device_path(), **GIT_ENV}, **options)

    def checkout_script(self) -> str:
        return remote.checkout_script(self.target, url=self.url, head=None, base_branch="main",
                                      branch=None, brief=False)

    def checkout(self) -> str:
        done = self.sh(self.checkout_script())
        assert done.returncode == 0, done.stderr
        return done.stdout

    def start(self) -> str:
        script = remote.start_script(self.target, ["/bin/sh", "-c", WORKER, "worker",
                                                   str(self.report)], {})
        done = self.sh(script, start_new_session=True)
        assert done.returncode == 0, done.stderr
        return self.report.read_text().strip()


def checkout_folder(tmp_path: Path, root: Path, job_id: str) -> Runner:
    runner = Runner(tmp_path, root, job_id)
    assert "new" in runner.checkout()
    jobs = root / "jobs"
    (jobs / f"{job_id}.launched").write_text("launched\n")
    for path in jobs.iterdir():
        age(path, wall_clock_days_ago())
    return runner


CLAIM_POINTS = ("before the hold", "between the hold and the reread", "after the reread")


class ClaimAndStart:
    def __init__(self, root: Path, db: Path, runner: Runner, *, when: str):
        self.device = InProcessTransport(root)
        self.claimer = RequeueAndClaim(root, db, runner.target.job_id, when=when)
        self.runner = runner
        self.when = when
        self.saw: str | None = None

    def claim_and_check_out(self) -> None:
        self.claimer.claim()
        assert "reused" in self.runner.checkout()

    def call(self, argv, *, timeout: float, input: bytes | None = None) -> Called:
        step = Plan.parse(input.decode()).step
        if (self.when, step) in (("before the hold", job_cleanup.HOLD),
                                 ("after the reread", job_cleanup.FINISH)):
            self.claim_and_check_out()
        said = self.device.call(argv, timeout=timeout, input=input)
        if self.when == "between the hold and the reread" and step == job_cleanup.HOLD:
            self.claim_and_check_out()
        if self.saw is None and (step == job_cleanup.HOLD) == (self.when != "after the reread"):
            self.saw = self.runner.start()
        return said


@pytest.mark.parametrize("when", CLAIM_POINTS)
def test_a_job_claimed_during_the_pass_keeps_its_checkout_and_its_worker_sees_it(
    tmp_path: Path, when: str
) -> None:
    root = work_root(tmp_path)
    jobs = root / "jobs"
    runner = checkout_folder(tmp_path, root, "j")
    db = database(tmp_path, [("j", "t1", "cancelled", wall_clock_days_ago())])
    transport = ClaimAndStart(root, db, runner, when=when)

    [(_, report)] = job_cleanup.run(db, machines(tmp_path, "pc"), transport,
                                    now=time.time(), grace_hours=6, dry_run=False)

    assert transport.saw == "True True"
    assert (jobs / "j" / "README").read_text() == "checkout\n"
    assert not (jobs / "j.cleanup").exists()
    assert job_cleanup.read_jobs(db)[0].state == "running"
    assert report.removed == {} and report.kept["j"] in ("live", "recent", "claimed-since")


def hold_lock(path: Path, seconds: float, then_touch: Path | None = None) -> subprocess.Popen:
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl, pathlib, sys, time\n"
         "lock = open(sys.argv[1], 'a'); fcntl.flock(lock, fcntl.LOCK_EX)\n"
         "print('locked', flush=True); time.sleep(float(sys.argv[2]))\n"
         "sys.argv[3] and pathlib.Path(sys.argv[3]).touch()\n",
         str(path), str(seconds), str(then_touch or "")],
        stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "locked"
    return holder


def test_a_checkout_waits_while_cleanup_holds_the_job_lock(tmp_path: Path) -> None:
    root = work_root(tmp_path)
    jobs = root / "jobs"
    runner = checkout_folder(tmp_path, root, "j")
    (jobs / "j").rename(jobs / "j.cleanup")

    with open(jobs / "j.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        checkout = subprocess.Popen(
            ["sh", "-c", runner.checkout_script()], stdout=subprocess.PIPE, text=True,
            env={"HOME": str(tmp_path), "PATH": device_path(), **GIT_ENV})
        time.sleep(0.5)
        assert checkout.poll() is None
        assert (jobs / "j.claimed").stat().st_mtime < wall_clock_days_ago(1) and not (jobs / "j").exists()
    out, _ = checkout.communicate(timeout=60)

    assert checkout.returncode == 0 and "reused" in out
    assert (jobs / "j" / "README").exists() and not (jobs / "j.cleanup").exists()
    assert (jobs / "j.claimed").stat().st_mtime > wall_clock_days_ago(1 / 24)


LOGGED_FLOCK = '#!/bin/sh\necho "$*" >> "$FLOCK_LOG"\nexec {flock} "$@"\n'


def wait_for(done: Callable[[], bool], what: str, seconds: float = 30) -> None:
    end = time.monotonic() + seconds
    while not done():
        assert time.monotonic() < end, what
        time.sleep(0.01)


def lines(path: Path) -> int:
    return len(path.read_text().splitlines()) if path.exists() else 0


@pytest.mark.parametrize("shell", ["sh", "bash"])
def test_a_claim_waiting_on_the_old_lock_relocks_the_new_one(tmp_path: Path, shell: str) -> None:
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    log = tmp_path / "flock.log"
    logged = tmp_path / "flock"
    logged.write_text(LOGGED_FLOCK.format(flock=shutil.which("flock")))
    logged.chmod(0o755)
    path = jobs / "j.lock"

    with open(path, "a") as old:
        fcntl.flock(old, fcntl.LOCK_EX)
        claim = subprocess.Popen(
            [shell, "-c", f'd=$1; lk={logged}; {remote._CLAIM}', "claim", str(jobs / "j")],
            env={**os.environ, "FLOCK_LOG": str(log)})
        wait_for(lambda: lines(log) == 1, "the claim never waited on the old lock")
        path.unlink()
        with open(path, "a") as new:
            fcntl.flock(new, fcntl.LOCK_EX)
            old.close()
            wait_for(lambda: lines(log) == 2 or claim.poll() is not None,
                     "the claim neither relocked nor finished")
            assert not (jobs / "j.claimed").exists(), "claimed on the old inode"
            assert claim.poll() is None
        assert claim.wait(timeout=30) == 0

    assert (jobs / "j.claimed").exists() and path.exists()


def test_cleanup_relocks_when_the_lock_was_replaced(tmp_path: Path, monkeypatch) -> None:
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    path = jobs / "j.lock"
    real_flock = fcntl.flock
    waits: list[int] = []
    entered: list[int] = []

    def flock(fd, operation):
        if threading.current_thread() is waiter:
            waits.append(os.fstat(fd.fileno()).st_ino)
        real_flock(fd, operation)

    def wait_then_enter() -> None:
        with job_cleanup._claim_lock(jobs, "j") as locked:
            assert locked
            entered.append(path.stat().st_ino)

    monkeypatch.setattr(job_cleanup.fcntl, "flock", flock)
    waiter = threading.Thread(target=wait_then_enter)
    with job_cleanup._claim_lock(jobs, "j"):
        old = path.stat().st_ino
        waiter.start()
        wait_for(lambda: len(waits) == 1, "the second holder never waited")
        path.unlink()
        new = open(path, "a")
        real_flock(new, fcntl.LOCK_EX)
    try:
        wait_for(lambda: len(waits) == 2 or bool(entered), "the second holder never woke")
        assert entered == [] and waits == [old, path.stat().st_ino]
    finally:
        new.close()
    waiter.join(timeout=30)

    assert entered == [waits[1]]


@pytest.mark.parametrize("dry_run", [True, False])
def test_a_device_without_flock_keeps_every_named_folder(
    tmp_path: Path, monkeypatch, dry_run: bool
) -> None:
    root = work_root(tmp_path)
    folder = job_folder(root, "j")
    monkeypatch.setattr(job_cleanup.shutil, "which", lambda name: None)

    hold = job_cleanup.device_step(root, Plan(frozenset({"j"}), NOW - 6 * HOUR, dry_run))

    assert hold.removed == {} and hold.kept == {"j": "cannot-lock"} and hold.held == ()
    assert (folder / "repo" / "README").exists() and not (root / "jobs" / "j.cleanup").exists()


def test_a_claim_moves_a_set_aside_checkout_back_and_leaves_one_beside_a_present_folder(
    tmp_path: Path,
) -> None:
    root = work_root(tmp_path)
    jobs = root / "jobs"
    runner = checkout_folder(tmp_path, root, "j")
    (jobs / "j").rename(jobs / "j.cleanup")

    assert "reused" in runner.checkout()
    assert (jobs / "j" / "README").exists() and not (jobs / "j.cleanup").exists()

    (jobs / "j.cleanup").mkdir()
    (jobs / "j.cleanup" / "stale").write_text("x")
    assert "reused" in runner.checkout()
    assert (jobs / "j.cleanup" / "stale").exists() and (jobs / "j" / "README").exists()


DEAD_BOOT = "00000000-0000-0000-0000-000000000000"
TAG = "0123456789abcdef0123456789abcdef"


def snapshot(jobs: Path, job_id: str) -> dict[str, bytes | None]:
    paths = [jobs / job_id, *(jobs / job_id).rglob("*"),
             *(path for path in jobs.iterdir() if path.name.startswith(f"{job_id}."))]
    return {str(path.relative_to(jobs)): None if path.is_dir() or path.is_symlink()
            else path.read_bytes() for path in paths if ".tree.pid" not in path.name}


def finished_with_leftovers(root: Path, job_id: str, *, tag: str = TAG) -> Path:
    folder = job_folder(root, job_id)
    jobs = root / "jobs"
    (folder / ".orch-worker.pid").write_text(f"99999 {DEAD_BOOT}\n")
    (folder / f".orch-worker-{tag}.pid").write_text(
        f"99999 {DEAD_BOOT} {tag} Mon Jan  1 00:00:00 2024\n")
    (jobs / f"{job_id}.run" / tag).mkdir()
    (jobs / f"{job_id}.claimed").touch()
    (jobs / f"{job_id}.lock").touch()
    for path in (folder, *jobs.glob(f"{job_id}.*")):
        age(path, OLD - HOUR)
    return folder


def live_cases_unchanged(root: Path, job_id: str, reason: str,
                         rows: list[tuple[str, str, str, float | None]], tmp_path: Path,
                         scan=job_cleanup.jobs_held_open) -> None:
    jobs = root / "jobs"
    before = snapshot(jobs, job_id)
    db = database(tmp_path, rows)

    [(_, report)] = job_cleanup.run(db, machines(tmp_path, "pc"), InProcessTransport(root, scan),
                                    now=NOW, grace_hours=6, dry_run=False)

    assert report.removed == {} and report.kept == {job_id: reason}
    assert snapshot(jobs, job_id) == before
    assert not (root / "jobs-kept").exists()


def test_leftover_claimed_and_lock_from_the_jobs_own_run_do_not_block(tmp_path: Path) -> None:
    root = work_root(tmp_path)
    folder = finished_with_leftovers(root, "j")
    db = database(tmp_path, [("j", "t1", "done", OLD)])

    [(_, report)] = job_cleanup.run(db, machines(tmp_path, "pc"), InProcessTransport(root),
                                    now=NOW, grace_hours=6, dry_run=False)

    jobs = root / "jobs"
    assert set(report.removed) == {"j"} and report.kept == {}
    assert not folder.exists()
    assert not (jobs / "j.claimed").exists() and not (jobs / "j.launched").exists()
    assert not (jobs / "j.lock").exists()


def test_a_collected_job_leaves_nothing_under_jobs(tmp_path: Path) -> None:
    root = work_root(tmp_path)
    finished_with_leftovers(root, "j")
    db = database(tmp_path, [("j", "t1", "done", OLD)])

    [(_, report)] = job_cleanup.run(db, machines(tmp_path, "pc"), InProcessTransport(root),
                                    now=NOW, grace_hours=6, dry_run=False)

    assert set(report.removed) == {"j"}
    assert list((root / "jobs").iterdir()) == []


class PlanFromAnOlderHost(InProcessTransport):
    def __init__(self, root: Path):
        super().__init__(root)
        self.sent: list[dict] = []

    def call(self, argv, *, timeout: float, input: bytes | None = None) -> Called:
        sent = json.loads(input.decode())
        self.sent.append(sent)
        older = {key: value for key, value in sent.items() if key != "unlink_lock"}
        return super().call(argv, timeout=timeout, input=json.dumps(older).encode())


def test_a_plan_from_an_older_host_leaves_the_lock(tmp_path: Path) -> None:
    root = work_root(tmp_path)
    folder = finished_with_leftovers(root, "j")
    transport = PlanFromAnOlderHost(root)

    [(_, report)] = job_cleanup.run(database(tmp_path, DONE), machines(tmp_path, "pc"),
                                    transport, now=NOW, grace_hours=6, dry_run=False)

    assert set(report.removed) == {"j"} and not folder.exists()
    assert [path.name for path in (root / "jobs").iterdir()] == ["j.lock"]
    assert [(plan["step"], plan["unlink_lock"]) for plan in transport.sent] == [
        (job_cleanup.HOLD, True), (job_cleanup.FINISH, True)]


def no_process_holds(jobs: Path) -> set[str]:
    return set()


def test_a_finish_that_waited_out_another_finish_leaves_nothing_under_jobs(
    tmp_path: Path, monkeypatch
) -> None:
    root = work_root(tmp_path)
    jobs = root / "jobs"
    job_folder(root, "j")
    cutoff = NOW - 6 * HOUR
    hold = Plan(frozenset({"j"}), cutoff, False, unlink_lock=True)
    finish = Plan(frozenset({"j"}), cutoff, False, job_cleanup.FINISH, unlink_lock=True)
    assert job_cleanup.device_step(root, hold, scan=no_process_holds).held == ("j",)
    waiting = threading.Event()
    reports: dict[str, job_cleanup.Report] = {}
    real_flock, real_save = fcntl.flock, job_leftovers.save_work
    second = threading.Thread(target=lambda: reports.__setitem__(
        "second", job_cleanup.device_step(root, finish, scan=no_process_holds)))

    def flock(fd, operation):
        if threading.current_thread() is second:
            waiting.set()
        return real_flock(fd, operation)

    def save_while_the_second_waits(*args):
        second.start()
        assert waiting.wait(30), "the second finish never waited on the lock"
        return real_save(*args)

    monkeypatch.setattr(job_cleanup.fcntl, "flock", flock)
    monkeypatch.setattr(job_leftovers, "save_work", save_while_the_second_waits)
    reports["first"] = job_cleanup.device_step(root, finish, scan=no_process_holds)
    second.join(30)

    assert not second.is_alive() and set(reports["first"].removed) == {"j"}
    assert [path.name for path in jobs.iterdir()] == []


def test_a_lock_left_by_a_pass_that_stopped_after_retiring_goes_with_the_next_pass(
    tmp_path: Path, monkeypatch
) -> None:
    root = work_root(tmp_path)
    jobs = root / "jobs"
    finished_with_leftovers(root, "j")
    real_commit = job_leftovers.commit_record

    def commit_then_stop(*args) -> int:
        real_commit(*args)
        raise SystemExit(1)

    with monkeypatch.context() as stopping:
        stopping.setattr(job_leftovers, "commit_record", commit_then_stop)
        with pytest.raises(SystemExit):
            device_pass(root, Plan(frozenset({"j"}), NOW - 6 * HOUR, False, unlink_lock=True),
                        scan=no_process_holds)
    assert [path.name for path in jobs.iterdir()] == ["j.lock"]
    assert (root / "jobs-kept" / "j" / "kept.json").exists()

    [(_, report)] = job_cleanup.run(database(tmp_path, DONE), machines(tmp_path, "pc"),
                                    InProcessTransport(root, no_process_holds),
                                    now=NOW, grace_hours=6, dry_run=False)

    assert report.kept == {}
    assert [path.name for path in jobs.iterdir()] == []


@pytest.mark.parametrize("case", ["held", "claimed", "not-named", "older-host", "dry-run"])
def test_a_lone_lock_stays_while_claimed_or_not_retired_by_the_plan(
    tmp_path: Path, case: str
) -> None:
    root = work_root(tmp_path)
    jobs = root / "jobs"
    jobs.mkdir(parents=True)
    (jobs / "j.lock").touch()
    if case == "claimed":
        (jobs / "j.claimed").touch()
    plan = Plan(frozenset() if case == "not-named" else frozenset({"j"}), NOW - 6 * HOUR,
                case == "dry-run", unlink_lock=case != "older-host")
    holder = hold_lock(jobs / "j.lock", 60) if case == "held" else None
    try:
        report = job_cleanup.device_step(root, plan, scan=no_process_holds)
    finally:
        if holder is not None:
            holder.kill()
            holder.wait()

    assert report.removed == {}
    assert "j.lock" in [path.name for path in jobs.iterdir()]


def stale_worktree(tmp_path: Path, root: Path, job_id: str = "j") -> Path:
    origin = tmp_path / "origin"
    origin.mkdir()
    git = ["git", "-C", str(origin), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    (origin / "tracked.txt").write_text("original\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "initial"], check=True)
    head = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()
    checkout = worktrees.prepare(root, job_id, "acme/widgets", head,
                                 url_template=str(origin), pid=dead_pid())
    jobs = root / "jobs"
    (jobs / f"{job_id}.claimed").touch()
    (jobs / f"{job_id}.lock").touch()
    return checkout.path


def make_work(folder: Path, work: str) -> None:
    if work == "uncommitted-edit":
        (folder / "tracked.txt").write_text("valuable uncommitted edit\n")
    elif work == "untracked-file":
        (folder / "notes.md").write_text("valuable new file\n")
    else:
        (folder / "tracked.txt").write_text("committed, never pushed\n")
        subprocess.run(["git", "-C", str(folder), "-c", "user.name=t", "-c",
                        "user.email=t@example.invalid", "commit", "-qam", "work"], check=True)


def age_job(root: Path, job_id: str) -> None:
    for path in (root / "jobs").glob(f"{job_id}*"):
        age(path, OLD - HOUR)


CLONE = Path("clones") / "acme" / "widgets.git"


def git_out(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def registered(root: Path) -> list[str]:
    return [line.removeprefix("worktree ") for line in
            git_out(root / CLONE, "worktree", "list", "--porcelain").splitlines()
            if line.startswith("worktree ") and "/jobs/" in line]


def pinned(root: Path, ref: str = "refs/orch-kept/j") -> str:
    return git_out(root / CLONE, "rev-parse", "--verify", ref)


def collect_stale(tmp_path: Path, root: Path) -> job_cleanup.Report:
    age_job(root, "j")
    db = tmp_path / "orch.db"
    [(_, report)] = job_cleanup.run(db if db.exists() else database(tmp_path, DONE),
                                    machines(tmp_path, "pc"),
                                    InProcessTransport(root), now=NOW, grace_hours=6,
                                    dry_run=False)
    return report


def assert_retired(root: Path, report: job_cleanup.Report) -> None:
    jobs = root / "jobs"
    assert set(report.removed) == {"j"} and report.kept == {}
    assert not (jobs / "j").exists() and not (jobs / "j.json").exists()
    assert registered(root) == []


def test_a_dead_owners_worktree_with_an_unpushed_commit_is_retired_and_pinned(
    tmp_path: Path
) -> None:
    root = work_root(tmp_path)
    folder = stale_worktree(tmp_path, root)
    make_work(folder, "unpushed-commit")
    commit = git_out(folder, "rev-parse", "HEAD")

    assert_retired(root, collect_stale(tmp_path, root))

    assert pinned(root) == commit
    assert git_out(root / CLONE, "show", f"{commit}:tracked.txt") == "committed, never pushed"
    assert not (root / "jobs-kept" / "j" / "worktree.patch").exists()


def apply_to_a_fresh_worktree(tmp_path: Path, root: Path, start: str, patch: Path) -> Path:
    fresh = tmp_path / "fresh"
    git_out(root / CLONE, "worktree", "add", "-q", "--detach", str(fresh), start)
    git_out(fresh, "apply", "--check", str(patch))
    git_out(fresh, "apply", str(patch))
    return fresh


UNCOMMITTED = {
    "tracked-edit": ("tracked.txt", b"valuable uncommitted edit\n"),
    "untracked-file": ("notes.md", b"valuable new file\n"),
    "new-file-one-level-down": ("docs/plan.md", b"\x00binary\xffplan\n"),
}


@pytest.mark.parametrize("work", UNCOMMITTED)
def test_a_dead_owners_uncommitted_work_is_saved_as_a_patch_that_applies(
    tmp_path: Path, work: str
) -> None:
    root = work_root(tmp_path)
    folder = stale_worktree(tmp_path, root)
    start = json.loads((root / "jobs" / "j.json").read_text())["start"]
    name, content = UNCOMMITTED[work]
    (folder / name).parent.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(content)

    assert_retired(root, collect_stale(tmp_path, root))

    patch = root / "jobs-kept" / "j" / "worktree.patch"
    fresh = apply_to_a_fresh_worktree(tmp_path, root, start, patch)
    assert (fresh / name).read_bytes() == content
    assert git_out(root / CLONE, "for-each-ref", "refs/orch-kept/") == ""


def test_work_made_in_a_dead_owners_worktree_after_hold_is_saved_at_finish(
    tmp_path: Path
) -> None:
    root = work_root(tmp_path)
    stale_worktree(tmp_path, root)
    start = json.loads((root / "jobs" / "j.json").read_text())["start"]
    age_job(root, "j")
    jobs = root / "jobs"
    plan = Plan(frozenset({"j"}), NOW - 6 * HOUR, False, finished_at={"j": OLD})
    assert job_cleanup.device_step(root, plan).held == ("j",)
    make_work(jobs / "j.cleanup", "uncommitted-edit")

    done = job_cleanup.device_step(
        root, Plan(frozenset({"j"}), plan.cutoff, False, job_cleanup.FINISH, {"j": OLD}))

    assert set(done.removed) == {"j"} and done.kept == {}
    assert not (jobs / "j.json").exists() and registered(root) == []
    fresh = apply_to_a_fresh_worktree(tmp_path, root, start,
                                      root / "jobs-kept" / "j" / "worktree.patch")
    assert (fresh / "tracked.txt").read_text() == "valuable uncommitted edit\n"


def test_a_dead_owners_clean_worktree_is_retired_with_its_record(tmp_path: Path) -> None:
    root = work_root(tmp_path)
    stale_worktree(tmp_path, root)

    assert_retired(root, collect_stale(tmp_path, root))

    jobs = root / "jobs"
    assert not (jobs / "j.claimed").exists()
    assert not (root / "jobs-kept" / "j" / "worktree.patch").exists()
    assert git_out(root / CLONE, "for-each-ref", "refs/orch-kept/") == ""


def test_a_requeued_job_retired_twice_keeps_both_pins_and_both_patches(tmp_path: Path) -> None:
    root = work_root(tmp_path)
    folder = stale_worktree(tmp_path, root)
    make_work(folder, "unpushed-commit")
    (folder / "first.txt").write_text("first run\n")
    first = git_out(folder, "rev-parse", "HEAD")
    assert_retired(root, collect_stale(tmp_path, root))
    again = worktrees.prepare(root, "j", "acme/widgets", git_out(tmp_path / "origin", "rev-parse",
                              "HEAD"), url_template=str(tmp_path / "origin"), pid=dead_pid())
    (again.path / "tracked.txt").write_text("second run\n")
    subprocess.run(["git", "-C", str(again.path), "-c", "user.name=t", "-c",
                    "user.email=t@example.invalid", "commit", "-qam", "second"], check=True)
    (again.path / "second.txt").write_text("second run\n")
    second = git_out(again.path, "rev-parse", "HEAD")

    assert_retired(root, collect_stale(tmp_path, root))

    assert {pinned(root), pinned(root, "refs/orch-kept/j.1")} == {first, second}
    kept = root / "jobs-kept" / "j"
    patches = sorted(path.read_text() for path in kept.glob("worktree.patch*"))
    assert len(patches) == 2
    assert any("first run" in text for text in patches)
    assert any("second run" in text for text in patches)


def test_a_dead_owners_record_over_a_folder_that_is_not_its_worktree_keeps_it(
    tmp_path: Path
) -> None:
    root = work_root(tmp_path)
    stale_worktree(tmp_path, root, "other")
    finished_with_leftovers(root, "j")
    record = json.loads((root / "jobs" / "other.json").read_text())
    shutil.rmtree(root / "jobs" / "other")
    (root / "jobs" / "j.json").write_text(json.dumps({**record, "job_id": "j"}))
    age(root / "jobs" / "j.json", OLD - HOUR)

    live_cases_unchanged(root, "j", "worktree-record", DONE, tmp_path)


def orphan_record(tmp_path: Path, root: Path, owner: int) -> str:
    folder = stale_worktree(tmp_path, root)
    make_work(folder, "unpushed-commit")
    commit = git_out(folder, "rev-parse", "HEAD")
    shutil.rmtree(folder)
    record = root / "jobs" / "j.json"
    record.write_text(json.dumps({**json.loads(record.read_text()), "pid": owner}))
    (root / "jobs" / "j.claimed").unlink()
    return commit


def test_an_orphan_record_with_a_dead_owner_is_unlinked_and_its_commit_pinned(
    tmp_path: Path
) -> None:
    root = work_root(tmp_path)
    commit = orphan_record(tmp_path, root, dead_pid())

    report = collect_stale(tmp_path, root)

    assert report.removed == {"j": 0} and report.kept == {}
    assert list((root / "jobs").iterdir()) == []
    assert registered(root) == []
    assert pinned(root) == commit


def test_an_orphan_record_with_a_live_owner_is_kept(tmp_path: Path) -> None:
    root = work_root(tmp_path)
    orphan_record(tmp_path, root, os.getpid())
    before = (root / "jobs" / "j.json").read_bytes()

    report = collect_stale(tmp_path, root)

    assert report.removed == {} and report.kept == {"j": "worktree-owner-live"}
    assert (root / "jobs" / "j.json").read_bytes() == before
    assert len(registered(root)) == 1


def test_an_orphan_record_the_plan_does_not_name_is_untouched(tmp_path: Path) -> None:
    root = work_root(tmp_path)
    orphan_record(tmp_path, root, dead_pid())
    before = (root / "jobs" / "j.json").read_bytes()
    age_job(root, "j")

    [(_, report)] = job_cleanup.run(database(tmp_path, [("j", "t1", "running", None)]),
                                    machines(tmp_path, "pc"), InProcessTransport(root),
                                    now=NOW, grace_hours=6, dry_run=False)

    assert report.removed == {} and "j" not in report.kept
    assert (root / "jobs" / "j.json").read_bytes() == before
    assert len(registered(root)) == 1


def commit_in(folder: Path, text: str) -> str:
    (folder / "tracked.txt").write_text(text)
    subprocess.run(["git", "-C", str(folder), "-c", "user.name=t", "-c",
                    "user.email=t@example.invalid", "commit", "-qam", text], check=True)
    return git_out(folder, "rev-parse", "HEAD")


def survives_gc(root: Path, commit: str) -> bool:
    clone = root / CLONE
    git_out(clone, "reflog", "expire", "--expire=now", "--all")
    git_out(clone, "gc", "-q", "--prune=now")
    return subprocess.run(["git", "-C", str(clone), "cat-file", "-e", commit]).returncode == 0


def test_every_orphan_in_one_clone_keeps_its_commit_when_one_is_retired(
    tmp_path: Path
) -> None:
    root = work_root(tmp_path)
    commits = {"j": commit_in(stale_worktree(tmp_path, root), "j never pushed\n")}
    k = worktrees.prepare(root, "k", "acme/widgets", None,
                          url_template=str(tmp_path / "origin"), pid=dead_pid())
    commits["k"] = commit_in(k.path, "k never pushed\n")
    for job_id in commits:
        shutil.rmtree(root / "jobs" / job_id)
        age_job(root, job_id)
    database(tmp_path, [*DONE, ("k", "t2", "done", OLD)])

    report = collect_stale(tmp_path, root)

    assert set(report.removed) == {"j", "k"} and report.kept == {}
    assert {job_id: pinned(root, f"refs/orch-kept/{job_id}") for job_id in commits} == commits
    assert all(survives_gc(root, commit) for commit in commits.values())


def test_finishing_another_job_pins_a_missing_worktrees_unpushed_commit(
    tmp_path: Path
) -> None:
    root = work_root(tmp_path)
    commit = commit_in(stale_worktree(tmp_path, root), "never pushed\n")
    shutil.rmtree(root / "jobs" / "j")
    other = worktrees.prepare(root, "k", "acme/widgets", None,
                              url_template=str(tmp_path / "origin"), pid=dead_pid())

    assert worktrees.finish(other, lambda _: None)

    assert pinned(root) == commit and survives_gc(root, commit)


def test_staged_work_that_differs_from_the_working_file_is_saved(tmp_path: Path) -> None:
    root = work_root(tmp_path)
    folder = stale_worktree(tmp_path, root)
    start = json.loads((root / "jobs" / "j.json").read_text())["start"]
    (folder / "tracked.txt").write_text("unique staged work\n")
    git_out(folder, "add", "tracked.txt")
    (folder / "tracked.txt").write_text("different working edit\n")

    assert_retired(root, collect_stale(tmp_path, root))

    kept = root / "jobs-kept" / "j"
    staged = apply_to_a_fresh_worktree(tmp_path, root, start, kept / "index.patch")
    assert (staged / "tracked.txt").read_text() == "unique staged work\n"
    shutil.rmtree(staged)
    git_out(root / CLONE, "worktree", "prune")
    working = apply_to_a_fresh_worktree(tmp_path, root, start, kept / "worktree.patch")
    assert (working / "tracked.txt").read_text() == "different working edit\n"


def hold_then_finish(root: Path, between: Callable[[], None]) -> job_cleanup.Report:
    age_job(root, "j")
    plan = Plan(frozenset({"j"}), NOW - 6 * HOUR, False, finished_at={"j": OLD})
    assert job_cleanup.device_step(root, plan, scan=lambda _: set()).held == ("j",)
    between()
    return job_cleanup.device_step(
        root, Plan(frozenset({"j"}), plan.cutoff, False, job_cleanup.FINISH, {"j": OLD}),
        scan=lambda _: set())


def test_gc_between_hold_and_finish_leaves_the_work_to_be_saved(tmp_path: Path) -> None:
    root = work_root(tmp_path)
    folder = stale_worktree(tmp_path, root)
    start = json.loads((root / "jobs" / "j.json").read_text())["start"]
    commit = commit_in(folder, "never pushed\n")
    (folder / "notes.md").write_text("valuable uncommitted note\n")

    done = hold_then_finish(root, lambda: worktrees.gc(root, older_than_hours=0,
                                                         log=lambda _: None, now=NOW))

    assert set(done.removed) == {"j"} and done.kept == {}
    assert pinned(root) == commit
    fresh = apply_to_a_fresh_worktree(tmp_path, root, start,
                                      root / "jobs-kept" / "j" / "worktree.patch")
    assert (fresh / "notes.md").read_text() == "valuable uncommitted note\n"


def test_a_set_aside_worktree_whose_record_vanished_is_kept(tmp_path: Path) -> None:
    root = work_root(tmp_path)
    folder = stale_worktree(tmp_path, root)
    (folder / "notes.md").write_text("valuable uncommitted note\n")

    done = hold_then_finish(root, (root / "jobs" / "j.json").unlink)

    assert done.removed == {} and done.kept["j"].startswith("error:")
    assert (root / "jobs" / "j" / "notes.md").read_text() == "valuable uncommitted note\n"


def test_a_nested_repository_with_its_own_work_keeps_the_folder(tmp_path: Path) -> None:
    root = work_root(tmp_path)
    nested = stale_worktree(tmp_path, root) / "nested"
    nested.mkdir()
    git_out(nested, "init", "-q")
    (nested / "work.txt").write_text("unique nested work\n")
    git_out(nested, "add", ".")
    git_out(nested, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
            "commit", "-qm", "nested")

    report = collect_stale(tmp_path, root)

    assert report.removed == {} and report.kept["j"].startswith("error:")
    assert (root / "jobs" / "j" / "nested" / "work.txt").read_text() == "unique nested work\n"
    assert git_out(root / "jobs" / "j" / "nested", "log", "--format=%s") == "nested"
    assert (root / "jobs" / "j.json").exists()


def detour(folder: Path, text: str) -> str:
    start = git_out(folder, "rev-parse", "HEAD")
    commit = commit_in(folder, text)
    git_out(folder, "reset", "-q", "--hard", start)
    return commit


def stop_on_merge_conflict(folder: Path) -> str:
    incoming = detour(folder, "unique incoming merge work\n")
    commit_in(folder, "ours\n")
    merge = subprocess.run(["git", "-C", str(folder), "-c", "user.name=t", "-c",
                            "user.email=t@example.invalid", "merge", "--no-edit", incoming],
                           capture_output=True, text=True)
    assert merge.returncode == 1 and "CONFLICT" in merge.stdout, merge.stderr
    assert git_out(folder, "rev-parse", "MERGE_HEAD") == incoming
    return incoming


def conflicted_merge(folder: Path) -> str:
    stop_on_merge_conflict(folder)
    (folder / "tracked.txt").write_text("ours\n")
    return git_out(folder, "rev-parse", ":3:tracked.txt")


def resolved_merge(folder: Path) -> str:
    incoming = stop_on_merge_conflict(folder)
    git_out(folder, "add", "tracked.txt")
    assert git_out(folder, "ls-files", "--unmerged") == ""
    return incoming


def stopped_rebase(folder: Path) -> str:
    first = commit_in(folder, "first\n")
    later = commit_in(folder, "unique later work\n")
    subprocess.run(["git", "-C", str(folder), "-c", "user.name=t",
                    "-c", "user.email=t@example.invalid", "rebase", "-q",
                    "--exec", "false", "--force-rebase", f"{first}~1"], capture_output=True)
    assert (folder / "tracked.txt").read_text() == "first\n"
    return later


@pytest.mark.parametrize("state", [conflicted_merge, resolved_merge, stopped_rebase])
def test_index_or_operation_state_no_patch_holds_keeps_the_folder(
        tmp_path: Path, state) -> None:
    root = work_root(tmp_path)
    folder = stale_worktree(tmp_path, root)
    work = state(folder)

    report = collect_stale(tmp_path, root)

    assert report.removed == {} and report.kept["j"].startswith("error:")
    assert (root / "jobs" / "j.json").exists()
    assert git_out(root / "jobs" / "j", "cat-file", "-t", work) in ("blob", "commit")


def test_gc_keeps_a_clean_worktree_whose_rebase_holds_the_only_copy(tmp_path: Path) -> None:
    root = work_root(tmp_path)
    folder = stale_worktree(tmp_path, root)
    start = git_out(folder, "rev-parse", "HEAD")
    later = commit_in(folder, "unique later work\n")
    git_out(folder, "-c", "sequence.editor=printf 'break\\n' >", "rebase", "-q", "-i", start)
    assert git_out(folder, "rev-parse", "HEAD") == start
    assert git_out(folder, "status", "--porcelain", "--untracked-files=all") == ""
    logged: list[str] = []

    worktrees.gc(root, older_than_hours=0, log=logged.append, now=NOW)

    assert folder.exists(), logged
    assert git_out(folder, "cat-file", "-t", later) == "commit"


def test_a_held_lock_keeps_the_folder_without_waiting(tmp_path: Path) -> None:
    root = work_root(tmp_path)
    finished_with_leftovers(root, "j")
    holder = hold_lock(root / "jobs" / "j.lock", 60)
    began = time.monotonic()
    try:
        live_cases_unchanged(root, "j", "lock-held", [("j", "t1", "done", OLD)], tmp_path)
    finally:
        holder.kill()
        holder.wait()
    assert time.monotonic() - began < 30


def test_an_unsure_worker_check_keeps_the_folder(tmp_path: Path, monkeypatch) -> None:
    root = work_root(tmp_path)
    finished_with_leftovers(root, "j")
    seen = []
    monkeypatch.setattr(job_cleanup.remote, "worker_check_script",
                        lambda job_dir, tag: seen.append((job_dir, tag)) or "exit 3")
    live_cases_unchanged(root, "j", "worker-unsure", [("j", "t1", "done", OLD)], tmp_path)
    assert seen == [(str(root / "jobs" / "j"), TAG)]


@pytest.mark.skipif(sys.platform != "linux", reason="the Linux check reads /proc environments")
def test_a_live_tagged_worker_keeps_the_folder_on_linux(tmp_path: Path) -> None:
    root = work_root(tmp_path)
    finished_with_leftovers(root, "j")
    worker = subprocess.Popen(["sleep", "60"], cwd=tmp_path,
                              env={**os.environ, remote.TAG_ENV: TAG})
    try:
        live_cases_unchanged(root, "j", "worker-live", [("j", "t1", "done", OLD)], tmp_path)
    finally:
        worker.kill()
        worker.wait()


@pytest.mark.skipif(sys.platform != "darwin", reason="the Darwin check reads the start's record")
def test_a_live_recorded_worker_keeps_the_folder_on_darwin(tmp_path: Path) -> None:
    root = work_root(tmp_path)
    folder = finished_with_leftovers(root, "j")
    worker = subprocess.Popen(["sleep", "60"], cwd=tmp_path, start_new_session=True)
    try:
        boot = subprocess.run(["sysctl", "-n", "kern.bootsessionuuid"], capture_output=True,
                              text=True, check=True).stdout.strip()
        started = " ".join(subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(worker.pid)], capture_output=True, text=True,
            check=True, env={**os.environ, "LC_ALL": "C", "TZ": "UTC0"}).stdout.split())
        record = folder / f".orch-worker-{TAG}.pid"
        record.write_text(f"{worker.pid} {boot} {TAG} {started}\n")
        age(record, OLD - HOUR)
        live_cases_unchanged(root, "j", "worker-live", [("j", "t1", "done", OLD)], tmp_path)
    finally:
        worker.kill()
        worker.wait()


def test_a_worker_that_starts_between_hold_and_finish_keeps_the_folder(
    tmp_path: Path, monkeypatch
) -> None:
    root = work_root(tmp_path)
    finished_with_leftovers(root, "j")
    jobs = root / "jobs"
    plan = Plan(frozenset({"j"}), NOW - 6 * HOUR, False, finished_at={"j": OLD})
    hold = job_cleanup.device_step(root, plan)
    assert hold.held == ("j",)
    monkeypatch.setattr(job_cleanup.remote, "worker_check_script", lambda job_dir, tag: "exit 1")

    done = job_cleanup.device_step(
        root, Plan(frozenset({"j"}), plan.cutoff, False, job_cleanup.FINISH, {"j": OLD}))

    assert done.removed == {} and done.kept == {"j": "worker-live"}
    assert (jobs / "j" / "repo" / "README").exists() and not (jobs / "j.cleanup").exists()


def test_the_plan_carries_each_jobs_finish_time_to_the_device(tmp_path: Path) -> None:
    plan = Plan(frozenset({"a", "b"}), NOW, True, finished_at={"a": OLD, "b": OLD + 1},
                unlink_lock=True)
    assert Plan.parse(plan.to_json()) == plan
    legacy = Plan.parse(json.dumps({"finished": ["a"], "cutoff": NOW, "dry_run": False}))
    assert legacy.own_finish("a") == NOW and not legacy.unlink_lock
    with pytest.raises(ValueError):
        Plan.parse(json.dumps({"finished": ["a"], "cutoff": NOW, "dry_run": False,
                               "finished_at": {"../x": 1.0}}))


def dead_pid() -> int:
    child = subprocess.Popen(["true"])
    child.wait()
    return child.pid


def test_a_dry_run_reports_a_held_lock_and_writes_nothing(tmp_path: Path) -> None:
    root = work_root(tmp_path)
    finished_with_leftovers(root, "held")
    job_folder(root, "free")
    jobs = root / "jobs"
    holder = hold_lock(jobs / "held.lock", 60)
    try:
        before = sorted(path.name for path in jobs.iterdir())
        dry = job_cleanup.device_step(
            root, Plan(frozenset({"held", "free"}), NOW - 6 * HOUR, True))
    finally:
        holder.kill()
        holder.wait()

    assert dry.kept == {"held": "lock-held"} and set(dry.removed) == {"free"}
    assert sorted(path.name for path in jobs.iterdir()) == before


def files_under(top: Path) -> list[str]:
    return sorted(Path(base, name).relative_to(top).as_posix()
                  for base, _, names in os.walk(top) for name in names)


def git_checkout(path: Path) -> None:
    (path / "src").mkdir(parents=True)
    (path / "README.md").write_bytes(os.urandom(64))
    (path / "src" / "x.py").write_bytes(os.urandom(64))
    for args in (["init", "-q", "-b", "main"], ["add", "README.md", "src/x.py"],
                 ["commit", "-qm", "one"]):
        subprocess.run(["git", "-C", str(path), *args], env={**os.environ, **GIT_ENV},
                       check=True, capture_output=True)


def write_random(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(256))
    return path.read_bytes()


@pytest.mark.parametrize("layout", ["remote", "local"])
def test_archive_mirrors_jobs_and_drops_the_trees(tmp_path: Path, layout: str) -> None:
    root = work_root(tmp_path)
    jobs = root / "jobs"
    folder = jobs / "j"
    git_checkout(folder if layout == "remote" else folder / "repo")
    untracked = (["handoff.md", "gate.log", "prbody.md"] if layout == "remote"
                 else ["repo/handoff.md", "handoff.md"])
    originals = {f"j/{name}": write_random(folder / name) for name in untracked}
    record = folder / f".orch-worker-{TAG}.pid"
    record.write_text(f"99999 {DEAD_BOOT} {TAG} Mon Jan  1 00:00:00 2024\n")
    originals[f"j/{record.name}"] = record.read_bytes()
    for name in ("venv/bin/python", "interrogate-a/f"):
        write_random(folder / name)
    for name in (f"j.run/{TAG}/log", f"j.run/{TAG}/handoff", f"j.run/{TAG}/exit",
                 "j.brief.md", "j.launched"):
        originals[name] = write_random(jobs / name)
    for name in ("j.claimed", "j.lock"):
        (jobs / name).touch()
    for path in jobs.iterdir():
        age(path, OLD - HOUR)
    db = database(tmp_path, [("j", "t1", "done", OLD)])

    [(_, report)] = job_cleanup.run(db, machines(tmp_path, "pc"), InProcessTransport(root),
                                    now=NOW, grace_hours=6, dry_run=False)

    assert set(report.removed) == {"j"} and report.kept == {}
    kept = root / "jobs-kept" / "j"
    check_lock = [f"j/.orch-worker-{TAG}.tree.pid.lock"] if sys.platform == "linux" else []
    archived = sorted([*originals, *check_lock])
    assert files_under(kept) == sorted([*archived, "kept.json"])
    assert {name: (kept / name).read_bytes() for name in originals} == originals
    assert not [path for path in (root / "jobs-kept").rglob("*")
                if path.name in ("README.md", "src", ".git", "venv")
                or path.name.startswith("interrogate-")]
    written = json.loads((kept / "kept.json").read_text())
    assert written["archived"] == archived
    assert (written["id"], written["finished_at"]) == ("j", OLD)
    assert written["removed_bytes"] == report.removed["j"]
    for name in ("j", "j.claimed", "j.launched", "j.gone", "j.cleanup", "j.lock"):
        assert not os.path.lexists(jobs / name), name


@pytest.mark.parametrize("layout", ["remote", "local"])
def test_scratch_trees_with_their_own_git_are_not_archived(tmp_path: Path, layout: str) -> None:
    root = work_root(tmp_path)
    folder = root / "jobs" / "j"
    checkout = folder if layout == "remote" else folder / "repo"
    git_checkout(checkout)
    subprocess.run(["git", "-C", str(checkout), "worktree", "add", "-q", "--detach",
                    str(folder / "interrogate-a"), "HEAD"],
                   env={**os.environ, **GIT_ENV}, check=True, capture_output=True)
    git_checkout(folder / "venv")
    for tree in ("interrogate-a", "venv"):
        write_random(folder / tree / "review.log")
    handoff = write_random(folder / "handoff.md")
    gone = folder.with_name("j.gone")
    folder.rename(gone)

    job_leftovers.retire(root, "j", gone, siblings=True, finished_at=OLD, removed_bytes=0)

    kept = root / "jobs-kept" / "j"
    assert files_under(kept) == ["j/handoff.md", "kept.json"]
    assert (kept / "j" / "handoff.md").read_bytes() == handoff
    assert json.loads((kept / "kept.json").read_text())["archived"] == ["j/handoff.md"]


def test_kept_json_is_written_last(tmp_path: Path, monkeypatch) -> None:
    root = work_root(tmp_path)
    finished_with_leftovers(root, "j")
    jobs = root / "jobs"
    monkeypatch.setattr(job_leftovers.scratch, "remove",
                        lambda path: f"could not remove {path}: refused")
    db = database(tmp_path, [("j", "t1", "done", OLD)])

    [(_, report)] = job_cleanup.run(db, machines(tmp_path, "pc"), InProcessTransport(root),
                                    now=NOW, grace_hours=6, dry_run=False)

    kept = root / "jobs-kept" / "j"
    assert report.removed == {} and report.kept["j"].startswith("error:")
    assert (kept / "j" / "top-file").exists() and (kept / "j.run" / "tag" / "log").exists()
    assert not (kept / "kept.json").exists()
    assert (jobs / "j.gone").is_dir() and not (jobs / "j").exists()


def test_archiving_the_same_id_twice_keeps_both_copies(tmp_path: Path) -> None:
    root = work_root(tmp_path)
    jobs = root / "jobs"
    gone = jobs / "j.gone"
    written = {}
    for suffix in ("", ".1"):
        gone.mkdir(parents=True)
        written[f"j/handoff.md{suffix}"] = write_random(gone / "handoff.md")
        written[f"j.run{suffix}/tag/log"] = write_random(jobs / "j.run" / "tag" / "log")
        written[f"j.brief.md{suffix}"] = write_random(jobs / "j.brief.md")
        job_leftovers.archive(root, "j", gone, siblings=True)
        gone.rmdir()

    kept = root / "jobs-kept" / "j"
    assert files_under(kept) == sorted(written)
    assert {name: (kept / name).read_bytes() for name in written} == written


def test_a_cross_device_jobs_kept_keeps_the_folder_with_an_error(
    tmp_path: Path, monkeypatch
) -> None:
    root = work_root(tmp_path)
    finished_with_leftovers(root, "j")
    jobs = root / "jobs"
    assert job_leftovers.cross_device(root) is None
    real_stat = os.stat

    def stat(path, *args, **kwargs):
        found = real_stat(path, *args, **kwargs)
        if Path(path) != jobs:
            return found
        return os.stat_result((*found[:2], found.st_dev + 1, *found[3:]))

    with monkeypatch.context() as patched:
        patched.setattr(os, "stat", stat)
        away = job_leftovers.cross_device(root)
    assert away == f"error: {root} is on another device than {jobs}"

    monkeypatch.setattr(job_leftovers, "cross_device", lambda _: away)
    live_cases_unchanged(root, "j", away, [("j", "t1", "done", OLD)], tmp_path)
    assert not (jobs / "j.cleanup").exists() and not (jobs / "j.gone").exists()


def test_finish_refuses_to_commit_onto_an_existing_gone(tmp_path: Path) -> None:
    root = work_root(tmp_path)
    finished_with_leftovers(root, "j")
    jobs = root / "jobs"
    before = snapshot(jobs, "j")
    plan = Plan(frozenset({"j"}), NOW - 6 * HOUR, False, finished_at={"j": OLD})
    assert job_cleanup.device_step(root, plan).held == ("j",)
    (jobs / "j.gone").mkdir()

    done = job_cleanup.device_step(
        root, Plan(frozenset({"j"}), plan.cutoff, False, job_cleanup.FINISH, {"j": OLD}))

    assert done.removed == {} and done.kept == {"j": "error: j.gone is still there"}
    assert snapshot(jobs, "j") == {**before, "j.gone": None}
    assert not (root / "jobs-kept").exists()


def test_an_open_file_under_run_keeps_the_folder(tmp_path: Path) -> None:
    root = work_root(tmp_path)
    finished_with_leftovers(root, "j")
    jobs = root / "jobs"
    sleeper = subprocess.Popen(["sleep", "60"], cwd=jobs / "j.run" / TAG)
    try:
        report = device_pass(root, Plan(frozenset({"j"}), NOW - 6 * HOUR, False))
    finally:
        sleeper.kill()
        sleeper.wait()

    assert report.removed == {} and report.kept == {"j": "held-open"}
    assert (jobs / "j" / "repo" / "README").exists() and not (root / "jobs-kept").exists()


DONE = [("j", "t1", "done", OLD)]


def hold_for_the_test(stack: ExitStack, argv: list[str], **options) -> None:
    process = subprocess.Popen(argv, **options)
    stack.callback(process.wait)
    stack.callback(process.kill)


def no_flock(root, monkeypatch, stack):
    monkeypatch.setattr(job_cleanup.shutil, "which", lambda name: None)
    return DONE, job_cleanup.jobs_held_open


def lock_held(root, monkeypatch, stack):
    holder = hold_lock(root / "jobs" / "j.lock", 60)
    stack.callback(holder.wait)
    stack.callback(holder.kill)
    return DONE, job_cleanup.jobs_held_open


def claimed_since(root, monkeypatch, stack):
    os.utime(root / "jobs" / "j.claimed", (OLD + HOUR, OLD + HOUR))
    return DONE, job_cleanup.jobs_held_open


def worktree_record(root, monkeypatch, stack):
    (root / "jobs" / "j.json").write_text("{not json")
    return DONE, job_cleanup.jobs_held_open


def worktree_owner_live(root, monkeypatch, stack):
    (root / "jobs" / "j.json").write_text(json.dumps({"job_id": "j", "pid": os.getpid()}))
    age(root / "jobs" / "j.json", OLD)
    return DONE, job_cleanup.jobs_held_open


def worker_exits(code: int):
    def arrange(root, monkeypatch, stack):
        monkeypatch.setattr(job_cleanup.remote, "worker_check_script",
                            lambda job_dir, tag: f"exit {code}")
        return DONE, job_cleanup.jobs_held_open
    return arrange


def held_open(root, monkeypatch, stack):
    hold_for_the_test(stack, ["sleep", "60"], cwd=root / "jobs" / "j" / "repo")
    return DONE, job_cleanup.jobs_held_open


def cannot_scan(root, monkeypatch, stack):
    return DONE, lambda _: None


def rows(*given):
    return lambda root, monkeypatch, stack: (list(given), job_cleanup.jobs_held_open)


PART_1_GUARDS = {
    "cannot-lock": no_flock,
    "lock-held": lock_held,
    "claimed-since": claimed_since,
    "worktree-record": worktree_record,
    "worktree-owner-live": worktree_owner_live,
    "worker-live": worker_exits(1),
    "worker-unsure": worker_exits(3),
    "held-open": held_open,
    "cannot-scan": cannot_scan,
    "live": rows(("j", "t1", "running", None)),
    "recent": rows(("j", "t1", "done", NOW - HOUR)),
    "retry-held": rows(("j", "t1", "failed", OLD), ("retry", "t1", "queued", None)),
    "unknown": rows(),
}


@pytest.mark.parametrize("guard", PART_1_GUARDS, ids=list(PART_1_GUARDS))
def test_every_part_1_guard_keeps_its_folder_and_archives_nothing(
    tmp_path: Path, monkeypatch, guard: str
) -> None:
    root = work_root(tmp_path)
    finished_with_leftovers(root, "j")
    with ExitStack() as stack:
        given, scan = PART_1_GUARDS[guard](root, monkeypatch, stack)
        live_cases_unchanged(root, "j", guard, given, tmp_path, scan)


def old_gone_beside(root: Path) -> Path:
    gone = root / "jobs" / "j.gone"
    (gone / "handoff.md").parent.mkdir(parents=True)
    (gone / "handoff.md").write_text("old handoff\n")
    return gone


def current_run(jobs: Path) -> dict[str, bytes | None]:
    return {name: content for name, content in snapshot(jobs, "j").items()
            if not name.startswith("j.gone")}


@pytest.mark.parametrize("guard", PART_1_GUARDS, ids=list(PART_1_GUARDS))
def test_every_part_1_guard_keeps_a_later_checkout_beside_an_old_gone(
    tmp_path: Path, monkeypatch, guard: str
) -> None:
    root = work_root(tmp_path)
    jobs = root / "jobs"
    finished_with_leftovers(root, "j")
    old_gone_beside(root)
    with ExitStack() as stack:
        given, scan = PART_1_GUARDS[guard](root, monkeypatch, stack)
        before = current_run(jobs)

        [(_, report)] = job_cleanup.run(database(tmp_path, given), machines(tmp_path, "pc"),
                                        InProcessTransport(root, scan), now=NOW,
                                        grace_hours=6, dry_run=False)

    assert report.kept == {"j": guard}
    assert current_run(jobs) == before
    kept = root / "jobs-kept" / "j"
    assert not kept.exists() or not any(path.name.startswith("j.") for path in kept.iterdir())


def test_finishing_an_old_gone_leaves_a_later_run_held_open_by_a_process(tmp_path: Path) -> None:
    root = work_root(tmp_path)
    jobs = root / "jobs"
    old_gone_beside(root)
    current = jobs / "j"
    subprocess.run(["sh", "-c", "d=$1; lk=$(command -v flock); " + remote._CLAIM,
                    "claim", str(current)], check=True)
    (current / "repo").mkdir(parents=True)
    (current / "repo" / "source").write_text("new checkout\n")
    reader = jobs / "j.run" / "log-reader"
    reader.mkdir(parents=True)
    with ExitStack() as stack:
        hold_for_the_test(stack, ["sleep", "60"], cwd=reader)
        finished = time.time()
        [(_, report)] = job_cleanup.run(database(tmp_path, [("j", "t1", "done", finished)]),
                                        machines(tmp_path, "pc"), InProcessTransport(root),
                                        now=finished + 1, grace_hours=0, dry_run=False)

        assert report.kept == {"j": "held-open"}
        assert (current / "repo" / "source").read_text() == "new checkout\n"
        assert reader.is_dir() and (jobs / "j.claimed").exists()
        assert not (jobs / "j.gone").exists()
        assert (root / "jobs-kept" / "j" / job_leftovers.KEPT_JSON).exists()


class Crash(BaseException):
    pass


def crash_at(point: str) -> tuple[object, str, Callable]:
    real_archive, real_remove, real_rename = (
        job_leftovers.archive, job_leftovers.scratch.remove, os.rename)
    renames = []

    def at_once(*args, **kwargs):
        raise Crash

    def after(real):
        def call(*args, **kwargs):
            real(*args, **kwargs)
            raise Crash
        return call

    def on_the_third_rename(source, target):
        renames.append(source)
        if len(renames) == 3:
            raise Crash
        real_rename(source, target)

    return {
        "after the rename": (job_leftovers, "archive", at_once),
        "partway through the archive": (job_leftovers.os, "rename", on_the_third_rename),
        "after the archive": (job_leftovers, "archive", after(real_archive)),
        "after .claimed is deleted": (job_leftovers.scratch, "remove", at_once),
        "after the folder is deleted": (job_leftovers.scratch, "remove", after(real_remove)),
    }[point]


CRASH_POINTS = ("after the rename", "partway through the archive", "after the archive",
                "after .claimed is deleted", "after the folder is deleted")


def collect(root: Path, rows) -> job_cleanup.Report:
    [(_, report)] = job_cleanup.run(database(root.parent, rows), machines(root.parent.parent, "pc"),
                                    InProcessTransport(root), now=NOW, grace_hours=6,
                                    dry_run=False)
    return report


def crash_while_finishing(root: Path, point: str, monkeypatch,
                          finished_at: float = OLD, cutoff: float = NOW - 6 * HOUR) -> None:
    plan = Plan(frozenset({"j"}), cutoff, False, finished_at={"j": finished_at})
    assert job_cleanup.device_step(root, plan).held == ("j",)
    with monkeypatch.context() as patched:
        patched.setattr(*crash_at(point))
        with pytest.raises(Crash):
            job_cleanup.device_step(root, Plan(frozenset({"j"}), cutoff, False,
                                               job_cleanup.FINISH, {"j": finished_at}))


def archived(root: Path) -> dict[str, bytes]:
    kept = root / "jobs-kept" / "j"
    return {name: (kept / name).read_bytes() for name in files_under(kept)
            if name != job_leftovers.KEPT_JSON}


@pytest.mark.parametrize("point", CRASH_POINTS)
def test_a_crash_after_the_commit_point_is_finished_by_the_next_pass(
    tmp_path: Path, monkeypatch, point: str
) -> None:
    reference = work_root(tmp_path / "reference")
    finished_with_leftovers(reference, "j")
    assert set(collect(reference, DONE).removed) == {"j"}
    root = work_root(tmp_path / "crashed")
    finished_with_leftovers(root, "j")
    jobs = root / "jobs"
    crash_while_finishing(root, point, monkeypatch)
    assert not (jobs / "j").exists() and not (jobs / "j.cleanup").exists()
    assert not (root / "jobs-kept" / "j" / job_leftovers.KEPT_JSON).exists()

    report = collect(root, DONE)

    assert set(report.removed) == {"j"} and report.kept == {}
    assert list(jobs.iterdir()) == []
    assert archived(root) == archived(reference)
    record = json.loads((root / "jobs-kept" / "j" / job_leftovers.KEPT_JSON).read_text())
    assert record["archived"] == sorted(archived(reference))
    assert record["id"] == "j" and record["finished_at"] == OLD


@pytest.mark.parametrize("point", ("after the rename", "after the archive",
                                   "after the folder is deleted"))
def test_a_crash_after_the_commit_point_with_a_record_is_finished_by_the_next_pass(
    tmp_path: Path, monkeypatch, point: str
) -> None:
    root = work_root(tmp_path)
    folder = stale_worktree(tmp_path, root)
    make_work(folder, "unpushed-commit")
    commit = git_out(folder, "rev-parse", "HEAD")
    (folder / "tracked.txt").write_text("uncommitted on top\n")
    age_job(root, "j")
    jobs = root / "jobs"
    crash_while_finishing(root, point, monkeypatch)
    assert (jobs / "j.json").exists()

    report = collect(root, DONE)

    assert set(report.removed) == {"j"} and report.kept == {}
    assert list(jobs.iterdir()) == []
    assert registered(root) == [] and pinned(root) == commit
    fresh = apply_to_a_fresh_worktree(tmp_path, root, commit,
                                      root / "jobs-kept" / "j" / "worktree.patch")
    assert (fresh / "tracked.txt").read_text() == "uncommitted on top\n"


@pytest.mark.parametrize("since", ["requeued", "claimed after the host read"])
def test_a_claim_beside_a_gone_folder_leaves_it_and_the_next_pass_finishes_only_it(
    tmp_path: Path, monkeypatch, since: str
) -> None:
    root = work_root(tmp_path)
    jobs = root / "jobs"
    runner = checkout_folder(tmp_path, root, "j")
    (jobs / "j" / "handoff.md").write_text("old handoff\n")
    (jobs / "j.run" / "old").mkdir(parents=True)
    (jobs / "j.run" / "old" / "log").write_text("old log\n")
    for path in jobs.iterdir():
        age(path, wall_clock_days_ago(3))
    finished_at = wall_clock_days_ago(2)
    crash_while_finishing(root, "after the rename", monkeypatch, finished_at,
                          time.time() - 6 * HOUR)
    gone = sorted(path.relative_to(jobs) for path in (jobs / "j.gone").rglob("*"))

    assert "new" in runner.checkout()

    assert sorted(path.relative_to(jobs) for path in (jobs / "j.gone").rglob("*")) == gone
    assert (jobs / "j" / "README").read_text() == "checkout\n"
    (jobs / "j.run" / "new").mkdir()
    (jobs / "j.run" / "new" / "log").write_text("new log\n")
    beside = {str(path.relative_to(jobs)): None if path.is_dir() else path.read_bytes()
              for path in jobs.rglob("*") if not path.is_relative_to(jobs / "j.gone")}
    rows = ([("j", "t1", "running", None)] if since == "requeued"
            else [("j", "t1", "done", finished_at)])
    [(_, report)] = job_cleanup.run(database(tmp_path, rows), machines(tmp_path, "pc"),
                                    InProcessTransport(root), now=time.time(), grace_hours=6,
                                    dry_run=False)

    assert set(report.removed) == {"j"}
    assert report.kept == {"j": "live" if since == "requeued" else "claimed-since"}
    assert not (jobs / "j.gone").exists()
    assert {str(path.relative_to(jobs)): None if path.is_dir() else path.read_bytes()
            for path in jobs.rglob("*")} == beside
    kept = root / "jobs-kept" / "j"
    assert (kept / "j" / "handoff.md").read_text() == "old handoff\n"
    assert (kept / job_leftovers.KEPT_JSON).exists() and not (kept / "j.run").exists()


def test_a_dry_run_lists_a_gone_folder_and_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    root = work_root(tmp_path)
    finished_with_leftovers(root, "j")
    jobs = root / "jobs"
    crash_while_finishing(root, "after the rename", monkeypatch)
    before = {str(path.relative_to(root)): None if path.is_dir() else path.read_bytes()
              for path in root.rglob("*") if not path.is_symlink()}

    [(_, report)] = job_cleanup.run(database(tmp_path, DONE), machines(tmp_path, "pc"),
                                    InProcessTransport(root), now=NOW, grace_hours=6,
                                    dry_run=True)

    assert report.dry_run and report.kept == {}
    assert report.removed["j"] > 0 and set(report.removed) == {"j"}
    assert {str(path.relative_to(root)): None if path.is_dir() else path.read_bytes()
            for path in root.rglob("*") if not path.is_symlink()} == before
    assert (jobs / "j.gone").is_dir()
