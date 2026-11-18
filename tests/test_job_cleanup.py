"""`orch cleanup` (issue #928): finished job checkouts go, logs and live jobs stay.

The end-to-end tests send the real ssh script through a transport that runs
it locally under `sh -c`, as sshd would on the device, so `orch cleanup
device` runs from this checkout's bin/orch against a fixture work root.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType

import pytest

from lib import job_cleanup
from lib.job_cleanup import Plan
from lib.machines_yml import Device, MachinesConfig
from lib.queue import SQLiteJobQueue
from lib.remote import SSH_UNREACHABLE, Called

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000.0
OLD = NOW - 3 * 86400
HOUR = 3600.0


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
            env={"HOME": str(self.home),
                 "PATH": f"{Path(sys.executable).parent}:/usr/local/bin:/usr/bin:/bin"},
        )
        return Called(done.returncode, done.stdout.decode(), done.stderr.decode())


class InProcessTransport:
    def __init__(self, root: Path, scan=job_cleanup.jobs_held_open):
        self.root = root
        self.scan = scan

    def call(self, argv, *, timeout: float, input: bytes | None = None) -> Called:
        report = job_cleanup.device_step(self.root, Plan.parse(input.decode()), scan=self.scan)
        return Called(0, f"{job_cleanup.MARK} {report.to_json()}\n", "")


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
        report = job_cleanup.device_step(root, plan)
        assert (jobs / "held" / "repo" / "README").exists()
    finally:
        sleeper.kill()
        sleeper.wait()

    assert report.kept == {"launched": "recent", "worker": "recent", "held": "held-open"}
    assert set(report.removed) == {"free"}
    job_folder(root, "free2")
    blind = job_cleanup.device_step(root, Plan(frozenset({"free2"}), NOW - 6 * HOUR, False),
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
        report = job_cleanup.device_step(
            linked, Plan(frozenset({"held"}), NOW - 6 * HOUR, False))
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
