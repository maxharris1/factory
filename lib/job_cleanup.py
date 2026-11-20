"""`orch cleanup` (issue #928): delete finished job checkouts on every device.

A placed job's checkout is `<workdir_root>/jobs/<job-id>/` on its device.
Its log, handoff and exit code live beside it in `jobs/<job-id>.run/`, with
`jobs/<job-id>.launched` and `jobs/<job-id>.brief.md`; telemetry and
transcripts are never under the work root.

The orch host decides and the device acts. `orch cleanup run` reads orch.db
once and names the finished jobs: done, failed or cancelled before the
cutoff (now minus the grace period), except a failed job whose ticket still
has a queued or running job (a retry may still need it). It sends that list
to every device target over ssh. `orch cleanup device` then moves each
named folder aside to `<job-id>.cleanup` unless `_live_reason`, a held lock
or a `jobs-kept/` on another device keeps it. The host reads orch.db again
and confirms only the ids still finished, so a job requeued and claimed
since the first read is moved back. For each confirmed folder the device
renames `<job-id>.cleanup` to `<job-id>.gone`, the commit point, then moves
its logs, results and worker records with the `.run/`, `.brief.md` and
`.launched` beside it to `jobs-kept/<job-id>/` (lib/job_leftovers.py),
deletes `.claimed` and the folder, and writes `kept.json` last. The
`.lock` stays. A `<job-id>.gone` already there puts the folder back with
an error. `_CLAIM` only ever restores `.cleanup`, never `.gone`, so the
next HOLD first finishes every `<job-id>.gone` and pending `kept.json.tmp`
under the root it was sent to. It archives the siblings and deletes
`.claimed` only while the host still names the job finished and nothing
claimed it since; otherwise it finishes only the `.gone` tree and leaves
the siblings to the requeued run. A device that cannot scan for
processes of this user with a cwd or open file in a folder removes
nothing. A process of this user whose cwd or open files the scan cannot
read (a non-dumpable one: ssh, an agent, a sandboxed shell) could be in any
job folder. So while one lives, every folder is kept as held-open.

The runner's checkout and start claim the job first under `flock` on
`jobs/<job-id>.lock`: they touch `jobs/<job-id>.claimed` and move a
set-aside folder back (lib/remote.py `_CLAIM`). Each move aside and each
archive here holds the same lock. A device whose login PATH has no `flock`
claims unlocked, so it removes nothing. A folder the host did not name is
never touched; the host labels each one live, recent, retry-held or
unknown.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from lib import health, job_leftovers, remote, worktrees
from lib.device_deploy import PICK_PYTHON, Target, targets
from lib.job_leftovers import GONE, WORKER_RECORD
from lib.remote import SSH_UNREACHABLE, CaptureTransport, SshTransport, _login, _marked, ssh_argv

DEFAULT_GRACE_HOURS = 6.0
TIMEOUT_SECONDS = 3600.0
MARK = "orch-cleanup:"
FINISHED = ("done", "failed", "cancelled")
ACTIVE = ("queued", "running")
LIVE = "live"
RECENT = "recent"
RETRY_HELD = "retry-held"
UNKNOWN = "unknown"
NOT_NAMED = "not-finished"
HELD_OPEN = "held-open"
CANNOT_SCAN = "cannot-scan"
CANNOT_LOCK = "cannot-lock"
LOCK_HELD = "lock-held"
CLAIMED_SINCE = "claimed-since"
WORKTREE_RECORD = "worktree-record"
WORKTREE_OWNER_LIVE = "worktree-owner-live"
WORKTREE_WORK = "worktree-work"
WORKER_LIVE = "worker-live"
WORKER_UNSURE = "worker-unsure"
HOLD = "hold"
FINISH = "finish"
SET_ASIDE = ".cleanup"
CLAIMED = ".claimed"
LAUNCHED = ".launched"
LOCK = ".lock"
PROC = Path("/proc")
SSH_SESSION = frozenset({"sshd", "sshd-session", "systemd", "(sd-pam)"})

_JOB_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_TAG = re.compile(r"[0-9a-f]{32}")
_RECORD_TAG = re.compile(rf"{re.escape(WORKER_RECORD)}-([0-9a-f]{{32}})\.")


@dataclass(frozen=True)
class JobRow:
    id: str
    repo: str
    ticket_id: str
    state: str
    finished_at: float | None
    failure_handled: bool = True


def keep_reason(job: JobRow, ticket_active: bool, cutoff: float) -> str | None:
    if job.state not in FINISHED:
        return LIVE
    if job.finished_at is None or job.finished_at >= cutoff:
        return RECENT
    if job.state == "failed" and (ticket_active or not job.failure_handled):
        return RETRY_HELD
    return None


def keep_reasons(jobs: Iterable[JobRow], cutoff: float) -> dict[str, str | None]:
    rows = list(jobs)
    active = {(row.repo.lower(), row.ticket_id) for row in rows if row.state in ACTIVE}
    return {
        row.id: keep_reason(row, (row.repo.lower(), row.ticket_id) in active, cutoff)
        for row in rows
    }


def read_jobs(db: Path) -> list[JobRow]:
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [
            JobRow(id, repo, ticket, state, finished_at, bool(handled))
            for id, repo, ticket, state, finished_at, handled in connection.execute(
                "SELECT id, repo, ticket_id, state, finished_at, failure_handled FROM jobs"
            )
        ]
    finally:
        connection.close()


@dataclass(frozen=True)
class Plan:
    finished: frozenset[str]
    cutoff: float
    dry_run: bool
    step: str = HOLD
    finished_at: Mapping[str, float] = field(default_factory=dict)

    def own_finish(self, job_id: str) -> float:
        return self.finished_at.get(job_id, self.cutoff)

    def to_json(self) -> str:
        return json.dumps(
            {"finished": sorted(self.finished), "cutoff": self.cutoff, "dry_run": self.dry_run,
             "step": self.step, "finished_at": dict(sorted(self.finished_at.items()))}
        )

    @classmethod
    def parse(cls, text: str) -> Plan:
        data = json.loads(text)
        finished = data["finished"]
        if not isinstance(finished, list) or not all(
            isinstance(item, str) and _JOB_ID.fullmatch(item) for item in finished
        ):
            raise ValueError("finished must be a list of plain job ids")
        step = data.get("step", HOLD)
        if step not in (HOLD, FINISH):
            raise ValueError(f"unknown step {step!r}")
        finished_at = data.get("finished_at", {})
        if not isinstance(finished_at, dict) or not all(
            isinstance(key, str) and _JOB_ID.fullmatch(key) for key in finished_at
        ):
            raise ValueError("finished_at must map plain job ids to times")
        return cls(frozenset(finished), float(data["cutoff"]), bool(data["dry_run"]), step,
                   {key: float(value) for key, value in finished_at.items()})


@dataclass(frozen=True)
class Report:
    removed: dict[str, int]
    kept: dict[str, str]
    dry_run: bool
    held: tuple[str, ...] = ()

    @property
    def bytes_freed(self) -> int:
        return sum(self.removed.values())

    def to_json(self) -> str:
        return json.dumps(
            {"removed": self.removed, "kept": self.kept, "dry_run": self.dry_run,
             "held": list(self.held), "bytes_freed": self.bytes_freed}
        )

    @classmethod
    def parse(cls, text: str) -> Report:
        data = json.loads(text)
        return cls(
            {str(k): int(v) for k, v in data["removed"].items()},
            {str(k): str(v) for k, v in data["kept"].items()},
            bool(data["dry_run"]),
            tuple(str(job_id) for job_id in data.get("held", ())),
        )


def disk_bytes(paths: Iterable[Path]) -> int:
    seen: set[tuple[int, int]] = set()
    total = 0

    def add(path: str) -> None:
        nonlocal total
        try:
            info = os.lstat(path)
        except OSError:
            return
        if (info.st_dev, info.st_ino) not in seen:
            seen.add((info.st_dev, info.st_ino))
            total += info.st_blocks * 512

    for top in paths:
        add(str(top))
        if top.is_symlink() or not top.is_dir():
            continue
        for root, directories, files in os.walk(top):
            for name in (*directories, *files):
                add(os.path.join(root, name))
    return total


def _status(process: Path) -> list[str] | None:
    try:
        return (process / "status").read_text().splitlines()
    except OSError:
        return None


def _mine(process: Path) -> bool | None:
    status = _status(process)
    if status is None or not any(line.startswith("VmSize:") for line in status):
        return None
    uids = next((line.split()[1:] for line in status if line.startswith("Uid:")), [])
    return str(os.getuid()) in uids


def _parent(process: Path) -> Path | None:
    try:
        stat = (process / "stat").read_text()
    except OSError:
        return None
    return process.with_name(stat.rsplit(")", 1)[1].split()[1])


def _hides_from_scan(process: Path) -> bool:
    """Root starts sshd's session process, `systemd --user` and `(sd-pam)` and
    then drops them to this user, which leaves them non-dumpable, so their
    cwd cannot be read."""
    status = _status(process) if _mine(process) else None
    if status is None:
        return False
    name = next((line.removeprefix("Name:").strip() for line in status
                 if line.startswith("Name:")), None)
    return name not in SSH_SESSION or not _started_by_another_user(process)


def _started_by_another_user(process: Path) -> bool:
    seen = {process.name}
    while (parent := _parent(process)) is not None and parent.name not in seen:
        seen.add(parent.name)
        if parent.name == "0":
            return True
        mine = _mine(parent)
        if mine is None:
            return False
        if not mine:
            return True
        if _process_paths(parent) is not None:
            return False
        process = parent
    return _mine(process) is None


def _process_paths(process: Path) -> list[str] | None:
    try:
        links = [process / "cwd", *(process / "fd").iterdir()]
    except (FileNotFoundError, ProcessLookupError):
        return []
    except OSError:
        return None
    paths = []
    for link in links:
        try:
            paths.append(os.readlink(link).removesuffix(" (deleted)"))
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            return None
    return paths


def jobs_held_open(jobs: Path) -> set[str] | None:
    prefix = f"{os.path.realpath(jobs)}/"
    if (PROC / "self" / "cwd").exists():
        paths_of = {process: _process_paths(process) for process in PROC.iterdir()
                    if process.name.isdigit()}
        paths = [path for listed in paths_of.values() if listed for path in listed]
        blind = any(listed is None and _hides_from_scan(process)
                    for process, listed in paths_of.items())
    else:
        try:
            found = subprocess.run(
                ["lsof", "-u", str(os.getuid()), "-Fn"],
                capture_output=True, text=True, timeout=120, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if found.returncode != 0:
            return None
        paths = [line[1:] for line in found.stdout.splitlines() if line.startswith("n")]
        blind = False
    held = {path[len(prefix):].split("/", 1)[0] for path in paths if path.startswith(prefix)}
    if blind and jobs.is_dir():
        held |= {entry.name for entry in jobs.iterdir()}
    return held


def _changed_after(path: Path, when: float) -> bool:
    try:
        return path.lstat().st_mtime > when
    except FileNotFoundError:
        return False


def runner_locks() -> bool:
    return shutil.which("flock") is not None


@contextmanager
def _claim_lock(jobs: Path, job_id: str, *, wait: bool = True,
                create: bool = True) -> Iterator[bool]:
    try:
        opened = open(jobs / f"{job_id}{LOCK}", "a" if create else "r")
    except FileNotFoundError:
        if create:
            raise
        yield True
        return
    with opened as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True


def _worker_tags(folder: Path, job_id: str) -> list[str]:
    tags = {match.group(1) for path in folder.iterdir()
            if (match := _RECORD_TAG.match(path.name))}
    run = folder.parent / f"{job_id}.run"
    if run.is_dir():
        tags |= {path.name for path in run.iterdir()
                 if _TAG.fullmatch(path.name) and path.is_dir() and not path.is_symlink()}
    return sorted(tags)


def _worker_reason(folder: Path, job_id: str) -> str | None:
    for tag in _worker_tags(folder, job_id):
        try:
            state = remote.worker_state(subprocess.run(
                ["sh", "-c", remote.worker_check_script(str(folder), tag)],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=120, check=False,
            ).returncode)
        except (OSError, subprocess.TimeoutExpired):
            return WORKER_UNSURE
        if state != remote.GONE:
            return WORKER_LIVE if state == remote.RUNNING else WORKER_UNSURE
    return None


def _worktree_reason(folder: Path, job_id: str) -> str | None:
    record = folder.parent / f"{job_id}.json"
    if not record.exists() and not record.is_symlink():
        return None
    live = worktrees.record_owner_live(record)
    if live is None:
        return WORKTREE_RECORD
    if live:
        return WORKTREE_OWNER_LIVE
    if not (folder / ".git").is_file():
        return WORKTREE_WORK
    held = worktrees.keep_reason(folder, worktrees.recorded_start(record))
    return None if held is None else WORKTREE_WORK


def _claimed_since(jobs: Path, job_id: str, own_finish: float) -> bool:
    return any(_changed_after(jobs / f"{job_id}{mark}", own_finish) for mark in (CLAIMED, LAUNCHED))


def _live_reason(folder: Path, job_id: str, own_finish: float,
                 scan: Callable[[Path], set[str] | None]) -> str | None:
    if _claimed_since(folder.parent, job_id, own_finish):
        return CLAIMED_SINCE
    return (_worktree_reason(folder, job_id) or _worker_reason(folder, job_id)
            or _in_use(folder, job_id, scan))


def _in_use(folder: Path, job_id: str, scan: Callable[[Path], set[str] | None]) -> str | None:
    held = scan(folder.parent)
    if held is None:
        return CANNOT_SCAN
    if folder.name in held or f"{job_id}.run" in held:
        return HELD_OPEN
    return None


def _deleted_bytes(folder: Path) -> int:
    return disk_bytes([folder]) - disk_bytes(job_leftovers.kept_entries(folder))


def _job_folders(jobs: Path, suffix: str = "") -> dict[str, Path]:
    found = {}
    for path in jobs.iterdir() if jobs.is_dir() else ():
        job_id = path.name.removesuffix(suffix) if suffix else path.name
        if (path.name.endswith(suffix) and _JOB_ID.fullmatch(job_id) and path.is_dir()
                and not path.is_symlink()):
            found[job_id] = path
    return dict(sorted(found.items()))


def _put_back(aside: Path, job_id: str) -> str | None:
    home = aside.with_name(job_id)
    if home.exists() or home.is_symlink():
        return f"error: {job_id} was made again, left {aside.name}"
    aside.rename(home)
    return None


def _settle(root: Path, plan: Plan, locks: bool) -> tuple[dict[str, int], dict[str, str]]:
    jobs = root / "jobs"
    removed: dict[str, int] = {}
    kept: dict[str, str] = {}
    pending = filter(_JOB_ID.fullmatch, job_leftovers.pending(root))
    for job_id in sorted(set(_job_folders(jobs, GONE)).union(pending)):
        gone = jobs / f"{job_id}{GONE}"
        try:
            with _claim_lock(jobs, job_id, wait=False, create=not plan.dry_run) as locked:
                if not locked:
                    kept[job_id] = LOCK_HELD
                elif gone.is_dir() and not gone.is_symlink():
                    size = _deleted_bytes(gone)
                    if not plan.dry_run:
                        siblings = (locks and job_id in plan.finished and not _claimed_since(
                            jobs, job_id, plan.own_finish(job_id)))
                        job_leftovers.retire(root, job_id, gone, siblings=siblings,
                                             finished_at=plan.own_finish(job_id),
                                             removed_bytes=size)
                    removed[job_id] = size
                else:
                    removed[job_id] = 0 if plan.dry_run else job_leftovers.commit(root, job_id)
        except OSError as exc:
            kept[job_id] = f"error: {exc}".splitlines()[0]
    return removed, kept


def _hold(root: Path, plan: Plan, scan: Callable[[Path], set[str] | None]) -> Report:
    jobs = root / "jobs"
    locks = runner_locks()
    removed, kept = _settle(root, plan, locks)
    held = list(_job_folders(jobs, SET_ASIDE))
    folders = _job_folders(jobs)
    away = job_leftovers.cross_device(root) if folders else None
    for job_id, folder in folders.items():
        if job_id not in plan.finished:
            kept[job_id] = NOT_NAMED
            continue
        if not locks:
            kept[job_id] = CANNOT_LOCK
            continue
        if away is not None:
            kept[job_id] = away
            continue
        try:
            with _claim_lock(jobs, job_id, wait=False, create=not plan.dry_run) as locked:
                reason = (_live_reason(folder, job_id, plan.own_finish(job_id), scan)
                          if locked else LOCK_HELD)
                if reason is not None:
                    kept[job_id] = reason
                elif plan.dry_run:
                    removed[job_id] = removed.get(job_id, 0) + _deleted_bytes(folder)
                elif job_id in held:
                    kept[job_id] = f"error: {job_id}{SET_ASIDE} is still there"
                else:
                    folder.rename(folder.with_name(job_id + SET_ASIDE))
                    held.append(job_id)
        except OSError as exc:
            kept[job_id] = f"error: could not set aside {folder}: {exc}".splitlines()[0]
    return Report(removed, kept, plan.dry_run, tuple(sorted(held)))


def _finish_reason(aside: Path, job_id: str, plan: Plan, locks: bool,
                   scan: Callable[[Path], set[str] | None]) -> str | None:
    if job_id not in plan.finished:
        return NOT_NAMED
    if not locks:
        return CANNOT_LOCK
    return _live_reason(aside, job_id, plan.own_finish(job_id), scan)


def _finish(root: Path, plan: Plan, scan: Callable[[Path], set[str] | None]) -> Report:
    removed: dict[str, int] = {}
    kept: dict[str, str] = {}
    jobs = root / "jobs"
    locks = runner_locks()
    for job_id, aside in _job_folders(jobs, SET_ASIDE).items():
        gone = jobs / f"{job_id}{GONE}"
        try:
            with _claim_lock(jobs, job_id):
                if not aside.is_dir():
                    kept[job_id] = RECENT
                    continue
                reason = (_finish_reason(aside, job_id, plan, locks, scan)
                          or job_leftovers.cross_device(root)
                          or (f"error: {gone.name} is still there"
                              if os.path.lexists(gone) else None))
                if reason is not None or os.path.lexists(jobs / job_id):
                    kept[job_id] = _put_back(aside, job_id) or reason
                    continue
                size = _deleted_bytes(aside)
                aside.rename(gone)
                job_leftovers.retire(root, job_id, gone, siblings=True,
                                     finished_at=plan.own_finish(job_id), removed_bytes=size)
                removed[job_id] = size
        except OSError as exc:
            kept[job_id] = f"error: {exc}".splitlines()[0]
    return Report(removed, kept, False)


def device_step(root: Path, plan: Plan, *, scan: Callable[[Path], set[str] | None] = jobs_held_open) -> Report:
    return (_finish if plan.step == FINISH else _hold)(root, plan, scan)


def label_unnamed(report: Report, reasons: Mapping[str, str | None]) -> Report:
    kept = {
        job_id: (reasons.get(job_id) or UNKNOWN) if reason == NOT_NAMED else reason
        for job_id, reason in report.kept.items()
    }
    return Report(report.removed, kept, report.dry_run)


def _gib(size: int) -> str:
    return f"{size / 2**30:.2f} GiB"


def summary(target: Target, outcome: Report | str) -> str:
    label = f"{target.label} {target.remote.workdir_root}"
    if isinstance(outcome, str):
        return f"{label}: failed: {outcome}"
    report = outcome
    verb = "would remove" if report.dry_run else "removed"
    counts = Counter(reason.split(":", 1)[0] for reason in report.kept.values())
    kept = ", ".join(f"{reason} {count}" for reason, count in sorted(counts.items()))
    return (
        f"{label}: {verb} {len(report.removed)} checkouts ({_gib(report.bytes_freed)}), "
        f"kept {len(report.kept)}" + (f" ({kept})" if kept else "")
    )


def device_script(checkout: str, workdir_root: str) -> str:
    body = PICK_PYTHON + 'cd -- "$1" || exit 3; exec "$py" ./bin/orch cleanup device --root "$2"'
    return _login(body, "orch-cleanup", (checkout, workdir_root))


def _device_call(transport: CaptureTransport, target: Target, plan: Plan) -> Report | str:
    try:
        result = transport.call(
            ssh_argv(target.remote, device_script(target.checkout, target.remote.workdir_root)),
            timeout=TIMEOUT_SECONDS,
            input=plan.to_json().encode(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"ssh did not finish ({type(exc).__name__})"
    if result.code == SSH_UNREACHABLE:
        return "ssh failed"
    said = _marked(result.stdout, MARK)
    if result.code != 0 or said is None:
        detail = (result.stderr.strip().splitlines() or [""])[-1]
        return f"device step exited {result.code}" + (f": {detail}" if detail else "")
    try:
        return Report.parse(said)
    except (ValueError, KeyError, TypeError, AttributeError):
        return "device step printed an unreadable report"


def clean_target(transport: CaptureTransport, target: Target, db: Path, plan: Plan,
                 reasons: Mapping[str, str | None]) -> Report | str:
    hold = _device_call(transport, target, plan)
    if isinstance(hold, str):
        return hold
    held = label_unnamed(hold, reasons)
    if plan.dry_run or not hold.held:
        return held
    reread = keep_reasons(read_jobs(db), plan.cutoff)
    confirmed = frozenset(i for i in hold.held if i in reread and reread[i] is None)
    finish = _device_call(transport, target, Plan(
        confirmed, plan.cutoff, False, FINISH,
        {i: at for i, at in plan.finished_at.items() if i in confirmed}))
    if isinstance(finish, str):
        return f"set aside {len(hold.held)} checkouts, then {finish}"
    taken_back = {i: reread.get(i) or RECENT for i in hold.held if i not in finish.removed}
    removed = {i: hold.removed.get(i, 0) + finish.removed.get(i, 0)
               for i in hold.removed | finish.removed}
    return Report(removed,
                  {**held.kept, **taken_back, **label_unnamed(finish, reread).kept}, False)


def run(db: Path, machines, transport: CaptureTransport, *, now: float, grace_hours: float,
        dry_run: bool) -> list[tuple[Target, Report | str]]:
    cutoff = now - grace_hours * 3600
    rows = read_jobs(db)
    reasons = keep_reasons(rows, cutoff)
    finished_at = {row.id: row.finished_at for row in rows
                   if reasons[row.id] is None and row.finished_at is not None}
    plan = Plan(frozenset(finished_at), cutoff, dry_run, HOLD, finished_at)
    found = {(t.device, t.remote.workdir_root): t for t in targets(machines)}
    work = list(found.values())
    if not work:
        return []
    with ThreadPoolExecutor(max_workers=len(work)) as pool:
        return list(zip(work, pool.map(
            lambda t: clean_target(transport, t, db, plan, reasons), work)))


USAGE = """\
orch cleanup run --backlog [--dry-run] [--grace-hours H] [--db PATH] [--machines PATH]
orch cleanup device --root DIR      (reads the host's plan as JSON on stdin)
"""


def main(argv: list[str], *, clock: Callable[[], float] = time.time,
         transport: CaptureTransport | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orch cleanup", usage=USAGE)
    commands = parser.add_subparsers(dest="command", required=True)
    host = commands.add_parser("run")
    host.add_argument("--backlog", action="store_true")
    host.add_argument("--dry-run", action="store_true")
    host.add_argument("--grace-hours", type=float, default=DEFAULT_GRACE_HOURS)
    host.add_argument("--db", type=Path)
    host.add_argument("--machines", type=Path)
    device = commands.add_parser("device")
    device.add_argument("--root", type=Path, required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if args.command == "device":
        try:
            plan = Plan.parse(sys.stdin.read())
        except (ValueError, KeyError, TypeError) as exc:
            print(f"orch cleanup device: bad plan: {exc}", file=sys.stderr)
            return 2
        print(MARK, device_step(args.root, plan).to_json(), flush=True)
        return 0
    if not args.backlog:
        print("orch cleanup run: only the hand-run --backlog pass exists so far", file=sys.stderr)
        return 2
    if args.grace_hours < 0:
        print("orch cleanup run: --grace-hours must not be negative", file=sys.stderr)
        return 2
    db = args.db or Path(os.environ.get("ORCH_DB")
                         or Path.home() / ".local/state/factory/orch.db")
    if not db.is_file():
        print(f"orch cleanup run: no orch.db at {db}", file=sys.stderr)
        return 1
    outcomes = run(db, health.load_machines(args.machines),
                   transport if transport is not None else SshTransport(),
                   now=clock(), grace_hours=args.grace_hours, dry_run=args.dry_run)
    for target, outcome in outcomes:
        print(summary(target, outcome), flush=True)
    return 1 if any(isinstance(outcome, str) for _, outcome in outcomes) else 0
