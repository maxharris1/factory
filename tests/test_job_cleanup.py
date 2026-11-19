"""`orch cleanup` (issue #928): finished job checkouts go, logs and live jobs stay.

The end-to-end tests send the real ssh script through a transport that runs
it locally under `sh -c`, as sshd would on the device, so `orch cleanup
device` runs from this checkout's bin/orch against a fixture work root.
"""

from __future__ import annotations

import fcntl
import io
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import MappingProxyType

import pytest

from lib import job_cleanup, remote
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


def device_path() -> str:
    return (f"{Path(sys.executable).parent}:{Path(shutil.which('flock')).parent}"
            ":/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")


class ShellTransport:
    def __init__(self, home: Path):
        self.home = home
        self.offline: set[str] = set()

    def call(self, argv, *, timeout: float, input: bytes | None = None) -> Called:
        alias, script = argv[-2], argv[-1]
        if alias in self.offline:
            return Called(SSH_UNREACHABLE, "", "")
        done = subprocess.run(
            ["sh", "-c", script], input=input or b"", capture_output=True, timeout=timeout,
            env={"HOME": str(self.home), "PATH": device_path()},
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
    before = kept_files(root, "done1")
    db = database(tmp_path, [("done1", "t1", "done", OLD)])
    outcomes = job_cleanup.run(db, machines(tmp_path, "pc"), ShellTransport(tmp_path),
                               now=NOW, grace_hours=6, dry_run=False)

    [(_, report)] = outcomes
    assert not isinstance(report, str), report
    assert set(report.removed) == {"done1"} and report.kept == {}
    assert sorted(path.name for path in folder.iterdir()) == [
        ".orch-worker-abc.tree.pid", ".orch-worker.pid"]
    assert kept_files(root, "done1") == before
    assert len(before) == 5
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
    for job_id in ("launched", "worker", "held", "free"):
        job_folder(root, job_id)
    jobs = root / "jobs"
    os.utime(jobs / "launched.launched", (NOW, NOW))
    os.utime(jobs / "worker" / ".orch-worker.pid", (NOW, NOW))
    plan = Plan(frozenset({"launched", "worker", "held", "free"}), NOW - 6 * HOUR, False)
    sleeper = subprocess.Popen(["sleep", "60"], cwd=jobs / "held" / "repo")
    try:
        report = device_pass(root, plan)
        assert (jobs / "held" / "repo" / "README").exists()
    finally:
        sleeper.kill()
        sleeper.wait()

    assert report.kept == {"launched": "recent", "worker": "recent", "held": "held-open"}
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
    (jobs / "wt1.json").write_text("{}")
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
    freed = {job_id: sizes[job_id] - du(jobs / job_id) for job_id in finished}
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
    job_folder(root, "j")
    jobs = root / "jobs"
    plan = Plan(frozenset({"j"}), NOW - 6 * HOUR, False)
    hold = job_cleanup.device_step(root, plan)
    assert hold.held == ("j",) and not (jobs / "j").exists()
    (jobs / "j").mkdir()
    (jobs / "j" / "new-run").write_text("work of the next run\n")

    done = job_cleanup.device_step(
        root, Plan(frozenset({"j"}), plan.cutoff, False, job_cleanup.FINISH))

    assert (jobs / "j" / "new-run").read_text() == "work of the next run\n"
    assert done.removed == {} and done.kept["j"].startswith("error: j was made again")
    assert sorted(path.name for path in (jobs / "j.cleanup").iterdir()) == [
        ".orch-worker-abc.tree.pid", ".orch-worker.pid"]


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


@pytest.mark.skipif(not Path("/proc/self/cwd").exists(), reason="needs Linux /proc")
def test_a_live_process_of_this_user_that_hides_its_cwd_keeps_every_folder(tmp_path: Path) -> None:
    root = work_root(tmp_path)
    job_folder(root, "held")
    jobs = root / "jobs"
    child = subprocess.Popen([sys.executable, "-c", NON_DUMPABLE], cwd=jobs / "held" / "repo",
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        try:
            os.readlink(f"/proc/{child.pid}/cwd")
            pytest.skip("this user can read a non-dumpable process's cwd (root or CAP_SYS_PTRACE)")
        except PermissionError:
            pass
        assert job_cleanup.jobs_held_open(jobs) is None
        report = device_pass(root, Plan(frozenset({"held"}), NOW - 6 * HOUR, False))
        assert child.poll() is None
    finally:
        child.kill()
        child.wait()

    assert report.removed == {} and report.kept == {"held": "cannot-scan"}
    assert (jobs / "held" / "repo" / "README").exists()
    assert job_cleanup.jobs_held_open(jobs) == set()


def test_a_process_that_exits_while_the_scan_walks_its_parents_does_not_fail_the_scan(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    jobs = work_root(tmp_path) / "jobs"
    jobs.mkdir(parents=True)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    gone = f"/proc/{child.pid}"
    read_paths, read_parent = job_cleanup._process_paths, job_cleanup._parent

    def paths(process: Path) -> list[str] | None:
        return None if str(process) == gone else read_paths(process)

    def parent(process: Path) -> Path | None:
        if str(process) == gone and child.poll() is None:
            child.kill()
            child.wait()
        return read_parent(process)

    monkeypatch.setattr(job_cleanup, "_process_paths", paths)
    monkeypatch.setattr(job_cleanup, "_parent", parent)
    try:
        assert job_cleanup.jobs_held_open(jobs) == set()
    finally:
        child.kill()
        child.wait()


GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}
TAG = "0123456789abcdef" * 2


def days_ago(days: float = 3) -> float:
    """Real script runs stamp their files with the wall clock, not NOW."""
    return time.time() - days * 86400
WORKER = (
    'printf "%s %s\\n" "$(test -d .git && echo True || echo False)" '
    '"$(test -f README && echo True || echo False)" > "$1"'
)


class Runner:
    """The real checkout and start scripts, run under `sh -c` as sshd would."""

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
        age(path, days_ago())
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
    db = database(tmp_path, [("j", "t1", "cancelled", days_ago())])
    transport = ClaimAndStart(root, db, runner, when=when)

    [(_, report)] = job_cleanup.run(db, machines(tmp_path, "pc"), transport,
                                    now=time.time(), grace_hours=6, dry_run=False)

    assert transport.saw == "True True"
    assert (jobs / "j" / "README").read_text() == "checkout\n"
    assert not (jobs / "j.cleanup").exists()
    assert job_cleanup.read_jobs(db)[0].state == "running"
    assert report.removed == {} and report.kept["j"] in ("live", "recent")


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
        assert (jobs / "j.claimed").stat().st_mtime < days_ago(1) and not (jobs / "j").exists()
    out, _ = checkout.communicate(timeout=60)

    assert checkout.returncode == 0 and "reused" in out
    assert (jobs / "j" / "README").exists() and not (jobs / "j.cleanup").exists()
    assert (jobs / "j.claimed").stat().st_mtime > days_ago(1 / 24)


def test_the_hold_waits_for_a_claim_in_progress_and_keeps_the_folder(tmp_path: Path) -> None:
    root = work_root(tmp_path)
    folder = job_folder(root, "j", when=days_ago())
    jobs = root / "jobs"
    holder = hold_lock(jobs / "j.lock", 1.0, then_touch=jobs / "j.claimed")
    began = time.monotonic()

    hold = job_cleanup.device_step(root, Plan(frozenset({"j"}), days_ago(0.25), False))

    assert time.monotonic() - began >= 0.8
    holder.wait(timeout=10)
    assert hold.kept == {"j": "recent"} and hold.held == ()
    assert (folder / "repo" / "README").exists() and not (jobs / "j.cleanup").exists()


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
