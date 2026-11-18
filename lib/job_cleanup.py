"""`orch cleanup` (issue #928): delete finished job checkouts on every device.

A placed job's checkout is `<workdir_root>/jobs/<job-id>/` on its device.
Its log, handoff and exit code live beside it in `jobs/<job-id>.run/`, with
`jobs/<job-id>.launched` and `jobs/<job-id>.brief.md`; telemetry and
transcripts are never under the work root. So removing the checkout keeps
everything the weekly eval and the dashboards read.

The orch host decides and the device acts. `orch cleanup run` reads orch.db
once and names the finished jobs: done, failed or cancelled before the
cutoff (now minus the grace period), except a failed job whose ticket still
has a queued or running job (a retry may still need it). It sends that list
to every device target over ssh. `orch cleanup device` then empties each
named folder there, keeping only its `.orch-worker*` records, unless the
folder or its launch or worker records changed after the cutoff (a requeue
racing the pass), a worktree record sits beside it, or a process of this
user has its cwd or an open file inside it. A device that cannot scan for
such processes removes nothing. A folder the host did not name is never
touched; the host labels each one live, recent, retry-held or unknown.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from lib import health, scratch
from lib.device_deploy import PICK_PYTHON, Target, targets
from lib.remote import SSH_UNREACHABLE, CaptureTransport, SshTransport, _login, _marked, ssh_argv

DEFAULT_GRACE_HOURS = 6.0
TIMEOUT_SECONDS = 3600.0
MARK = "orch-cleanup:"
WORKER_RECORD = ".orch-worker"
FINISHED = ("done", "failed", "cancelled")
ACTIVE = ("queued", "running")
LIVE = "live"
RECENT = "recent"
RETRY_HELD = "retry-held"
UNKNOWN = "unknown"
NOT_NAMED = "not-finished"
HELD_OPEN = "held-open"
CANNOT_SCAN = "cannot-scan"
WORKTREE_RECORD = "worktree-record"

_JOB_ID = re.compile(r"^[A-Za-z0-9_-]+$")


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

    def to_json(self) -> str:
        return json.dumps(
            {"finished": sorted(self.finished), "cutoff": self.cutoff, "dry_run": self.dry_run}
        )

    @classmethod
    def parse(cls, text: str) -> Plan:
        data = json.loads(text)
        finished = data["finished"]
        if not isinstance(finished, list) or not all(
            isinstance(item, str) and _JOB_ID.fullmatch(item) for item in finished
        ):
            raise ValueError("finished must be a list of plain job ids")
        return cls(frozenset(finished), float(data["cutoff"]), bool(data["dry_run"]))


@dataclass(frozen=True)
class Report:
    removed: dict[str, int]
    kept: dict[str, str]
    dry_run: bool

    @property
    def bytes_freed(self) -> int:
        return sum(self.removed.values())

    def to_json(self) -> str:
        return json.dumps(
            {"removed": self.removed, "kept": self.kept, "dry_run": self.dry_run,
             "bytes_freed": self.bytes_freed}
        )

    @classmethod
    def parse(cls, text: str) -> Report:
        data = json.loads(text)
        return cls(
            {str(k): int(v) for k, v in data["removed"].items()},
            {str(k): str(v) for k, v in data["kept"].items()},
            bool(data["dry_run"]),
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


def jobs_held_open(jobs: Path) -> set[str] | None:
    prefix = f"{os.path.realpath(jobs)}/"
    proc = Path("/proc")
    if (proc / "self" / "cwd").exists():
        paths = []
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            links = [entry / "cwd"]
            try:
                links.extend((entry / "fd").iterdir())
            except OSError:
                pass
            for link in links:
                try:
                    paths.append(os.readlink(link).removesuffix(" (deleted)"))
                except OSError:
                    continue
    else:
        try:
            found = subprocess.run(
                ["lsof", "-u", str(os.getuid()), "-Fn"],
                capture_output=True, text=True, timeout=120, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if found.returncode not in (0, 1):
            return None
        paths = [line[1:] for line in found.stdout.splitlines() if line.startswith("n")]
    return {path[len(prefix):].split("/", 1)[0] for path in paths if path.startswith(prefix)}


def _newer(path: Path, cutoff: float) -> bool:
    try:
        return path.lstat().st_mtime >= cutoff
    except FileNotFoundError:
        return False


def _device_reason(folder: Path, cutoff: float, scan: Callable[[Path], set[str] | None]) -> str | None:
    jobs = folder.parent
    if (jobs / f"{folder.name}.json").exists():
        return WORKTREE_RECORD
    records = [path for path in folder.iterdir() if path.name.startswith(WORKER_RECORD)]
    if any(_newer(path, cutoff) for path in (folder, jobs / f"{folder.name}.launched", *records)):
        return RECENT
    held = scan(jobs)
    if held is None:
        return CANNOT_SCAN
    if folder.name in held:
        return HELD_OPEN
    return None


def _removable(folder: Path) -> list[Path]:
    return [entry for entry in folder.iterdir() if not entry.name.startswith(WORKER_RECORD)]


def _empty(entries: Iterable[Path]) -> str | None:
    for entry in entries:
        if entry.is_dir() and not entry.is_symlink():
            failed = scratch.remove(entry)
            if failed is not None:
                return failed
            continue
        try:
            entry.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            return f"could not remove {entry}: {exc}"
    return None


def device_step(root: Path, plan: Plan, *, scan: Callable[[Path], set[str] | None] = jobs_held_open) -> Report:
    jobs = root / "jobs"
    folders = sorted(
        path for path in (jobs.iterdir() if jobs.is_dir() else ())
        if path.is_dir() and not path.is_symlink() and _JOB_ID.fullmatch(path.name)
    )
    removed: dict[str, int] = {}
    kept: dict[str, str] = {}
    for folder in folders:
        if folder.name not in plan.finished:
            kept[folder.name] = NOT_NAMED
            continue
        try:
            reason = _device_reason(folder, plan.cutoff, scan)
            if reason is not None:
                kept[folder.name] = reason
                continue
            entries = _removable(folder)
            size = disk_bytes(entries)
            failed = None if plan.dry_run else _empty(entries)
        except OSError as exc:
            failed = f"could not read {folder}: {exc}"
        if failed is None:
            removed[folder.name] = size
        else:
            kept[folder.name] = f"error: {failed.splitlines()[0]}"
    return Report(removed, kept, plan.dry_run)


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


def clean_target(transport: CaptureTransport, target: Target, plan: Plan) -> Report | str:
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


def run(db: Path, machines, transport: CaptureTransport, *, now: float, grace_hours: float,
        dry_run: bool) -> list[tuple[Target, Report | str]]:
    cutoff = now - grace_hours * 3600
    reasons = keep_reasons(read_jobs(db), cutoff)
    plan = Plan(frozenset(i for i, r in reasons.items() if r is None), cutoff, dry_run)
    found = {(t.device, t.remote.workdir_root): t for t in targets(machines)}
    work = list(found.values())
    if not work:
        return []
    with ThreadPoolExecutor(max_workers=len(work)) as pool:
        outcomes = pool.map(lambda t: clean_target(transport, t, plan), work)
        return [
            (target, outcome if isinstance(outcome, str) else label_unnamed(outcome, reasons))
            for target, outcome in zip(work, outcomes)
        ]


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
