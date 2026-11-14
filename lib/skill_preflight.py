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
  skills/factory/harness/, which are never installed. On a remote device the checkout is its factory_root
  (machines.yml); on the box it is the checkout the waiter runs from, whose
  lock it reads (deploy/install.sh points the orch wrapper there).

The whole tree is checked, not a list of named files: the routers reach
other skills by reference (interrogate runs arena; figure-it-out runs
show-me-your-work and architect).

On the box the files are read in place. On a remote device one ssh call
sends the paths on stdin (relative to $HOME, or absolute) and reads back one
`<sha256> <path>` line per file, or `- <path>` for a file that is not there.
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
from dataclasses import dataclass
from pathlib import Path

from lib.device_monitor import remote_target
from lib.device_pool import Placement
from lib.machines_yml import Device
from lib.pstack_pin import DIR_HARNESS, FACTORY_DIR, KIT_DIR, PSTACK_DIR, ROOT, load_pin
from lib.queue import Job, SQLiteJobQueue
from lib.remote import SSH_UNREACHABLE, CaptureTransport, SshTransport, ssh_argv
from lib.wait import OWNING_BOTS, retry_locked

DETAIL_KEY = "skill_preflight"
TIMEOUT_SECONDS = 120.0
ABSENT = "-"

_SKILLS_DIR = {harness: rel for rel, harness in DIR_HARNESS.items()}

HOME_PREFIX = "~/"

# Reads one path per line on stdin, absolute or relative to $HOME, and prints
# `<sha256> <path>`, or `- <path>` when the path is not a readable regular
# file (a symlink to one counts: local installs link).
_SCRIPT = (
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


def checkout_root(device: Device | None, placement: Placement | None, harness: str, root: Path) -> str:
    if device is None or device.local or placement is None:
        return str(root.resolve())
    configured = device.factory_root_for(harness)
    if configured is None:
        raise PreflightError(f"preflight: {device.name} has no factory_root for {harness}")
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


def remote_script() -> str:
    return f"exec sh -c {shlex.quote(_SCRIPT)} orch-skills"


def remote_hashes(
    transport: CaptureTransport, device: Device, placement: Placement, paths: list[str]
) -> dict[str, str]:
    """One ssh round trip: the hash of each path on the device, absent ones left out."""
    target = remote_target(device, placement.harness, placement.job_id)
    if target is None:
        raise PreflightError(f"preflight: {device.name} has no usable ssh_alias or workdir_root")
    by_wire = {_wire(key): key for key in paths}
    stdin = "".join(f"{rel}\n" for rel in by_wire).encode("utf-8")
    result = transport.call(ssh_argv(target, remote_script()), timeout=TIMEOUT_SECONDS, input=stdin)
    if result.code == SSH_UNREACHABLE:
        raise PreflightError(f"preflight-unreachable: {device.name} ({target.alias})")
    if result.code != 0:
        raise PreflightError(f"preflight-failed: {device.name} exited {result.code}")
    found: dict[str, str] = {}
    for line in result.stdout.splitlines():
        digest, _, rel = line.partition(" ")
        if rel in by_wire and digest != ABSENT:
            found[by_wire[rel]] = digest
    return found


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
    root: Path = ROOT,
) -> Report:
    skills = skills_dir(bot)
    try:
        pinned = pinned_files(root)
    except (OSError, ValueError, UnicodeError) as exc:
        raise PreflightError(f"preflight: cannot read skills.lock: {exc}") from exc
    checkout = checkout_root(device, placement, OWNING_BOTS[bot], root)
    expected = wanted(skills, checkout, pinned)
    paths = sorted(expected)
    if device is None or device.local or placement is None:
        name = device.name if device is not None else "box"
        found = local_hashes(home, paths)
    else:
        name = device.name
        found = remote_hashes(transport, device, placement, paths)
    return Report(name, skills, checkout, pinned.commit, found, compare(expected, found))


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
    transport: CaptureTransport | None = None,
    home: Path | None = None,
    root: Path | None = None,
) -> Report:
    """Check the pinned skills on the job's device and record the result. Raises PreflightError."""
    try:
        report = check(
            bot=bot,
            placement=placement,
            device=device,
            transport=transport if transport is not None else SshTransport(),
            home=home if home is not None else Path.home(),
            root=root if root is not None else ROOT,
        )
    except PreflightError as exc:
        record(queue, job, {"device": device.name if device is not None else "box", "error": str(exc)})
        raise
    record(queue, job, report.entry())
    if report.error is not None:
        raise PreflightError(report.error)
    return report
