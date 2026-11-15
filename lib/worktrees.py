"""Job checkouts as git worktrees of one shared clone per repo.

A review or fix job used to leave a full clone behind in its own directory.
Here every repo has one shared bare clone under `<root>/clones/<owner>/<name>.git`,
and each job gets a detached worktree at `<root>/jobs/<job-id>`, with a
`<job-id>.json` record beside it (repo, clone, creation time, owning pid).

When the job ends, `finish` removes the worktree and prunes the clone unless
the worktree has uncommitted changes or commits on no remote-tracking ref;
it then keeps the worktree and logs why. `gc` applies the same rule to
records older than N hours whose owning process is gone. Both decide under
the clone lock, right before removing, so a job re-run that takes the
worktree over in between keeps it. gc removes only a worktree of the clone
derived from the record's repo, never one the record merely names.

Nothing here is project-specific: the remote URL comes from a `{repo}`
template (default GitHub HTTPS), and git authenticates the usual way. A
template carrying credentials is refused, so no secret reaches git's argv or
the clone's config.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import time
import urllib.parse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import NamedTuple

WORK_ROOT_ENV = "ORCH_WORK_ROOT"
URL_TEMPLATE_ENV = "ORCH_GIT_URL_TEMPLATE"
DEFAULT_URL_TEMPLATE = "https://github.com/{repo}.git"
DEFAULT_GC_HOURS = 24.0
NETWORK_TIMEOUT_SECONDS = 900.0
LOCAL_TIMEOUT_SECONDS = 120.0

_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_JOB_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_SHA = re.compile(r"^[0-9a-f]{7,64}$")

Log = Callable[[str], None]


class WorktreeError(Exception):
    """A job worktree could not be created. The message is one line."""


@dataclass(frozen=True)
class JobCheckout:
    job_id: str
    repo: str
    path: Path
    clone: Path
    record: Path
    start: str | None = None


def work_root(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    configured = os.environ.get(WORK_ROOT_ENV)
    if configured:
        return Path(configured)
    return Path.home() / "factory-work"


def url_template(explicit: str | None) -> str:
    return explicit or os.environ.get(URL_TEMPLATE_ENV) or DEFAULT_URL_TEMPLATE


_SSH_SCHEMES = ("ssh", "git+ssh", "ssh+git")


def check_remote_url(url: str) -> None:
    """Refuse a remote URL that carries credentials. Never echoes the URL.

    A password is never allowed. A user name is allowed only for SSH
    (`ssh://git@host/...` or `git@host:...`), where it is not a secret; over
    HTTPS the user slot is where tokens go. Use a git credential helper.
    """
    if "://" in url:
        scheme = url.split("://", 1)[0].lower()
        authority = urllib.parse.urlsplit(url).netloc
        user_allowed = scheme in _SSH_SCHEMES
    else:
        authority = url.split("/", 1)[0]  # scp-like [user@]host:path, or a path
        user_allowed = True
    userinfo, at, _host = authority.rpartition("@")
    if at and (":" in userinfo or not user_allowed):
        raise WorktreeError(
            "the git URL template carries credentials before '@'; "
            "remove them and let a git credential helper authenticate"
        )


def prepare(
    root: Path,
    job_id: str,
    repo: str,
    head: str | None,
    *,
    url_template: str = DEFAULT_URL_TEMPLATE,
    pid: int | None = None,
    adopt: str | None = None,
    log: Log | None = None,
) -> JobCheckout:
    """Create (or reuse) the job's worktree at head, else the default branch.

    `adopt` names the failed job a T6 retry repeats (#105). When that job's
    worktree was kept for its work (uncommitted changes or unpushed commits)
    and no live run owns it, it is moved to this job instead of starting
    from head, so the retry picks up where the first attempt stopped.
    """
    if not _JOB_ID.match(job_id):
        raise WorktreeError(f"unsafe job id {job_id!r}")
    if adopt is not None and (not _JOB_ID.match(adopt) or adopt == job_id):
        adopt = None
    if head is not None and not _SHA.match(head):
        raise WorktreeError(f"head {head!r} is not a commit SHA")
    root = root.absolute()
    clone = _clone_path(root, repo)
    jobs = root / "jobs"
    checkout = JobCheckout(
        job_id=job_id,
        repo=repo,
        path=jobs / job_id,
        clone=clone,
        record=jobs / f"{job_id}.json",
    )
    url = url_template.format(repo=repo)
    check_remote_url(url)
    jobs.mkdir(parents=True, exist_ok=True)
    with _clone_lock(clone):
        _ensure_clone(clone, url)
        if checkout.path.exists():
            if not checkout.record.is_file() or not (checkout.path / ".git").is_file():
                raise WorktreeError(f"{checkout.path} exists and is not this job's worktree")
            reused = replace(checkout, start=_recorded_start(checkout.record))
            _write_record(reused, pid)
            return reused
        if adopt is not None:
            adopted = _adopt_kept(root, adopt, checkout, pid, log)
            if adopted is not None:
                return adopted
        target = _target(clone, head)
        start = _git(clone, "rev-parse", "--verify", f"{target}^{{commit}}").strip()
        checkout = replace(checkout, start=start)
        _write_record(checkout, pid)
        try:
            _git(clone, "worktree", "add", "--detach", "--quiet", str(checkout.path), start)
        except WorktreeError:
            checkout.record.unlink(missing_ok=True)
            raise
    return checkout


def fetched_clone(root: Path, repo: str, heads: tuple[str, ...], template: str) -> Path:
    """The repo's bare clone, fetched, holding every commit in heads (#130).

    The poller's read of the clone the waiter keeps for job worktrees, under
    the same lock. It never makes a worktree or moves a local ref.
    """
    clone = _clone_path(root, repo)
    url = template.format(repo=repo)
    check_remote_url(url)
    with _clone_lock(clone):
        _ensure_clone(clone, url)
        for head in heads:
            _target(clone, head)
    return clone


def _adopt_kept(
    root: Path,
    adopt: str,
    checkout: JobCheckout,
    pid: int | None,
    log: Log | None,
) -> JobCheckout | None:
    """Move a kept, ownerless worktree of the same clone to checkout's path.

    The caller holds the clone lock. None when there is nothing to adopt.
    """
    record = checkout.record.with_name(f"{adopt}.json")
    loaded = _load_record(root, record)
    if loaded is None:
        return None
    previous, data, _created = loaded
    if previous.job_id != adopt or previous.clone != checkout.clone:
        return None
    if previous.path.is_symlink() or not previous.path.is_dir():
        return None
    if _live_owner(data) is not None:
        return None
    try:
        _require_worktree_of(previous.path, checkout.clone)
    except WorktreeError:
        return None
    reason = keep_reason(previous.path, previous.start)
    if reason is None:
        return None
    moved = replace(checkout, start=previous.start)
    _write_record(moved, pid)
    try:
        _git(checkout.clone, "worktree", "move", str(previous.path), str(moved.path))
    except WorktreeError:
        moved.record.unlink(missing_ok=True)
        raise
    record.unlink(missing_ok=True)
    if log is not None:
        log(f"reusing kept worktree of job {adopt} ({reason})")
    return moved


def finish(checkout: JobCheckout, log: Log) -> bool:
    """Remove a finished job's worktree unless it holds work. True if removed.

    Decided under the clone lock: if another live `orch job run` has taken
    the worktree over (its record names another live owner), it is kept.
    """
    with _clone_lock(checkout.clone):
        reason = None
        data = _read_record(checkout.record)
        if data is not None:
            owner = _live_owner(data)
            if owner is not None and owner != os.getpid():
                reason = f"in use by pid {owner}"
        if reason is None:
            reason = keep_reason(checkout.path, checkout.start)
        if reason is None:
            _remove_locked(checkout)
    if reason is not None:
        log(f"kept job worktree {checkout.path}: {reason}")
        return False
    log(f"removed job worktree {checkout.path}")
    return True


def keep_reason(path: Path, start: str | None = None) -> str | None:
    """Why a worktree must not be deleted, or None when it is disposable.

    Unpushed commits are those reachable from HEAD but from no remote-tracking
    ref and not from `start`, the commit the job was checked out at (a PR head
    fetched by SHA is on no remote-tracking ref).
    """
    if not path.exists():
        return None
    try:
        status = _git(path, "status", "--porcelain", "--untracked-files=all")
    except WorktreeError:
        return "not a readable git worktree"
    if status.strip():
        return "uncommitted changes"
    try:
        excluded = ["--remotes"] + ([start] if start else [])
        unpushed = _git(path, "rev-list", "--count", "HEAD", "--not", *excluded)
    except WorktreeError:
        return "not a readable git worktree"
    if unpushed.strip() not in ("", "0"):
        return "unpushed commits"
    return None


def gc(
    root: Path,
    *,
    older_than_hours: float,
    log: Log,
    dry_run: bool = False,
    now: float | None = None,
) -> int:
    """Remove job worktrees older than N hours with no work and no live owner.

    Returns the number of records that could not be read or removed. The
    clone is always `<root>/clones/<owner>/<name>.git` for the record's repo
    (the record's own `clone` field is ignored), a worktree is removed only
    if it belongs to that clone, and a symlinked `jobs` directory or job
    path is refused.
    """
    root = root.absolute()
    jobs = root / "jobs"
    if jobs.is_symlink():
        log(f"skipped {jobs}: a symlink, not a job directory")
        return 1
    if not jobs.is_dir():
        return 0
    current = time.time() if now is None else now
    cutoff = older_than_hours * 3600
    problems = 0
    clones: set[Path] = set()
    for record in sorted(jobs.glob("*.json")):
        loaded = _load_record(root, record)
        if loaded is None:
            log(f"skipped unreadable job record {record}")
            problems += 1
            continue
        checkout, data, created = loaded
        if record.stem != checkout.job_id:
            log(f"skipped mismatched job record {record}")
            problems += 1
            continue
        clones.add(checkout.clone)
        age = current - created
        if age < cutoff:
            continue
        detail = f"({checkout.repo}, {int(age // 3600)}h old)"
        if dry_run:
            reason = _gc_keep_reason(checkout, data)
            if reason is not None:
                log(f"kept job worktree {checkout.path}: {reason}")
            else:
                log(f"would remove job worktree {checkout.path} {detail}")
            continue
        try:
            with _clone_lock(checkout.clone):
                # Decide under the lock, from the record as it is now: a
                # job re-run may have taken the worktree over since the scan.
                fresh = _load_record(root, record)
                if fresh is None or fresh[0] != checkout:
                    continue  # finished or replaced meanwhile; next gc decides
                reason = _gc_keep_reason(checkout, fresh[1])
                if reason is None:
                    _remove_locked(checkout)
        except (WorktreeError, OSError) as exc:
            log(f"could not remove job worktree {checkout.path}: {_one_line(exc)}")
            problems += 1
            continue
        if reason is not None:
            log(f"kept job worktree {checkout.path}: {reason}")
            continue
        log(f"removed job worktree {checkout.path} {detail}")
    if not dry_run:
        for clone in sorted(clones):
            if clone.is_dir():
                try:
                    with _clone_lock(clone):
                        _git(clone, "worktree", "prune")
                except WorktreeError:
                    problems += 1
    return problems


def _gc_keep_reason(checkout: JobCheckout, data: dict) -> str | None:
    owner = _live_owner(data)
    if owner is not None:
        return f"in use by pid {owner}"
    return keep_reason(checkout.path, checkout.start)


def _load_record(root: Path, record: Path) -> tuple[JobCheckout, dict, float] | None:
    data = _read_record(record)
    if data is None:
        return None
    try:
        job_id = str(data["job_id"])
        repo = str(data["repo"])
        checkout = JobCheckout(
            job_id=job_id,
            repo=repo,
            path=root / "jobs" / job_id,
            clone=_clone_path(root, repo),
            record=record,
            start=_valid_start(data.get("start")),
        )
        created = float(data["created_at"])
    except (ValueError, KeyError, TypeError, WorktreeError):
        return None
    return checkout, data, created


def _read_record(record: Path) -> dict | None:
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _remove_locked(checkout: JobCheckout) -> None:
    """Remove the worktree; the caller holds the clone lock."""
    if checkout.path.is_symlink():
        raise WorktreeError(f"{checkout.path} is a symlink, not a job worktree")
    if checkout.path.exists():
        _require_worktree_of(checkout.path, checkout.clone)
        # keep_reason found nothing to keep; --force only lets git also
        # drop ignored build output, which `worktree remove` refuses.
        _git(checkout.clone, "worktree", "remove", "--force", str(checkout.path))
    if checkout.clone.is_dir():
        _git(checkout.clone, "worktree", "prune")
    checkout.record.unlink(missing_ok=True)


def _require_worktree_of(path: Path, clone: Path) -> None:
    try:
        common = _git(path, "rev-parse", "--git-common-dir").strip()
    except WorktreeError:
        common = ""
    if not common or (path / common).resolve() != clone.resolve():
        raise WorktreeError(f"{path} is not a worktree of {clone}")


def _clone_path(root: Path, repo: str) -> Path:
    owner, name = _split_repo(repo)
    return root / "clones" / owner / f"{name}.git"


def _split_repo(repo: str) -> tuple[str, str]:
    parts = repo.split("/")
    if len(parts) != 2 or not all(_NAME.match(part) and ".." not in part for part in parts):
        raise WorktreeError(f"unsafe repo name {repo!r}")
    return parts[0], parts[1]


def _ensure_clone(clone: Path, url: str) -> None:
    if not (clone / "HEAD").is_file():
        if clone.exists() and any(clone.iterdir()):
            raise WorktreeError(f"{clone} exists and is not a git clone")
        clone.mkdir(parents=True, exist_ok=True)
        _git(clone, "init", "--bare", "--quiet")
        _git(clone, "remote", "add", "origin", url)
    else:
        _git(clone, "remote", "set-url", "origin", url)
    _git(clone, "fetch", "--prune", "--quiet", "origin", network=True)


def _target(clone: Path, head: str | None) -> str:
    if head is None:
        try:
            _git(clone, "remote", "set-head", "origin", "--auto", network=True)
        except WorktreeError as exc:
            raise WorktreeError(f"cannot find the default branch: {exc}") from exc
        return "refs/remotes/origin/HEAD"
    if not _has_commit(clone, head):
        # A PR head is not on a branch; GitHub serves reachable SHAs directly.
        try:
            _git(clone, "fetch", "--quiet", "origin", head, network=True)
        except WorktreeError as exc:
            raise WorktreeError(f"cannot fetch {head}: {exc}") from exc
        if not _has_commit(clone, head):
            raise WorktreeError(f"cannot fetch {head}")
    return head


def _has_commit(clone: Path, sha: str) -> bool:
    try:
        _git(clone, "cat-file", "-e", f"{sha}^{{commit}}")
    except WorktreeError:
        return False
    return True


def _write_record(checkout: JobCheckout, pid: int | None) -> None:
    owner = os.getpid() if pid is None else pid
    created = time.time()
    if checkout.record.is_file():
        try:
            created = float(json.loads(checkout.record.read_text(encoding="utf-8"))["created_at"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
    data = {
        "job_id": checkout.job_id,
        "repo": checkout.repo,
        "clone": str(checkout.clone),
        "created_at": created,
        "pid": owner,
    }
    if checkout.start is not None:
        data["start"] = checkout.start
    start = _process_start(owner)
    if start is not None:
        data["pid_start"] = start
    tmp = checkout.record.with_name(checkout.record.name + ".tmp")
    tmp.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, checkout.record)


def _recorded_start(record: Path) -> str | None:
    try:
        return _valid_start(json.loads(record.read_text(encoding="utf-8")).get("start"))
    except (OSError, ValueError, AttributeError):
        return None


def _valid_start(value: object) -> str | None:
    return value if isinstance(value, str) and _SHA.match(value) else None


def _live_owner(data: dict) -> int | None:
    pid = data.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    except OSError:
        return None
    stat = _proc_stat(pid)
    if stat is not None and stat.zombie:
        return None
    recorded = data.get("pid_start")
    if recorded is not None and (stat is None or stat.start != recorded):
        return None  # the pid was reused by an unrelated process
    return pid


def process_start(pid: int) -> str | None:
    """A pid's start time, recorded beside it so a reused pid is not mistaken."""
    return _process_start(pid)


def pid_alive(pid: int | None, start: str | None) -> bool:
    """True while pid runs and, when start was recorded, is the same process."""
    if pid is None:
        return False
    data: dict = {"pid": pid}
    if start is not None:
        data["pid_start"] = start
    return _live_owner(data) is not None


def pid_gone(pid: int, start: str | None) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        pass
    if start is None:
        return False
    current = _process_start(pid)
    return current is not None and current != start


def parent_pid(pid: int) -> int | None:
    """A pid's parent, from /proc/<pid>/stat (field 4); None if unknown."""
    stat = _proc_stat(pid)
    return stat.ppid if stat is not None else None


def descends_from(pid: int, ancestor: int, *, parent=parent_pid, limit: int = 64) -> bool:
    """True when ancestor is pid itself or one of its parents (at most limit up)."""
    current: int | None = pid
    for _ in range(limit):
        if current is None or current <= 1:
            return False
        if current == ancestor:
            return True
        current = parent(current)
    return False


def _process_start(pid: int) -> str | None:
    """Field 22 of /proc/<pid>/stat, so a reused pid is not mistaken for the owner."""
    stat = _proc_stat(pid)
    return stat.start if stat is not None else None


class _ProcStat(NamedTuple):
    zombie: bool
    ppid: int | None
    start: str | None


def _proc_stat(pid: int) -> _ProcStat | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    fields = text.rpartition(")")[2].split()
    return _ProcStat(
        zombie=fields[:1] == ["Z"],
        ppid=int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else None,
        start=fields[19] if len(fields) > 19 else None,
    )


@contextmanager
def _clone_lock(clone: Path) -> Iterator[None]:
    clone.parent.mkdir(parents=True, exist_ok=True)
    lock = clone.with_name(clone.name + ".lock")
    with lock.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def _git(cwd: Path, *args: str, network: bool = False) -> str:
    environment = dict(os.environ)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=NETWORK_TIMEOUT_SECONDS if network else LOCAL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorktreeError(f"git {args[0]} failed: {type(exc).__name__}") from exc
    if result.returncode != 0:
        detail = " ".join(result.stderr.split())[:200] or f"exit {result.returncode}"
        raise WorktreeError(f"git {args[0]} failed: {detail}")
    return result.stdout


def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split()) or type(exc).__name__


__all__ = [
    "DEFAULT_GC_HOURS",
    "JobCheckout",
    "WorktreeError",
    "check_remote_url",
    "finish",
    "gc",
    "keep_reason",
    "prepare",
    "url_template",
    "work_root",
]
