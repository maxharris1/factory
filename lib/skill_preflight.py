"""Skill preflight on the placed device (issue #179, SF-7).

Before the waiter starts a harness, every file the pinned skills install
must be on the device the job was placed on, with the sha256 that
skills/skills.lock records. The lock decides the set: each pstack file under
skills/<name>/, and each factory router (a skills/factory/<name>/ holding a
SKILL.md), the same set install-skills places. They are looked for under
the bot's skills directory: ~/.claude/skills for claude-bot,
~/.agents/skills for codex-bot, ~/.cursor/skills for cursor-bot. The whole
tree is checked, not a list of named files: the routers reach other skills
by reference (interrogate runs arena; figure-it-out runs show-me-your-work
and architect).

On the box the files are read in place. On a remote device one ssh call
sends the relative paths on stdin and reads back one `<sha256> <path>` line
per file, or `- <path>` for a file that is not there. The first problem in
path order fails the job before launch: `preflight-missing: <path>` or
`preflight-hash: <path>`. Either way the job's detail gets a
`skill_preflight` entry: the device, the skills directory, the lock commit,
every path with the hash found there, and the error when there is one.
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
from lib.pstack_pin import DIR_HARNESS, ROOT, load_pin
from lib.queue import Job, SQLiteJobQueue
from lib.remote import SSH_UNREACHABLE, CaptureTransport, SshTransport, ssh_argv
from lib.wait import OWNING_BOTS, retry_locked

DETAIL_KEY = "skill_preflight"
TIMEOUT_SECONDS = 120.0
ABSENT = "-"

_SKILLS_DIR = {harness: rel for rel, harness in DIR_HARNESS.items()}

# $1: the skills directory under $HOME. Reads one relative path per line on
# stdin and prints `<sha256> <path>`, or `- <path>` when the path is not a
# readable regular file (a symlink to one counts: local installs link).
_SCRIPT = (
    'd="$HOME/$1"; '
    "if command -v sha256sum >/dev/null 2>&1; then s=sha256sum; "
    "elif command -v shasum >/dev/null 2>&1; then s='shasum -a 256'; "
    "else echo 'orch-skills: no sha256sum or shasum' >&2; exit 4; fi; "
    'while IFS= read -r p; do '
    'if [ -f "$d/$p" ] && h=$($s < "$d/$p" 2>/dev/null); then '
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
    commit: str
    found: dict[str, str]
    error: str | None

    def entry(self) -> dict[str, object]:
        entry: dict[str, object] = {
            "device": self.device,
            "root": f"~/{self.skills_dir}",
            "commit": self.commit,
            "files": dict(sorted(self.found.items())),
        }
        if self.error is not None:
            entry["error"] = self.error
        return entry


def pinned_files(root: Path = ROOT) -> tuple[str, dict[str, str]]:
    """The lock commit and {path under the skills dir: sha256} it pins."""
    pin = load_pin(root)
    files = {
        rel.removeprefix("skills/"): digest
        for rel, digest in pin.files
        if rel.startswith("skills/") and rel.count("/") >= 2
    }
    routers = {
        rel.split("/", 1)[0]
        for rel, _ in pin.factory
        if rel.count("/") == 1 and rel.endswith("/SKILL.md")
    }
    for rel, digest in pin.factory:
        if rel.split("/", 1)[0] in routers:
            files[rel] = digest
    for rel in files:
        if any(ord(c) < 32 for c in rel):
            raise ValueError(f"skills.lock path has a control character: {rel!r}")
    return pin.commit, files


def skills_dir(bot: str) -> str:
    try:
        return _SKILLS_DIR[OWNING_BOTS[bot]]
    except KeyError:
        raise PreflightError(f"preflight: no skills directory for {bot}") from None


def local_hashes(home: Path, skills: str, paths: list[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for rel in paths:
        path = home / skills / rel
        try:
            if path.is_file():
                found[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return found


def remote_script(skills: str) -> str:
    return f"exec sh -c {shlex.quote(_SCRIPT)} orch-skills {shlex.quote(skills)}"


def remote_hashes(
    transport: CaptureTransport, device: Device, placement: Placement, skills: str, paths: list[str]
) -> dict[str, str]:
    """One ssh round trip: the hash of each path on the device, absent ones left out."""
    target = remote_target(device, placement.harness, placement.job_id)
    if target is None:
        raise PreflightError(f"preflight: {device.name} has no usable ssh_alias or workdir_root")
    stdin = "".join(f"{rel}\n" for rel in paths).encode("utf-8")
    result = transport.call(ssh_argv(target, remote_script(skills)), timeout=TIMEOUT_SECONDS, input=stdin)
    if result.code == SSH_UNREACHABLE:
        raise PreflightError(f"preflight-unreachable: {device.name} ({target.alias})")
    if result.code != 0:
        raise PreflightError(f"preflight-failed: {device.name} exited {result.code}")
    wanted = set(paths)
    found: dict[str, str] = {}
    for line in result.stdout.splitlines():
        digest, _, rel = line.partition(" ")
        if rel in wanted and digest != ABSENT:
            found[rel] = digest
    return found


def compare(skills: str, pinned: dict[str, str], found: dict[str, str]) -> str | None:
    for rel in sorted(pinned):
        if rel not in found:
            return f"preflight-missing: ~/{skills}/{rel}"
        if found[rel] != pinned[rel]:
            return f"preflight-hash: ~/{skills}/{rel}"
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
        commit, pinned = pinned_files(root)
    except (OSError, ValueError, UnicodeError) as exc:
        raise PreflightError(f"preflight: cannot read skills.lock: {exc}") from exc
    paths = sorted(pinned)
    if device is None or device.local or placement is None:
        name = device.name if device is not None else "box"
        found = local_hashes(home, skills, paths)
    else:
        name = device.name
        found = remote_hashes(transport, device, placement, skills, paths)
    return Report(name, skills, commit, found, compare(skills, pinned, found))


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
