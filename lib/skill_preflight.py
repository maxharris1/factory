"""Skill preflight on the placed device (issue #179, SF-7).

Before the waiter starts a harness, every file skills/skills.lock pins
must be on the device the job was placed on, with the sha256 the lock
records, at both places the harness reads it from:

* the bot's skills directory (~/.claude/skills for claude-bot,
  ~/.agents/skills for codex-bot, ~/.cursor/skills for cursor-bot) holds
  the set install-skills places: each pstack file under skills/<name>/ and
  each factory router and kit skill (a skills/factory/<name>/ or
  skills/cursor-team-kit/<name>/ holding a SKILL.md);
* the device's deployed factory checkout holds the whole pinned tree,
  skills/pstack/, skills/factory/ and skills/cursor-team-kit/. The routers
  send the harness there for pstack's docs/ and agents/ and for
  skills/factory/harness/, which are never installed. The checkout is the
  one the brief names (checkout_root): the device's factory_root
  (machines.yml), or on a box without one the checkout the waiter runs
  from, whose lock it reads (deploy/install.sh points the orch wrapper there).

The whole tree is checked, not a list of named files: the routers reach
other skills by reference (interrogate runs arena; figure-it-out runs
show-me-your-work and architect).

On a remote device the same ssh call first reads the checkout's HEAD
(#297). When it is not the box checkout's HEAD (the commit whose
skills.lock the waiter checks against), or the checkout does not exist, the
start fails with CheckoutLagging (`preflight-lagging: ...` or
`preflight-no-checkout: ...`) before any hash is compared, so the error names
the cause. The waiter requeues such a job without charging a round and
marks the target lagging, so placement skips that device until
`orch heal devices` brings it up to date. The check is skipped when the box
HEAD cannot be read. When ssh cannot reach the device at all the start
fails with DeviceUnreachable (`preflight-unreachable: ...`); the waiter
requeues the job with no attempt charged and backs the device off (#460).
A remote start is also refused while the box checkout itself is behind its
fetched origin/main (`preflight-lagging: box ...`), so
a pending or refused box deploy never lets a remote run the old commit. On
the box the checkout's HEAD is compared with its own fetched origin/main
instead: a box HEAD behind it (self-deploy has not moved it yet) fails the
same way, until it deploys. A box factory_root other than the box checkout
(a per-account checkout, #345) must also sit at the box HEAD, the same rule
as a remote, and is refused while the box checkout lags its origin/main; the
waiter syncs it before each claim.
A box HEAD with commits origin/main lacks is not refused here; self-deploy
escalates it and health flags it.

On the box the files are read in place. When an installed file is missing
or changed, the waiter first runs install-skills from the checkout into its
own home and hashes again, and a failed install is retried. Each bot's
waiter also does this before each claim (#345), so a newly pinned skill
reaches every bot account on the box with no job start. On a
remote device one ssh call sends the paths on stdin (relative to $HOME, or
absolute) and reads back one `<sha256> <path>` line per file, or
`- <path>` for a file that is not there.
The first problem in path order fails the job before launch:
`preflight-missing: <path>` or `preflight-hash: <path>`. Either way the
job's detail gets a `skill_preflight` entry: the device, the skills
directory, the checkout, the lock commit, every path with the hash found
there, and the error when there is one.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

from lib.device_monitor import remote_target
from lib.health import checkout_sha
from lib.device_pool import Placement
from lib.machines_yml import Device
from lib.pstack_models import rules_text
from lib.pstack_pin import (
    DIR_HARNESS, FACTORY_DIR, KIT_DIR, PSTACK_DIR, ROOT, install_skills, load_pin,
)
from lib.queue import Job, SQLiteJobQueue
from lib.remote import SSH_UNREACHABLE, CaptureTransport, RemoteTarget, SshTransport, ssh_argv
from lib.wait import OWNING_BOTS, retry_locked

DETAIL_KEY = "skill_preflight"
TIMEOUT_SECONDS = 120.0
ABSENT = "-"

_SKILLS_DIR = {harness: rel for rel, harness in DIR_HARNESS.items()}

HOME_PREFIX = "~/"
HEAD_MARK = "@head"

# Reads one path per line on stdin, absolute or relative to $HOME, and prints
# `<sha256> <path>`, or `- <path>` when the path is not a readable regular
# file (a symlink to one counts: local installs link).
_SCRIPT = (
    'checkout=$1; if [ ! -d "$checkout" ]; then echo "@head missing"; '
    "elif h=$(git -C \"$checkout\" rev-parse -q --verify 'HEAD^{commit}' 2>/dev/null); then "
    'b=$(git -C "$checkout" symbolic-ref -q --short HEAD 2>/dev/null) || b=-; '
    'echo "@head $h $b"; '
    'else echo "@head - -"; fi; '
    "if command -v sha256sum >/dev/null 2>&1; then s=sha256sum; "
    "elif command -v shasum >/dev/null 2>&1; then s='shasum -a 256'; "
    "else echo 'orch-skills: no sha256sum or shasum' >&2; exit 4; fi; "
    'while IFS= read -r p; do '
    'case $p in /*) f=$p ;; *) f="$HOME/$p" ;; esac; '
    'if [ -f "$f" ] && h=$($s < "$f" 2>/dev/null); then '
    "printf '%s %s\\n' \"${h%% *}\" \"$p\"; "
    "else printf -- '- %s\\n' \"$p\"; fi; "
    "done"
)


class PreflightError(ValueError):
    """The pinned skills are not all on the placed device. One line."""


class CheckoutLagging(PreflightError):
    """The placed device's factory checkout is not the box's commit, or is
    missing. box_behind_origin: the box's own checkout lags its origin/main."""

    def __init__(self, message: str, *, box_behind_origin: bool = False):
        super().__init__(message)
        self.box_behind_origin = box_behind_origin


class DeviceUnreachable(PreflightError):
    """ssh could not reach the placed device, so nothing ran there."""

    def __init__(self, device: str, alias: str):
        super().__init__(f"preflight-unreachable: {device} ({alias})")
        self.device = device


@dataclass(frozen=True)
class Report:
    device: str
    skills_dir: str
    checkout: str
    commit: str
    found: dict[str, str]
    error: str | None

    def entry(self) -> dict[str, object]:
        entry: dict[str, object] = {
            "device": self.device,
            "root": f"{HOME_PREFIX}{self.skills_dir}",
            "checkout": self.checkout,
            "commit": self.commit,
            "files": dict(sorted(self.found.items())),
        }
        if self.error is not None:
            entry["error"] = self.error
        return entry


@dataclass(frozen=True)
class Pinned:
    commit: str
    installed: dict[str, str]
    checkout: dict[str, str]


def pinned_files(root: Path = ROOT) -> Pinned:
    """The lock commit, {path under the skills dir: sha256} and {path under the checkout: sha256}."""
    pin = load_pin(root)
    sections = (
        (PSTACK_DIR, pin.files),
        (FACTORY_DIR, pin.factory),
        (KIT_DIR, pin.kit),
    )
    checkout = {f"{base.as_posix()}/{rel}": digest for base, rows in sections for rel, digest in rows}
    files = {
        rel.removeprefix("skills/"): digest
        for rel, digest in pin.files
        if rel.startswith("skills/") and rel.count("/") >= 2
    }
    for rows in (pin.factory, pin.kit):
        skills = {rel.split("/", 1)[0] for rel, _ in rows if rel.count("/") == 1 and rel.endswith("/SKILL.md")}
        files |= {rel: digest for rel, digest in rows if rel.split("/", 1)[0] in skills}
    for rel in checkout:
        if any(ord(c) < 32 for c in rel):
            raise ValueError(f"skills.lock path has a control character: {rel!r}")
    return Pinned(pin.commit, files, checkout)


def skills_dir(bot: str) -> str:
    try:
        return _SKILLS_DIR[OWNING_BOTS[bot]]
    except KeyError:
        raise PreflightError(f"preflight: no skills directory for {bot}") from None


def wanted(skills: str, checkout: str, pinned: Pinned) -> dict[str, str]:
    """{path on the device: sha256}, ~/-prefixed under the home, else absolute."""
    paths = {f"{HOME_PREFIX}{skills}/{rel}": digest for rel, digest in pinned.installed.items()}
    paths |= {f"{checkout}/{rel}": digest for rel, digest in pinned.checkout.items()}
    return paths


def checkout_root(device: Device | None, harness: str, root: Path) -> str | None:
    """The placed device's deployed factory checkout: its factory_root, else on the box root."""
    configured = device.factory_root_for(harness) if device is not None else None
    if configured is None and (device is None or device.local):
        return str(root.resolve())
    return configured


def _wire(path: str) -> str:
    return path.removeprefix(HOME_PREFIX)


def local_hashes(home: Path, paths: list[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for key in paths:
        path = home / _wire(key) if key.startswith(HOME_PREFIX) else Path(key)
        try:
            if path.is_file():
                found[key] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return found


def remote_script(checkout: str) -> str:
    return f"exec sh -c {shlex.quote(_SCRIPT)} orch-skills {shlex.quote(checkout)}"


@dataclass(frozen=True)
class Head:
    """The checkout's HEAD as the device reported it; sha None when it has none."""

    exists: bool
    sha: str | None
    branch: str | None


@dataclass(frozen=True)
class Seen:
    head: Head
    found: dict[str, str]


def remote_seen(
    transport: CaptureTransport, target: RemoteTarget, checkout: str, paths: list[str]
) -> Seen | None:
    """One ssh round trip: the checkout's HEAD and the hash of each path. None when unreachable."""
    by_wire = {_wire(key): key for key in paths}
    stdin = "".join(f"{rel}\n" for rel in by_wire).encode("utf-8")
    result = transport.call(
        ssh_argv(target, remote_script(checkout)), timeout=TIMEOUT_SECONDS, input=stdin
    )
    if result.code == SSH_UNREACHABLE:
        return None
    if result.code != 0:
        raise PreflightError(f"preflight-failed: {target.device} exited {result.code}")
    head = Head(exists=False, sha=None, branch=None)
    found: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if line.startswith(f"{HEAD_MARK} "):
            fields = line.split()[1:]
            if fields == ["missing"]:
                continue
            sha = fields[0] if fields and fields[0] != ABSENT else None
            branch = fields[1] if len(fields) > 1 and fields[1] != ABSENT else None
            head = Head(exists=True, sha=sha, branch=branch)
            continue
        digest, _, rel = line.partition(" ")
        if rel in by_wire and digest != ABSENT:
            found[by_wire[rel]] = digest
    return Seen(head, found)


def remote_hashes(
    transport: CaptureTransport, device: Device, placement: Placement, paths: list[str],
    checkout: str,
) -> Seen:
    target = remote_target(device, placement.harness, placement.job_id)
    if target is None:
        raise PreflightError(f"preflight: {device.name} has no usable ssh_alias or workdir_root")
    seen = remote_seen(transport, target, checkout, paths)
    if seen is None:
        raise DeviceUnreachable(device.name, target.alias)
    return seen


def lag(device: str, bot: str, checkout: str, head: Head, box: str | None) -> str | None:
    """Why the checkout cannot run a job for the box's commit, or None."""
    if box is None:
        return None
    if not head.exists:
        return f"preflight-no-checkout: {device} {bot} {checkout}"
    if head.sha != box:
        at = head.sha[:12] if head.sha else "no commit"
        return f"preflight-lagging: {device} {bot} {checkout} at {at}, box at {box[:12]}"
    return None


def box_behind_origin(device: str, bot: str, checkout: str) -> str | None:
    head = checkout_sha(Path(checkout))
    upstream = checkout_sha(Path(checkout), "refs/remotes/origin/main")
    if head is None or upstream is None or head == upstream:
        return None
    try:
        behind = subprocess.run(
            ["git", "-C", checkout, "merge-base", "--is-ancestor", head, upstream],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=TIMEOUT_SECONDS, check=False,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not behind:
        return None
    return (f"preflight-lagging: {device} {bot} {checkout} at {head[:12]}, "
            f"origin/main at {upstream[:12]}")


def refuse_configured_checkout_behind_box(name: str, bot: str, checkout: str, root: Path) -> None:
    if Path(checkout).resolve() == root.resolve():
        return
    lagging = box_behind_origin("box", bot, str(root.resolve()))
    if lagging is not None:
        raise CheckoutLagging(lagging, box_behind_origin=True)
    head = Head(exists=Path(checkout).is_dir(), sha=checkout_sha(Path(checkout)), branch=None)
    lagging = lag(name, bot, checkout, head, checkout_sha(root))
    if lagging is not None:
        raise CheckoutLagging(lagging)


def home_install_stale(expected: dict[str, str], found: dict[str, str]) -> bool:
    return any(found.get(path) != digest for path, digest in expected.items()
               if path.startswith(HOME_PREFIX))


def install_into_home(checkout: Path, home: Path) -> list[str]:
    try:
        rules: str | None = rules_text(checkout)
    except (OSError, ValueError):
        rules = None
    return install_skills(checkout, home, "local", rules=rules)


def compare(pinned: dict[str, str], found: dict[str, str]) -> str | None:
    for path in sorted(pinned):
        if path not in found:
            return f"preflight-missing: {path}"
        if found[path] != pinned[path]:
            return f"preflight-hash: {path}"
    return None


def check(
    *,
    bot: str,
    placement: Placement | None,
    device: Device | None,
    transport: CaptureTransport,
    home: Path,
    checkout: str | None,
    root: Path = ROOT,
) -> Report:
    skills = skills_dir(bot)
    try:
        pinned = pinned_files(root)
    except (OSError, ValueError, UnicodeError) as exc:
        raise PreflightError(f"preflight: cannot read skills.lock: {exc}") from exc
    if checkout is None:
        name = device.name if device is not None else "box"
        raise PreflightError(f"preflight: {name} has no factory_root for {OWNING_BOTS[bot]}")
    expected = wanted(skills, checkout, pinned)
    paths = sorted(expected)
    install_errors: list[str] = []
    if device is None or device.local or placement is None:
        name = device.name if device is not None else "box"
        lagging = box_behind_origin(name, bot, checkout)
        if lagging is not None:
            raise CheckoutLagging(lagging, box_behind_origin=True)
        refuse_configured_checkout_behind_box(name, bot, checkout, root)
        found = local_hashes(home, paths)
        if home_install_stale(expected, found):
            install_errors = install_into_home(Path(checkout), home)
            found = local_hashes(home, paths)
    else:
        name = device.name
        lagging = box_behind_origin("box", bot, str(root.resolve()))
        if lagging is not None:
            raise CheckoutLagging(lagging, box_behind_origin=True)
        seen = remote_hashes(transport, device, placement, paths, checkout)
        lagging = lag(name, bot, checkout, seen.head, checkout_sha(root))
        if lagging is not None:
            raise CheckoutLagging(lagging)
        found = seen.found
    error = compare(expected, found)
    if error is not None and install_errors:
        error = f"{error} (install-skills: {install_errors[0]})"
    return Report(name, skills, checkout, pinned.commit, found, error)


def record(queue: SQLiteJobQueue, job: Job, entry: dict[str, object]) -> None:
    """Merge entry into the running job's detail under DETAIL_KEY."""

    def write() -> None:
        with queue.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT detail FROM jobs WHERE id = ? AND state = 'running' AND recipient = ?",
                (job.id, job.request.recipient),
            ).fetchone()
            if row is None:
                connection.commit()
                return
            try:
                detail = json.loads(row["detail"]) if row["detail"] else {}
            except ValueError:
                detail = None
            if not isinstance(detail, dict):
                connection.commit()
                return
            detail[DETAIL_KEY] = entry
            connection.execute(
                "UPDATE jobs SET detail = ? WHERE id = ? AND state = 'running' AND recipient = ?",
                (json.dumps(detail, separators=(",", ":"), sort_keys=True), job.id, job.request.recipient),
            )
            connection.commit()

    try:
        retry_locked(write)
    except sqlite3.Error as exc:
        raise PreflightError(f"preflight: cannot record the result: {exc}") from exc


def preflight(
    queue: SQLiteJobQueue,
    job: Job,
    *,
    bot: str,
    placement: Placement | None,
    device: Device | None,
    checkout: str | None,
    transport: CaptureTransport | None = None,
    home: Path | None = None,
    root: Path | None = None,
) -> Report:
    """Check the pinned skills on the job's device, under the checkout the brief names,
    and record the result. Raises PreflightError."""
    try:
        report = check(
            bot=bot,
            placement=placement,
            device=device,
            transport=transport if transport is not None else SshTransport(),
            home=home if home is not None else Path.home(),
            checkout=checkout,
            root=root if root is not None else ROOT,
        )
    except PreflightError as exc:
        record(queue, job, {"device": device.name if device is not None else "box", "error": str(exc)})
        raise
    record(queue, job, report.entry())
    if report.error is not None:
        raise PreflightError(report.error)
    return report
