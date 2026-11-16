"""`orch heal devices` (issue #297): bring every remote factory checkout to the box's commit.

lib/self_deploy.py moves the box checkout to origin/main. This pass then
moves each remote one to the box checkout's HEAD: the commit whose
skills/skills.lock the waiter checks a start against, so a remote never
runs ahead of the box when the box deploy was refused.

Targets come from the box's machines.yml: every schedulable device that is
not local, and each of its harnesses with a factory_root and an ssh_alias,
so a device added later is covered with no code change. Each target is a
bot's own checkout and has one device_sync row (lib/device_sync.py).

One pass:

1. Observe every target in its own thread, one ssh call each: the
   checkout's HEAD and branch, and the hash of every pinned skill file
   (lib/skill_preflight.py's script and pinned set). Synced means HEAD is
   the target, on main, and every pinned file matches.
2. In one orch.db write transaction: record each observation. A target that
   needs a deploy is marked deploying, which keeps every harness off its
   device while the checkout moves, whatever jobs run there (#463). A
   running job is never touched: it works in its own checkout under the
   device's workdir, never in the factory checkout. A dry run marks it
   lagging (or missing) and writes nothing.
3. Deploy each deploying target in its own thread, one ssh call under the
   bot's login shell: clone a missing checkout, or fetch origin main and
   refuse (nothing written) on staged or modified tracked files, a merge or
   rebase in progress, or a HEAD or local main with commits the target
   lacks; then `git checkout -B main <target>` with origin/main upstream
   (refused when an untracked or ignored local file is in the way), and
   `bin/orch install-skills` from that checkout under the Python the
   installed ~/.local/bin/orch wrapper names (deploy/install.sh's venv),
   else python3. Local files are never overwritten or discarded.
4. Observe again: synced, or refused with our own one-line reason.
   A refusal is escalated once per (target, reason, commit) through
   lib/heal.py; device-deploy.state.json remembers what was escalated.

A local account's own checkout (a box factory_root other than the box
checkout) is moved by that bot's waiter with the same git steps
(`sync_local`), since only that account may write it.

deploy/self-heal.sh starts this pass detached after each box pass, so a
slow clone never delays the box. A pass that finds device-deploy.lock held
exits at once. Every pass converges from what it observes, so a crashed
pass (a target left deploying) is repaired by the next one. Git runs with no
token or webhook variable in its environment, and only exit codes and our
own reasons are logged.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import sqlite3
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from lib import device_sync, health
from lib.device_pool import box_device
from lib.device_sync import Observed
from lib.grok_delivery import send_webhook
from lib.heal import ESCALATE, LOG_NAME, Action, Poster, Webhook, _webhook, escalate, stamp
from lib.machines_yml import MachinesConfig
from lib.queue import SQLiteJobQueue
from lib.remote import (
    CHECKOUT_TIMEOUT_SECONDS,
    SSH_UNREACHABLE,
    CaptureTransport,
    RemoteError,
    RemoteTarget,
    SshTransport,
    _login,
    _marked,
    ssh_argv,
)
from lib.self_deploy import ROOT, TOKEN_NAMES, load_state, own_repo, save_state
from lib.skill_preflight import (
    Pinned,
    PreflightError,
    compare,
    pinned_files,
    remote_seen,
    skills_dir,
    wanted,
)
from lib.wait import OWNING_BOTS
from lib.worktrees import url_template

STATE_NAME = "device-deploy.state.json"
LOCK_NAME = "device-deploy.lock"
PROBE_JOB = "device-sync"
SYNC_MARK = "orch-sync:"
_HARNESS_BOT = {harness: bot for bot, harness in OWNING_BOTS.items()}

_CHECKOUT = (
    'checkout=$1; url=$2; target=$3; '
    f"fail() {{ printf '{SYNC_MARK} %s\\n' \"$*\"; exit 3; }}; "
    "export GIT_TERMINAL_PROMPT=0; "
    "fresh=; "
    'if [ ! -e "$checkout/.git" ]; then '
    'if [ -e "$checkout" ] && [ -n "$(ls -A "$checkout" 2>/dev/null)" ]; then fail "not a git checkout and not empty"; fi; '
    'mkdir -p -- "$(dirname -- "$checkout")" || fail "cannot make the parent directory"; '
    'git clone -q --no-checkout "$url" "$checkout" || fail "git clone exited $?"; '
    "fresh=1; "
    "fi; "
    'cd -- "$checkout" || fail "cannot enter the checkout"; '
    'if [ -z "$fresh" ]; then '
    'git fetch -q --no-tags origin +refs/heads/main:refs/remotes/origin/main || fail "git fetch exited $?"; '
    "fi; "
    'git cat-file -e "$target^{commit}" 2>/dev/null || fail "target commit not on origin"; '
    'if [ -z "$fresh" ]; then '
    "g=$(git rev-parse --git-dir) || fail \"cannot read .git\"; "
    "for x in MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD rebase-merge rebase-apply; do "
    '[ -e "$g/$x" ] && fail "a merge or rebase is in progress"; done; '
    "if h=$(git rev-parse -q --verify 'HEAD^{commit}'); then "
    'git diff --quiet HEAD -- || fail "tracked files have local changes"; '
    'git merge-base --is-ancestor "$h" "$target" || fail "HEAD has commits not on origin/main"; '
    "fi; "
    "if m=$(git rev-parse -q --verify 'refs/heads/main^{commit}'); then "
    'git merge-base --is-ancestor "$m" "$target" || fail "local main has commits not on origin/main"; '
    "fi; "
    "fi; "
    'git checkout -q --no-overwrite-ignore -B main "$target" '
    '|| fail "git checkout exited $? (untracked or ignored files in the way?)"; '
    'git branch -q --set-upstream-to=origin/main main || fail "cannot set the upstream"; '
)
_SYNCED = f"printf '{SYNC_MARK} ok %s\\n' \"$(git rev-parse HEAD)\""
_DEPLOY = (
    _CHECKOUT
    + "py=python3; "
    'if [ -f "$HOME/.local/bin/orch" ]; then '
    """w=$(sed -n 's/^exec "\\{0,1\\}\\([^" ]*\\)"\\{0,1\\} .*/\\1/p' "$HOME/.local/bin/orch" | head -n 1); """
    '[ -n "$w" ] && [ -x "$w" ] && py=$w; '
    "fi; "
    + '"$py" ./bin/orch install-skills >/dev/null 2>&1 || fail "install-skills exited $?"; '
    + _SYNCED
)


@dataclass(frozen=True)
class Target:
    device: str
    harness: str
    bot: str
    checkout: str
    remote: RemoteTarget

    @property
    def key(self) -> tuple[str, str]:
        return (self.device, self.bot)

    @property
    def label(self) -> str:
        return f"{self.device} {self.bot}"


def targets(config: MachinesConfig | None) -> list[Target]:
    """Every remote checkout the fleet keeps in sync; never the local box."""
    found: list[Target] = []
    for device in config.devices if config is not None else ():
        if device.local or not device.schedulable:
            continue
        for harness, user in device.harnesses.items():
            bot = _HARNESS_BOT.get(harness)
            checkout = device.factory_root_for(harness)
            alias = device.ssh_alias_for(harness)
            if bot is None or not checkout or not alias:
                continue
            try:
                remote = RemoteTarget(
                    device=device.name,
                    alias=alias,
                    user=user,
                    workdir_root=device.workdir_for(harness) or checkout,
                    job_id=PROBE_JOB,
                )
            except RemoteError:
                continue
            found.append(Target(device.name, harness, bot, checkout, remote))
    return found


def deploy_script(target: Target, *, url: str, sha: str) -> str:
    return _login(_DEPLOY, "orch-sync", (target.checkout, url, sha))


def observe(transport: CaptureTransport, target: Target, pinned: Pinned) -> Observed:
    expected = wanted(skills_dir(target.bot), target.checkout, pinned)
    try:
        seen = remote_seen(transport, target.remote, target.checkout, sorted(expected))
    except PreflightError:
        return Observed(reachable=False)
    if seen is None:
        return Observed(reachable=False)
    if not seen.head.exists:
        return Observed(reachable=True)
    return Observed(
        reachable=True,
        sha=seen.head.sha or "",
        branch=seen.head.branch,
        skills_ok=compare(expected, seen.found) is None,
    )


def run_deploy(transport: CaptureTransport, target: Target, *, url: str, sha: str) -> str | None:
    """Deploy one target. None when it reports ok, else our reason."""
    result = transport.call(
        ssh_argv(target.remote, deploy_script(target, url=url, sha=sha)),
        timeout=CHECKOUT_TIMEOUT_SECONDS,
    )
    said = _marked(result.stdout, SYNC_MARK)
    if result.code == 0 and said is not None and said.startswith("ok "):
        return None
    if result.code == SSH_UNREACHABLE:
        return "ssh failed during the deploy"
    return said or f"deploy exited {result.code}"


def sync_local(checkout: str, *, url: str, sha: str, environ: Mapping[str, str]) -> str | None:
    """Bring a local bot account's own checkout to sha, as that account (#345).

    The same git steps and refusals as a remote deploy, run in this
    process's account; the caller installs the skills. None when it is at
    sha, else our reason."""
    try:
        result = subprocess.run(
            ["sh", "-c", _CHECKOUT + _SYNCED, "orch-sync", checkout, url, sha],
            env=dict(environ), capture_output=True, text=True, stdin=subprocess.DEVNULL,
            timeout=CHECKOUT_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"checkout sync did not run ({type(exc).__name__})"
    said = _marked(result.stdout, SYNC_MARK)
    if result.returncode == 0 and said == f"ok {sha}":
        return None
    return said or f"checkout sync exited {result.returncode}"


def _why_not_synced(observed: Observed, sha: str) -> str:
    if not observed.reachable:
        return "unreachable after the deploy"
    if observed.sha != sha:
        return f"HEAD is {(observed.sha or 'missing')[:12]} after the deploy"
    if observed.branch != "main":
        return "not on main after the deploy"
    return "pinned skills do not match after the deploy"


def _each(work: Callable[[Target], object], items: Sequence[Target]) -> list:
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=len(items)) as pool:
        return list(pool.map(work, items))


@dataclass(frozen=True)
class Result:
    target: Target
    state: str
    detail: str
    sha: str | None


def run_pass(
    queue: SQLiteJobQueue,
    config: MachinesConfig | None,
    *,
    sha: str,
    url: str,
    pinned: Pinned,
    transport: CaptureTransport,
    now: Callable[[], float] = time.time,
    dry_run: bool = False,
) -> list[Result]:
    found = targets(config)
    seen = dict(zip((t.key for t in found), _each(lambda t: observe(transport, t, pinned), found)))
    results: dict[tuple[str, str], Result] = {}
    deploying: list[Target] = []
    with queue.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        device_sync.ensure(connection)
        device_sync.forget_except(connection, seen, (box_device(config),))
        when = now()
        for target in found:
            observed = seen[target.key]
            action = device_sync.step(observed, sha)
            if action == device_sync.OBSERVE_UNREACHABLE:
                result = Result(target, device_sync.UNREACHABLE, "ssh failed", None)
            elif action == device_sync.KEEP_SYNCED:
                result = Result(target, device_sync.SYNCED, "", observed.sha)
            elif dry_run:
                result = Result(target, device_sync.waiting_state(observed), "", observed.sha)
            else:
                result = Result(target, device_sync.DEPLOYING, "", observed.sha)
                deploying.append(target)
            results[target.key] = result
        for result in results.values():
            _write(connection, result, sha, when)
        if dry_run:
            connection.rollback()
        else:
            connection.commit()
    if not deploying:
        return [results[t.key] for t in found]

    def finish(target: Target) -> Result:
        refused = run_deploy(transport, target, url=url, sha=sha)
        after = observe(transport, target, pinned)
        if refused is None and device_sync.step(after, sha) == device_sync.KEEP_SYNCED:
            return Result(target, device_sync.SYNCED, "", after.sha)
        return Result(target, device_sync.REFUSED, refused or _why_not_synced(after, sha), after.sha)

    for result in _each(finish, deploying):
        results[result.target.key] = result
    with queue.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        when = now()
        for target in deploying:
            _write(connection, results[target.key], sha, when)
        connection.commit()
    return [results[t.key] for t in found]


def _write(connection: sqlite3.Connection, result: Result, sha: str, now: float) -> None:
    target = result.target
    device_sync.upsert(
        connection,
        device=target.device,
        bot=target.bot,
        harness=target.harness,
        checkout=target.checkout,
        target=sha,
        sha=result.sha or None,
        state=result.state,
        detail=result.detail,
        now=now,
        replaces_deploying=True,
    )


def report(
    results: Iterable[Result],
    state_dir: Path,
    *,
    sha: str,
    device: str,
    webhook: Webhook,
    environ: Mapping[str, str],
    now: float,
    post: Poster = send_webhook,
    dry_run: bool = False,
) -> None:
    """Log each change of state and escalate each new refusal once."""
    state_path = state_dir / STATE_NAME
    state = load_state(state_path)
    last = state.get("last") if isinstance(state.get("last"), dict) else {}
    escalated = state.get("escalated") if isinstance(state.get("escalated"), dict) else {}

    def log(line: str) -> None:
        with open(state_dir / LOG_NAME, "a", encoding="utf-8") as out:
            out.write(f"{stamp(now)} device-deploy: {line}\n")

    for result in results:
        name = result.target.label
        note = result.state + (f": {result.detail}" if result.detail else "")
        if dry_run:
            print(f"{name} {result.target.checkout} {note}")
            continue
        if last.get(name) != note:
            log(f"{name} {note} (target {sha[:12]})")
            last[name] = note
        if result.state == device_sync.SYNCED:
            escalated.pop(name, None)
        if result.state != device_sync.REFUSED:
            continue
        key = f"{result.detail}:{sha}"
        if escalated.get(name) == key:
            continue
        action = Action(ESCALATE, f"device deploy {name}", "deploy", result.detail)
        outcome = escalate([action], device=device, now=now, webhook=webhook, environ=environ,
                           post=post)
        log(f"escalate device deploy {name} ({result.detail}) {outcome}")
        escalated[name] = key
    if not dry_run:
        state["last"] = last
        state["escalated"] = escalated
        save_state(state_path, state)


USAGE = """\
orch heal devices [--state DIR] [--db PATH] [--checkout DIR] [--machines PATH]
                  [--orch PATH] [--dry-run]
"""


def main(argv: list[str], *, clock: Callable[[], float] = time.time,
         transport: CaptureTransport | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orch heal devices", usage=USAGE)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--checkout", type=Path, default=ROOT)
    parser.add_argument("--machines", type=Path)
    parser.add_argument("--orch", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    state_dir = args.state or Path(os.environ.get("ORCH_STATE_DIR")
                                   or Path.home() / ".local/state/factory")
    state_dir.mkdir(parents=True, exist_ok=True)
    checkout = args.checkout.resolve()
    machines = health.load_machines(args.machines)
    db = args.db or Path(os.environ.get("ORCH_DB") or state_dir / "orch.db")
    if not targets(machines):
        if db.is_file():
            with SQLiteJobQueue(db, clock=clock).connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                device_sync.forget_except(connection, (), (box_device(machines),))
                connection.commit()
        return 0
    with open(state_dir / LOCK_NAME, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        sha = health.checkout_sha(checkout)
        repo = own_repo(checkout)
        if sha is None or repo is None:
            print("orch heal devices: cannot read the box checkout's HEAD or origin", flush=True)
            return 1
        webhook = _webhook(args.orch if args.orch is not None else checkout / "orch.yml")
        environ = dict(os.environ)
        for name in (*TOKEN_NAMES, *webhook.secret_names):
            os.environ.pop(name, None)
        results = run_pass(
            SQLiteJobQueue(db, clock=clock),
            machines,
            sha=sha,
            url=url_template(None).format(repo=repo),
            pinned=pinned_files(checkout),
            transport=transport if transport is not None else SshTransport(),
            now=clock,
            dry_run=args.dry_run,
        )
        report(results, state_dir, sha=sha, device=health.local_device(machines),
               webhook=webhook, environ=environ, now=clock(), dry_run=args.dry_run)
    return 0
