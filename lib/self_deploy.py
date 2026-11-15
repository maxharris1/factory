"""Self-deploy (issue #219): move the box checkout to origin/main.

`deploy/self-heal.sh once` runs `orch heal deploy` before
`orch health --json | orch heal`, so a merged commit reaches the checkout
within one self-heal cycle, and the heal pass that follows (a new process,
which reads the new HEAD) restarts each component whose heartbeat names
the old commit: the poller through deploy/poller-service.sh, an idle
waiter through deploy/waiter-service.sh, a busy waiter drained first
(lib/heal.py). The poller also runs `deploy/self-heal.sh kick` after a
cycle that merged a PR of the factory's own repo (MergeKick). Nothing
here calls an LLM.

One pass, in order; the first guard that fails ends it:

1. Only the account that owns the checkout and can write to it pulls;
   any other account exits at once and logs nothing. A pass holds
   self-deploy.lock in the state directory to its end, so a direct
   `orch heal deploy` and a timer pass never overlap; a pass that finds
   it held returns `skipped` at once and writes nothing.
2. The checkout is left alone (logged, escalated once) when it is not on
   main, has a merge, rebase, cherry-pick or revert in progress, has
   staged changes, has a KEPT path (or a parent of one) that is not a
   regular file (or a real directory), or has a modified tracked file
   outside KEPT.
3. `git fetch origin main` (no secret in its environment; on failure only
   the exit code is logged). Failures for FETCH_ESCALATE_SECONDS escalate.
4. HEAD is origin/main: nothing to do.
5. HEAD is not an ancestor of origin/main (local commits), or a local
   path the old HEAD does not track (untracked, ignored, a directory or a
   symlink) sits where upstream adds one, or inside a directory upstream
   turns into a file or symlink: left alone, escalated once. So is a kept
   file with local edits that upstream turns (itself or a parent) into
   anything but a regular file under directories. So the merge and a
   rollback's reset only ever touch tracked paths and the backed-up kept
   files, and merged kept files are written only to regular files.
6. Fast-forward keeping the local edits to KEPT files: each dirty kept
   file and `git diff HEAD` are backed up under self-deploy/<stamp>/ in
   the state directory; a kept file upstream also changed gets a 3-way
   merge computed before anything is touched, and any conflict stops the
   pass with the checkout unchanged. Then the changed kept files are
   checked out, `git merge --ff-only` runs, and the merged contents are
   written back. A failure there resets to the old HEAD and restores the
   backups.
   Before anything is touched (#297), the pass counts the box's jobs in
   orch.db (ORCH_DB) and, when there are none, marks the box `deploying`
   in device_sync in the same transaction, so no job is placed on the box
   while the checkout moves. A box with a job is left alone (`busy`, not
   escalated) and deployed by a later pass; meanwhile the waiter's
   preflight refuses box starts because HEAD lags origin/main. The mark is
   dropped when the apply ends, and the box's own row and every lagging
   row once HEAD is origin/main. A waiter's synced or refused row stays.
7. When the merge changed deploy/ or requirements.txt, it runs
   `deploy/install.sh --update` (output in self-deploy-install.log). When
   it changed skills/ (the pin and the pinned files), or HOME's installed
   skills miss the pin, it then runs `orch install-skills` from the
   checkout into HOME, this account's own home. A home it cannot read
   counts as missing the pin. Other bot accounts are never written from
   here (#345): each box bot's waiter, running as that account, installs
   into its own home while it has no box job (lib/waiter.py). An
   up-to-date pass makes the same check, so a failed install is rerun once
   its cause is gone; that install takes the same `deploying` mark as step
   6 and waits (`busy`) while the box has a job, and a failure is
   `install-failed`, never `up-to-date`.

Escalation goes through lib/heal.py's webhook POST (URL and auth from the
environment, never logged), once per reason: self-deploy.state.json keeps
the reason last escalated, and an up-to-date or deployed pass clears it.
Each pass logs to self-heal.log; a repeated outcome is logged once.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from lib import device_sync, health, pstack_pin
from lib.device_pool import slots_in_use
from lib.heal import (
    ESCALATE,
    LOG_NAME,
    Action,
    Poster,
    Runner,
    Webhook,
    _webhook,
    escalate,
    run_service,
    stamp,
)
from lib.grok_delivery import send_webhook
from lib.machines_yml import MachinesConfig
from lib.queue import SQLiteJobQueue

ROOT = Path(__file__).resolve().parent.parent

BRANCH = "main"
REMOTE_REF = "refs/remotes/origin/main"
KEPT = ("machines.yml", "orch.yml", "deploy/install.sh")
INSTALL_PATHS = ("deploy/", "requirements.txt")
SKILLS_PATHS = ("skills/",)
STATE_NAME = "self-deploy.state.json"
LOCK_NAME = "self-deploy.lock"
BACKUP_DIR = "self-deploy"
INSTALL_LOG = "self-deploy-install.log"
FETCH_TIMEOUT = 60
GIT_TIMEOUT = 120
FETCH_ESCALATE_SECONDS = 30 * 60
TOKEN_NAMES = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")

SKIPPED = "skipped"
UP_TO_DATE = "up-to-date"
DEPLOYED = "deployed"
REFUSED = "refused"
BUSY = "busy"
CONFLICT = "conflict"
FETCH_FAILED = "fetch-failed"
ROLLED_BACK = "rolled-back"
WOULD_DEPLOY = "would-deploy"
INSTALL_FAILED = "install-failed"


@dataclass(frozen=True)
class Outcome:
    kind: str
    detail: str = ""
    old: str = ""
    new: str = ""


class GitError(Exception):
    def __init__(self, args: Sequence[str], code: int):
        super().__init__(f"git {args[0]} exited {code}")
        self.code = code


def owns(checkout: Path) -> bool:
    """This account owns the checkout and may write to it and its .git."""
    try:
        return (checkout.stat().st_uid == os.getuid()
                and os.access(checkout, os.W_OK)
                and os.access(checkout / ".git", os.W_OK))
    except OSError:
        return False


class Git:
    """git in the checkout, with a fixed environment and output kept off the logs."""

    def __init__(self, checkout: Path, env: Mapping[str, str]):
        self.checkout = checkout
        self.env = dict(env)

    def run(self, *args: str, timeout: float = GIT_TIMEOUT,
            stdin: bytes | None = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(["git", *args], cwd=self.checkout, env=self.env,
                                  input=stdin if stdin is not None else b"",
                                  capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(args, 124, b"", b"")
        except OSError:
            return subprocess.CompletedProcess(args, 126, b"", b"")

    def out(self, *args: str, timeout: float = GIT_TIMEOUT, stdin: bytes | None = None) -> bytes:
        result = self.run(*args, timeout=timeout, stdin=stdin)
        if result.returncode != 0:
            raise GitError(args, result.returncode)
        return result.stdout

    def text(self, *args: str) -> str:
        return self.out(*args).decode("utf-8", "replace").strip()

    def ok(self, *args: str) -> bool:
        return self.run(*args).returncode == 0

    def names(self, *args: str) -> list[str]:
        return [name for name in self.out(*args).decode("utf-8", "replace").split("\0") if name]


def git_env(environ: Mapping[str, str], webhook: Webhook) -> dict[str, str]:
    drop = set(webhook.secret_names) | set(TOKEN_NAMES)
    env = {k: v for k, v in environ.items() if k not in drop}
    env.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    return env


# --- state -----------------------------------------------------------------------------------


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: Mapping) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(dict(state), sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


# --- one pass --------------------------------------------------------------------------------


def _shape(git: Git) -> tuple[str | None, list[str]]:
    """Why the checkout must be left alone (None if it may move), and the
    dirty kept files."""
    branch = git.run("symbolic-ref", "--quiet", "--short", "HEAD")
    name = branch.stdout.decode("utf-8", "replace").strip()
    if branch.returncode != 0:
        return "not on main (detached HEAD)", []
    if name != BRANCH:
        return f"not on main (on {name})", []
    for marker in ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD",
                   "REVERT_HEAD"):
        path = Path(git.text("rev-parse", "--git-path", marker))
        if not path.is_absolute():
            path = git.checkout / path
        if path.exists():
            return f"{marker} in progress", []
    if not git.ok("diff", "--cached", "--quiet"):
        return "staged changes", []
    odd = _odd_kept_path(git.checkout)
    if odd is not None:
        return f"unsupported file type at {odd}", []
    dirty = git.names("diff", "--name-only", "-z", "HEAD")
    other = sorted(name for name in dirty if name not in KEPT)
    if other:
        return "local edits outside the keep list: " + ",".join(other[:5]), []
    hidden = _hidden_edits(git, dirty)
    if hidden:
        return "local edits git diff does not report: " + ",".join(hidden[:5]), []
    return None, sorted(dirty)


def _hidden_edits(git: Git, dirty: Sequence[str]) -> list[str]:
    """Tracked paths whose local state `git diff` may not show, so a backup
    built from it would miss them and a rollback's reset would erase them:
    index flags that hide the working tree (assume-unchanged, skip-worktree),
    and regular files whose bytes on disk hash differently from the index
    (a stale fsmonitor or stat cache)."""
    hidden: set[str] = set()
    for line in git.names("ls-files", "-v", "-z"):
        tag, _, name = line.partition(" ")
        if tag.islower() or tag == "S":
            hidden.add(name)
    expected: dict[str, str] = {}
    for line in git.names("ls-files", "-s", "-z"):
        meta, _, name = line.partition("\t")
        mode, blob, _ = meta.split(" ", 2)
        if mode not in ("100644", "100755") or name in dirty or name in hidden:
            continue
        try:
            regular = stat.S_ISREG(os.lstat(git.checkout / name).st_mode)
        except FileNotFoundError:
            regular = False
        if not regular or "\n" in name:
            hidden.add(name)
        else:
            expected[name] = blob
    if expected:
        names = list(expected)
        hashes = git.out("hash-object", "--stdin-paths",
                         stdin="".join(f"{name}\n" for name in names).encode()).split()
        if len(hashes) != len(names):
            raise GitError(("hash-object",), 1)
        hidden.update(name for name, got in zip(names, hashes)
                      if got.decode("ascii", "replace") != expected[name])
    return sorted(hidden)


def _odd_kept_path(checkout: Path) -> str | None:
    """The first part of a kept path that is not a real directory (parents)
    or a regular file (the file itself), which a backup cannot restore."""
    for name in KEPT:
        parts = name.split("/")
        for depth in range(1, len(parts) + 1):
            try:
                mode = os.lstat(checkout.joinpath(*parts[:depth])).st_mode
            except FileNotFoundError:
                break
            leaf = depth == len(parts)
            if not (stat.S_ISREG(mode) if leaf else stat.S_ISDIR(mode)):
                return "/".join(parts[:depth])
    return None


def _in_the_way(git: Git, old: str, new: str) -> str | None:
    """A local path the merge would have to overwrite or delete: one that
    upstream adds (or a parent of it) but the old HEAD does not track."""
    tracked = set(git.names("ls-tree", "-r", "-t", "-z", "--name-only", old))
    for name in git.names("diff", "--name-only", "-z", "--no-renames", "--diff-filter=A",
                          old, new):
        parts = name.split("/")
        for depth in range(1, len(parts) + 1):
            prefix = "/".join(parts[:depth])
            if prefix in tracked:
                continue
            if os.path.lexists(git.checkout / prefix):
                return prefix
            break
    return None


def _tree(git: Git, rev: str) -> dict[str, tuple[str, str]]:
    """Every path `rev` tracks, directories included: (mode, object type)."""
    entries: dict[str, tuple[str, str]] = {}
    for line in git.names("ls-tree", "-r", "-t", "-z", rev):
        meta, _, name = line.partition("\t")
        mode, kind, _ = meta.split(" ", 2)
        entries[name] = (mode, kind)
    return entries


def _type_change(git: Git, old: str, new: str, dirty: Sequence[str]) -> tuple[str, str] | None:
    """Why a type change upstream could lose local data, as (kind of
    refusal, path): a directory the old HEAD tracks that becomes a file or
    a symlink while it holds a path the old HEAD does not track (git would
    delete it), or a kept file with local edits that is, or sits under, a
    path upstream turns into anything but a regular file and directories
    (the merged edit would be written through it)."""
    before = _tree(git, old)
    after = _tree(git, new)
    for name, (_, kind) in sorted(after.items()):
        if kind != "tree" and before.get(name, ("", ""))[1] == "tree":
            local = git.names("ls-files", "--others", "-z", "--", name)
            if local:
                return "in-the-way", sorted(local)[0]
    for name in dirty:
        parts = name.split("/")
        for depth in range(1, len(parts) + 1):
            prefix = "/".join(parts[:depth])
            mode, kind = after.get(prefix, ("", ""))
            if not kind:
                break
            leaf = depth == len(parts)
            if not (mode in ("100644", "100755") if leaf else kind == "tree"):
                return "kept-type", prefix
    return None


def _merge_kept(git: Git, files: Sequence[str], old: str, new: str
                ) -> tuple[dict[str, bytes], list[str]]:
    """3-way merge each kept file's local edit onto upstream's change:
    (merged contents, the files that conflict)."""
    merged: dict[str, bytes] = {}
    conflicts: list[str] = []
    with tempfile.TemporaryDirectory(prefix="self-deploy.") as scratch:
        for name in files:
            local = git.checkout / name
            base = git.run("show", f"{old}:{name}")
            theirs = git.run("show", f"{new}:{name}")
            if not local.is_file() or base.returncode != 0 or theirs.returncode != 0:
                conflicts.append(name)
                continue
            base_path = Path(scratch) / "base"
            theirs_path = Path(scratch) / "theirs"
            base_path.write_bytes(base.stdout)
            theirs_path.write_bytes(theirs.stdout)
            result = git.run("merge-file", "-p", str(local), str(base_path), str(theirs_path))
            if result.returncode != 0:
                conflicts.append(name)
            else:
                merged[name] = result.stdout
    return merged, conflicts


def _backup(git: Git, state_dir: Path, dirty: Sequence[str], now: float) -> Path:
    root = state_dir / BACKUP_DIR
    root.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix=time.strftime("%Y%m%dT%H%M%SZ-", time.gmtime(now)),
                                   dir=root))
    for name in dirty:
        source = git.checkout / name
        if source.is_file():
            target = folder / "files" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    (folder / "local.patch").write_bytes(git.out("diff", "--binary", "HEAD"))
    return folder


def _restore(git: Git, folder: Path, old: str, dirty: Sequence[str]) -> bool:
    """Back to the old HEAD with the local edits as backed up; True when
    `git diff HEAD` matches the saved patch again."""
    if not git.ok("reset", "--quiet", "--hard", old):
        return False
    for name in dirty:
        source = folder / "files" / name
        if source.is_file():
            shutil.copy2(source, git.checkout / name)
        else:
            (git.checkout / name).unlink(missing_ok=True)
    current = git.run("diff", "--binary", "HEAD")
    return current.returncode == 0 and current.stdout == (folder / "local.patch").read_bytes()


def _mark_deploying_unless_busy(
    queue: SQLiteJobQueue, device: str, machines: MachinesConfig | None,
    checkout: Path, old: str, new: str, now: float,
) -> int:
    with queue.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        jobs = slots_in_use(connection, machines).get(device, 0)
        if jobs == 0:
            device_sync.upsert(connection, device=device, bot="", harness="",
                               checkout=str(checkout), target=new, sha=old,
                               state=device_sync.DEPLOYING, detail="", now=now)
        connection.commit()
    return jobs


def _release(queue: SQLiteJobQueue, device: str, states: Sequence[str]) -> None:
    with queue.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        device_sync.clear(connection, device, states, bots=("",))
        if device_sync.LAGGING in states:
            device_sync.clear(connection, device, (device_sync.LAGGING,))
        connection.commit()


def run_pass(
    checkout: Path,
    state_dir: Path,
    *,
    environ: Mapping[str, str],
    webhook: Webhook,
    now: float,
    install: Runner = run_service,
    clock: Callable[[], float] = time.monotonic,
    dry_run: bool = False,
    queue: SQLiteJobQueue | None = None,
    device: str = "box",
    machines: MachinesConfig | None = None,
    homes: Sequence[Path] = (),
) -> tuple[Outcome, dict]:
    """Decide and do one deploy pass; the outcome and what to log about it."""
    git = Git(checkout, git_env(environ, webhook))
    try:
        reason, dirty = _shape(git)
    except GitError as exc:
        return Outcome(REFUSED, f"git unreadable ({exc})"), {}
    if reason is not None:
        return Outcome(REFUSED, reason), {}
    fetch = git.run("fetch", "--quiet", "--no-tags", "origin",
                    f"+refs/heads/{BRANCH}:{REMOTE_REF}", timeout=FETCH_TIMEOUT)
    if fetch.returncode != 0:
        return Outcome(FETCH_FAILED, f"fetch failed exit={fetch.returncode}"), {}
    try:
        old = git.text("rev-parse", "--verify", "HEAD")
        new = git.text("rev-parse", "--verify", REMOTE_REF)
    except GitError as exc:
        return Outcome(REFUSED, f"git unreadable ({exc})"), {}
    if old == new:
        if dry_run:
            return Outcome(UP_TO_DATE, old=old, new=new), {}
        if queue is not None:
            _release(queue, device, device_sync.STATES)
        stale = _stale_homes(checkout, homes)
        if not stale:
            return Outcome(UP_TO_DATE, old=old, new=new), {}
        if queue is None:
            return _installed(old, new, _install_skills(checkout, stale, environ, webhook,
                                                        state_dir, install, clock))
        jobs = _mark_deploying_unless_busy(queue, device, machines, checkout, old, new, now)
        if jobs:
            return Outcome(BUSY, f"{jobs} jobs on {device}, install-skills next pass",
                           old, new), {}
        try:
            skills = _install_skills(checkout, stale, environ, webhook, state_dir,
                                     install, clock)
        finally:
            _release(queue, device, (device_sync.DEPLOYING,))
        return _installed(old, new, skills)
    if not git.ok("merge-base", "--is-ancestor", old, new):
        return Outcome(REFUSED, f"local commits: HEAD {old[:12]} is not behind origin/main",
                       old, new), {}
    try:
        blocker = _in_the_way(git, old, new)
    except GitError as exc:
        return Outcome(REFUSED, f"git unreadable ({exc})", old, new), {}
    if blocker is not None:
        return Outcome(REFUSED, f"local path in the way of upstream: {blocker}", old, new), {}
    try:
        changed_type = _type_change(git, old, new, dirty)
    except GitError as exc:
        return Outcome(REFUSED, f"git unreadable ({exc})", old, new), {}
    if changed_type is not None:
        kind, path = changed_type
        detail = (f"local path in the way of upstream: {path}" if kind == "in-the-way"
                  else f"upstream changes the file type at {path}, which has local edits")
        return Outcome(REFUSED, detail, old, new), {}
    if dry_run:
        return Outcome(WOULD_DEPLOY, f"{old[:12]}..{new[:12]}", old, new), {}
    started = clock()
    upstream = set(git.names("diff", "--name-only", "-z", old, new, "--", *dirty)) if dirty else set()
    changed = sorted(name for name in dirty if name in upstream)
    merged, conflicts = _merge_kept(git, changed, old, new)
    if conflicts:
        return Outcome(CONFLICT, "local edits conflict with upstream: " + ",".join(conflicts),
                       old, new), {}
    if queue is None:
        return _apply(git, state_dir, dirty, changed, merged, old, new, environ=environ,
                      webhook=webhook, now=now, install=install, clock=clock, started=started,
                      homes=homes)
    jobs = _mark_deploying_unless_busy(queue, device, machines, checkout, old, new, now)
    if jobs:
        return Outcome(BUSY, f"{jobs} jobs on {device}, retry next pass", old, new), {}
    try:
        outcome, extra = _apply(git, state_dir, dirty, changed, merged, old, new,
                                environ=environ, webhook=webhook, now=now, install=install,
                                clock=clock, started=started, homes=homes)
    except BaseException:
        _release(queue, device, (device_sync.DEPLOYING,))
        raise
    _release(queue, device,
             device_sync.STATES if outcome.kind == DEPLOYED else (device_sync.DEPLOYING,))
    return outcome, extra


def _apply(
    git: Git,
    state_dir: Path,
    dirty: Sequence[str],
    changed: Sequence[str],
    merged: Mapping[str, bytes],
    old: str,
    new: str,
    *,
    environ: Mapping[str, str],
    webhook: Webhook,
    now: float,
    install: Runner,
    clock: Callable[[], float],
    started: float,
    homes: Sequence[Path],
) -> tuple[Outcome, dict]:
    checkout = git.checkout
    folder = _backup(git, state_dir, dirty, now)
    try:
        if changed:
            git.out("checkout", "--", *changed)
        git.out("merge", "--ff-only", "--quiet", new)
        for name, body in merged.items():
            (checkout / name).write_bytes(body)
    except (GitError, OSError) as exc:
        restored = _restore(git, folder, old, dirty)
        detail = f"apply failed ({exc}); " + (
            "restored the old HEAD and local edits" if restored
            else f"restore incomplete, backup in {folder}")
        return Outcome(ROLLED_BACK, detail, old, new), {}
    extra = {"kept": ",".join(dirty) or "-", "merged": ",".join(changed) or "-",
             "seconds": clock() - started}
    env = {k: v for k, v in environ.items() if k not in webhook.secret_names}
    if git.names("diff", "--name-only", "-z", old, new, "--", *INSTALL_PATHS):
        install_started = clock()
        code = install([str(checkout / "deploy" / "install.sh"), "--update"], env,
                       state_dir / INSTALL_LOG)
        extra["install"] = (code, clock() - install_started)
    targets = (homes if git.names("diff", "--name-only", "-z", old, new, "--", *SKILLS_PATHS)
               else _stale_homes(checkout, homes))
    if targets:
        extra["skills"] = _install_skills(checkout, targets, environ, webhook, state_dir,
                                          install, clock)
    return Outcome(DEPLOYED, f"{old[:12]}..{new[:12]}", old, new), extra


def own_home(environ: Mapping[str, str]) -> tuple[Path, ...]:
    return (Path(environ["HOME"]),) if environ.get("HOME") else ()


def _stale_homes(checkout: Path, homes: Sequence[Path]) -> list[Path]:
    try:
        pstack_pin.load_pin(checkout)
    except (OSError, ValueError, UnicodeError):
        return []
    stale = []
    for home in homes:
        try:
            if pstack_pin.installed_errors(checkout, home):
                stale.append(home)
        except (OSError, RuntimeError, ValueError, UnicodeError):
            stale.append(home)
    return stale


def _installed(old: str, new: str, skills: tuple[int, float]) -> tuple[Outcome, dict]:
    if skills[0] != 0:
        return (Outcome(INSTALL_FAILED, f"orch install-skills exited {skills[0]}", old, new),
                {"skills": skills})
    return Outcome(UP_TO_DATE, old=old, new=new), {"skills": skills}


def _install_skills(
    checkout: Path, homes: Sequence[Path], environ: Mapping[str, str], webhook: Webhook,
    state_dir: Path, install: Runner, clock: Callable[[], float],
) -> tuple[int, float]:
    env = {k: v for k, v in environ.items() if k not in webhook.secret_names}
    started = clock()
    first_failure = 0
    for home in homes:
        code = install([sys.executable, str(checkout / "bin" / "orch"), "install-skills"],
                       {**env, "HOME": str(home)}, state_dir / INSTALL_LOG)
        first_failure = first_failure or code
    return first_failure, clock() - started


def deploy(
    checkout: Path,
    state_dir: Path,
    *,
    device: str,
    environ: Mapping[str, str],
    webhook: Webhook,
    now: float,
    install: Runner = run_service,
    post: Poster = send_webhook,
    owner: Callable[[Path], bool] = owns,
    clock: Callable[[], float] = time.monotonic,
    dry_run: bool = False,
    machines: MachinesConfig | None = None,
    homes: Sequence[Path] | None = None,
) -> Outcome:
    """One pass: run it, log it, escalate once per reason, save the state."""
    if not owner(checkout):
        return Outcome(SKIPPED, "not the checkout owner")
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / LOCK_NAME, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return Outcome(SKIPPED, "another deploy pass is running")

        def log(line: str) -> None:
            with open(state_dir / LOG_NAME, "a", encoding="utf-8") as out:
                out.write(f"{stamp(now)} self-deploy: {line}\n")

        state_path = state_dir / STATE_NAME
        state = load_state(state_path)
        db = environ.get("ORCH_DB")
        queue = SQLiteJobQueue(Path(db), clock=lambda: now) if db and Path(db).is_file() else None
        outcome, extra = run_pass(checkout, state_dir, environ=environ, webhook=webhook, now=now,
                                  install=install, clock=clock, dry_run=dry_run, queue=queue,
                                  device=device, machines=machines,
                                  homes=own_home(environ) if homes is None else homes)
        escalation: str | None = None
        key: str | None = None
        if outcome.kind == FETCH_FAILED:
            since = state.get("fetch_failed_since")
            since = float(since) if isinstance(since, (int, float)) else now
            state["fetch_failed_since"] = since
            if now - since >= FETCH_ESCALATE_SECONDS:
                key = "fetch"
                escalation = f"{outcome.detail} for {int((now - since) // 60)}m"
        else:
            state.pop("fetch_failed_since", None)
        if outcome.kind in (REFUSED, CONFLICT):
            key = f"{outcome.kind}:{outcome.detail}"
            escalation = outcome.detail
        if outcome.kind == ROLLED_BACK:
            key = f"{outcome.kind}:{outcome.new}"
            escalation = outcome.detail
        if outcome.kind == DEPLOYED and "install" in extra and extra["install"][0] != 0:
            key = f"install:{outcome.new}"
            escalation = f"deploy/install.sh --update exited {extra['install'][0]}"
        elif (outcome.kind in (DEPLOYED, INSTALL_FAILED) and "skills" in extra
              and extra["skills"][0] != 0):
            key = f"install-skills:{outcome.new}"
            escalation = f"orch install-skills exited {extra['skills'][0]}"

        if outcome.kind == DEPLOYED:
            line = (f"deployed {outcome.detail} kept={extra['kept']} merged={extra['merged']} "
                    f"{extra['seconds']:.1f}s")
            log(line)
            if "install" in extra:
                code, seconds = extra["install"]
                log(f"install.sh --update exit={code} {seconds:.1f}s")
            if "skills" in extra:
                code, seconds = extra["skills"]
                log(f"install-skills exit={code} {seconds:.1f}s")
            state["last"] = None
        else:
            note = {
                UP_TO_DATE: f"up to date at {outcome.new[:12]}",
                WOULD_DEPLOY: f"would deploy {outcome.detail}",
            }.get(outcome.kind, f"{outcome.kind}: {outcome.detail}")
            if outcome.kind == ROLLED_BACK or state.get("last") != note:
                log(note)
            state["last"] = note
            if "skills" in extra:
                code, seconds = extra["skills"]
                log(f"install-skills exit={code} {seconds:.1f}s")
        if outcome.kind in (UP_TO_DATE, DEPLOYED) and key is None:
            state.pop("escalated", None)
        if key is not None and state.get("escalated") != key and not dry_run:
            action = Action(ESCALATE, f"deploy @ {device}", "deploy", escalation or outcome.kind)
            result = escalate([action], device=device, now=now, webhook=webhook, environ=environ,
                              post=post)
            log(f"escalate deploy @ {device} ({action.reason}) {result}")
            state["escalated"] = key
        save_state(state_path, state)
        return outcome


# --- the poller's post-merge kick ------------------------------------------------------------

_REPO = re.compile(r"github\.com[:/]+([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$")


def own_repo(checkout: Path = ROOT) -> str | None:
    """owner/name of the checkout's origin, lowercased, or None."""
    try:
        result = subprocess.run(["git", "-C", str(checkout), "remote", "get-url", "origin"],
                                capture_output=True, text=True, timeout=10, check=False,
                                stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = _REPO.search(result.stdout.strip()) if result.returncode == 0 else None
    return match.group(1).lower() if match else None


def spawn_kick(argv: Sequence[str], env: Mapping[str, str], log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as out:
        subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=out,
                         stderr=subprocess.STDOUT, env=dict(env), start_new_session=True)


class MergeKick:
    """After a poll cycle that merged a PR of the factory's own repo, start
    `deploy/self-heal.sh kick` (detached) so the merge deploys now, not at
    the next timer pass. A failure is reported and polling goes on."""

    def __init__(
        self,
        merged: Callable[[], set[str]],
        *,
        repo: str | None,
        on_error: Callable[[Exception], None],
        root: Path = ROOT,
        environ: Mapping[str, str] | None = None,
        secret_names: Sequence[str] = (),
        spawn: Callable[[Sequence[str], Mapping[str, str], Path], None] = spawn_kick,
    ):
        self.merged = merged
        self.repo = repo.lower() if repo else None
        self.on_error = on_error
        self.root = root
        source = environ if environ is not None else os.environ
        self.env = {k: v for k, v in source.items() if k not in set(secret_names)}
        self.spawn = spawn
        state = self.env.get("ORCH_STATE_DIR") or str(Path.home() / ".local/state/factory")
        self.log_path = Path(state) / "self-heal-service.log"

    def __call__(self) -> None:
        if self.repo is None or self.repo not in {r.lower() for r in self.merged()}:
            return
        try:
            self.spawn([str(self.root / "deploy" / "self-heal.sh"), "kick"], self.env,
                       self.log_path)
        except Exception as exc:
            self.on_error(exc)


# --- orch heal deploy ------------------------------------------------------------------------

USAGE = """\
orch heal deploy [--state DIR] [--checkout DIR] [--machines PATH] [--orch PATH]
                 [--device NAME] [--dry-run]
"""


def main(argv: list[str], *, clock: Callable[[], float] = time.time) -> int:
    parser = argparse.ArgumentParser(prog="orch heal deploy", usage=USAGE)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--checkout", type=Path, default=ROOT)
    parser.add_argument("--machines", type=Path)
    parser.add_argument("--orch", type=Path)
    parser.add_argument("--device")
    parser.add_argument("--dry-run", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    state_dir = args.state or Path(os.environ.get("ORCH_STATE_DIR")
                                   or Path.home() / ".local/state/factory")
    checkout = args.checkout.resolve()
    if not owns(checkout):
        return 0
    webhook = _webhook(args.orch if args.orch is not None else checkout / "orch.yml")
    machines = health.load_machines(args.machines)
    device = args.device or health.local_device(machines)
    deploy(checkout, state_dir, device=device, environ=os.environ, webhook=webhook,
           now=clock(), dry_run=args.dry_run, machines=machines)
    return 0
