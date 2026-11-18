"""`orch settings apply` (#777): apply device settings asked for in Mission Control.

The :8090 dashboard server writes each request Max makes on the Settings page
as one JSON file, `<id>.json`, into its spool directory
(deploy/factory-dashboard-http.py). It never touches machines.yml. This
command, run by `deploy/self-heal.sh once` as the checkout owner, reads the
spool named by orch.yml `dashboard.settings` and applies each request it has
not seen:

* The spool is on `dashboard.push.host` when the dashboard push is ssh, and
  is read there with one BatchMode ssh call using the push's ssh_config;
  otherwise it is a local directory. A file is trusted only if it is a
  regular file owned by `dashboard.settings.spool_owner` and neither it nor
  the spool directory is group- or world-writable.
* A request is refused, leaving machines.yml byte-identical and writing no
  backup, unless its actor is `dashboard.settings.approver` (not a bot), the
  device exists, the field is in FIELDS with a valid value, machines.local.yml
  does not set that field for that device, `expect` equals the current value,
  and it does not disable a device that holds a running job (drain it first).
  That check, the backup and the replace below run inside one BEGIN IMMEDIATE
  on orch.db, the write lock every DevicePool.claim takes, and a claim reads
  enabled and drain from the file under it: no job lands between the check
  and the write. The new text is a line edit inside that device's block, so
  comments and other devices stay byte-for-byte; both texts are parsed with
  load_machines_yml and must differ in exactly that one field.
* machines.yml is copied to `machines.yml.bak-settings-<UTC stamp>` beside
  it, then replaced atomically. The poller and then each waiter this account
  owns and has not held stopped are restarted through their service scripts
  (lib/heal.py service_argv) with heal's scrubbed environment, after the lock
  is released; services another account owns are listed as pending, and the
  result is then `pending`, not `applied`. A failed restart restores the
  backup under the same lock (not when that would disable a device now
  holding a job), restarts them again and records `failed`.
* Every outcome is one AuditEntry line in `<state>/settings-audit.jsonl`. An
  id already there is never applied again, so a pass is safe to repeat.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import fcntl
import json
import os
import pwd
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from lib import dashboard_push, heal
from lib.config_overlay import overlay_path, read_overlay
from lib.dashboard_config import SettingsConfig, is_bot_login
from lib.device_pool import slots_in_use
from lib.load import is_strict_int
from lib.machines_yml import Device, MachinesConfig, load_machines_yml
from lib.orch_yml import load_orch_yml
from lib.wait import OWNING_BOTS

ROOT = Path(__file__).resolve().parent.parent
AUDIT_NAME = "settings-audit.jsonl"
LOCK_NAME = "settings-apply.lock"
SLOT_MAX_LIMIT = 64
MAX_REQUEST_BYTES = 4096
SSH_TIMEOUT_SECONDS = 20
CLIP = 80

APPLIED = "applied"
PENDING = "pending"
REFUSED = "refused"
FAILED = "failed"

REQUEST_NAME = re.compile(r"^([A-Za-z0-9_-]{16,64})\.json$")
REQUESTED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
REQUEST_KEYS = frozenset({"id", "actor", "requested_at", "device", "field", "expect", "value"})


def _is_bool(value: object) -> bool:
    return isinstance(value, bool)


def _is_slot_max(value: object) -> bool:
    return is_strict_int(value) and 1 <= value <= SLOT_MAX_LIMIT


FIELDS: Mapping[str, Callable[[object], bool]] = MappingProxyType(
    {"enabled": _is_bool, "drain": _is_bool, "slot_max": _is_slot_max}
)


@dataclass(frozen=True)
class SettingsRequest:
    id: str
    actor: str
    requested_at: str
    device: str
    field: str
    expect: object
    value: object


class BadRequest(ValueError):
    pass


def parse_request(body: bytes, stem: str) -> SettingsRequest:
    """The request in a spool file named `<stem>.json`; values are checked
    against FIELDS later, so a bad value is refused with its device named."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BadRequest("not-json") from None
    if not isinstance(data, dict) or set(data) != REQUEST_KEYS:
        raise BadRequest("bad-keys")
    for key in ("id", "actor", "requested_at", "device", "field"):
        if not isinstance(data[key], str):
            raise BadRequest(f"bad-{key}")
    if data["id"] != stem:
        raise BadRequest("id-not-file-name")
    if REQUESTED_AT.fullmatch(data["requested_at"]) is None:
        raise BadRequest("bad-requested_at")
    return SettingsRequest(**data)


@dataclass(frozen=True)
class AuditEntry:
    id: str
    actor: str | None
    requested_at: str | None
    applied_at: str
    device: str | None
    field: str | None
    previous: object
    new: object
    result: str
    error: str | None = None
    backup: str | None = None
    restarted: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()

    def line(self) -> str:
        data = asdict(self)
        data["restarted"] = list(self.restarted)
        data["pending"] = list(self.pending)
        return json.dumps(data, separators=(",", ":"))


def _clip(value: object) -> object:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        return value[:CLIP]
    return None


def audited_ids(path: Path) -> set[str]:
    ids: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ids
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            ids.add(entry["id"])
    return ids


def append_audit(path: Path, entry: AuditEntry) -> None:
    with open(path, "a", encoding="utf-8") as out:
        out.write(entry.line() + "\n")
        out.flush()
        os.fsync(out.fileno())




class SpoolError(RuntimeError):
    pass


@dataclass(frozen=True)
class SpoolFile:
    name: str
    owner: str
    mode: int
    regular: bool
    body: bytes = b""


@dataclass(frozen=True)
class SpoolListing:
    directory: bool
    mode: int
    files: tuple[SpoolFile, ...]


class Spool(Protocol):
    def read(self) -> SpoolListing: ...


def untrusted(listing: SpoolListing, item: SpoolFile, owner: str) -> str | None:
    """Why a spool file may not be trusted, or None."""
    if not listing.directory:
        return "spool-not-a-directory"
    if listing.mode & 0o022:
        return "spool-writable-by-others"
    if not item.regular:
        return "not-a-regular-file"
    if item.owner != owner:
        return "untrusted-owner"
    if item.mode & 0o022:
        return "untrusted-mode"
    if len(item.body) > MAX_REQUEST_BYTES:
        return "too-large"
    return None


def _user(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


class LocalSpool:
    def __init__(self, path: Path):
        self.path = Path(path)

    def read(self) -> SpoolListing:
        try:
            info = os.lstat(self.path)
        except OSError as exc:
            raise SpoolError(f"spool unreadable: {type(exc).__name__}") from None
        if not stat.S_ISDIR(info.st_mode):
            return SpoolListing(False, stat.S_IMODE(info.st_mode), ())
        files = []
        for name in sorted(os.listdir(self.path)):
            if REQUEST_NAME.fullmatch(name) is not None:
                item = self._file(name)
                if item is not None:
                    files.append(item)
        return SpoolListing(True, stat.S_IMODE(info.st_mode), tuple(files))

    def _file(self, name: str) -> SpoolFile | None:
        path = self.path / name
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            try:
                info = os.lstat(path)
            except OSError:
                return None
            return SpoolFile(name, _user(info.st_uid), stat.S_IMODE(info.st_mode), False)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            regular = stat.S_ISREG(info.st_mode)
            body = handle.read(MAX_REQUEST_BYTES + 1) if regular else b""
        return SpoolFile(name, _user(info.st_uid), stat.S_IMODE(info.st_mode), regular, body)


# Lists the spool on the push host in one call: a `D <mode> <kind>` line for
# the directory, then `F <owner> <mode> <name> <base64 body>` per regular
# request file and `X <owner> <mode> <name>` for anything else.
REMOTE_LIST = """\
d={path}
stat -c 'D %a %F' -- "$d" || exit 3
if [ ! -d "$d" ] || [ -L "$d" ]; then exit 0; fi
cd -- "$d" || exit 3
for f in *.json; do
  case "$f" in *[!A-Za-z0-9_.-]*) continue ;; esac
  if [ -f "$f" ] && [ ! -L "$f" ]; then
    s=$(stat -c 'F %U %a %n' -- "$f") || continue
    b=$(head -c {limit} -- "$f" | base64 | tr -d '\\n') || continue
    printf '%s %s\\n' "$s" "$b"
  else
    stat -c 'X %U %a %n' -- "$f" 2>/dev/null
  fi
done
"""


class SshSpool:
    """The spool on the dashboard push host, read with the push's SSH login."""

    def __init__(
        self,
        host: str,
        path: str,
        *,
        ssh_config: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
    ):
        self.host = host
        self.path = path
        self.ssh_config = ssh_config
        self.runner = runner

    def read(self) -> SpoolListing:
        config = ["-F", self.ssh_config] if self.ssh_config else []
        script = REMOTE_LIST.format(path=f"'{self.path}'", limit=MAX_REQUEST_BYTES + 1)
        argv = ["ssh", *config, *dashboard_push.SSH_OPTIONS, "--", self.host, script]
        runner = self.runner or dashboard_push.run_in_own_group
        redact = dashboard_push.redact
        try:
            result = runner(argv, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=SSH_TIMEOUT_SECONDS, check=False)
        except subprocess.TimeoutExpired:
            raise SpoolError(f"ssh to {self.host} timed out") from None
        except FileNotFoundError:
            raise SpoolError("ssh is not installed") from None
        if result.returncode != 0:
            detail = next((line.strip() for line in (result.stderr or "").splitlines()
                           if line.strip()), "")
            raise SpoolError(f"ssh to {self.host} failed (exit {result.returncode})"
                             + (f": {redact(detail)[:200]}" if detail else ""))
        return parse_listing(result.stdout or "")


def parse_listing(text: str) -> SpoolListing:
    lines = text.splitlines()
    if not lines or not lines[0].startswith("D "):
        raise SpoolError("spool listing unreadable")
    try:
        _, mode, kind = lines[0].split(" ", 2)
        directory, dir_mode = kind == "directory", int(mode, 8)
        files = []
        for line in lines[1:]:
            parts = line.split(" ")
            if REQUEST_NAME.fullmatch(parts[3]) is None:
                continue
            if parts[0] == "F" and len(parts) in (4, 5):
                body = base64.b64decode(parts[4] if len(parts) == 5 else "", validate=True)
                files.append(SpoolFile(parts[3], parts[1], int(parts[2], 8), True, body))
            elif parts[0] == "X" and len(parts) == 4:
                files.append(SpoolFile(parts[3], parts[1], int(parts[2], 8), False))
            else:
                raise ValueError(line[:20])
    except (ValueError, IndexError, binascii.Error):
        raise SpoolError("spool listing unreadable") from None
    return SpoolListing(directory, dir_mode, tuple(files))




_DEVICES = re.compile(r"^devices:[ \t]*(#.*)?$")
_ITEM = re.compile(r"^(?P<dash>[ ]*-[ ]+)(?P<rest>\S.*)$")


def _scalar(text: str) -> str:
    value = text.split(" #", 1)[0].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _blocks(lines: Sequence[str]) -> list[tuple[int, int, int]]:
    """(first line, last content line, key indent) of each devices item."""
    top = next((i for i, line in enumerate(lines) if _DEVICES.match(line.rstrip("\r\n"))), None)
    if top is None:
        return []
    blocks: list[list[int]] = []
    dash = -1
    for index in range(top + 1, len(lines)):
        raw = lines[index].rstrip("\r\n")
        content = raw.lstrip(" ")
        if not content or content.startswith("#"):
            continue
        indent = len(raw) - len(content)
        item = _ITEM.match(raw)
        if item is not None and (not blocks or indent == dash):
            dash = indent
            blocks.append([index, index, len(item["dash"])])
        elif indent == 0 and not content.startswith("-"):
            break
        elif blocks and indent > dash:
            blocks[-1][1] = index
        else:
            return []
    return [(start, end, keys) for start, end, keys in blocks]


def _key_lines(lines: Sequence[str], block: tuple[int, int, int], key: str) -> list[tuple[int, int]]:
    """(line, column where `key:` starts) for each `key:` at the block's key level."""
    start, end, keys = block
    found = []
    for index in range(start, end + 1):
        raw = lines[index].rstrip("\r\n")
        column = keys if index == start or (len(raw) - len(raw.lstrip(" "))) == keys else None
        if column is not None and re.match(rf"{re.escape(key)}:(\s|$)", raw[column:]):
            found.append((index, column))
    return found


def _render(value: object) -> str:
    return ("true" if value else "false") if isinstance(value, bool) else str(value)


def edit_device_field(text: str, device: str, key: str, value: object) -> str | None:
    """text with `key: value` set in the device's block, or None when the
    block cannot be found or edited by line."""
    lines = text.splitlines(keepends=True)
    for block in _blocks(lines):
        names = _key_lines(lines, block, "name")
        if len(names) != 1:
            continue
        index, column = names[0]
        if _scalar(lines[index].rstrip("\r\n")[column + len("name:"):]) != device:
            continue
        found = _key_lines(lines, block, key)
        if len(found) > 1:
            return None
        if found:
            line, column = found[0]
            raw = lines[line]
            eol = raw[len(raw.rstrip("\r\n")):]
            body = raw.rstrip("\r\n")
            match = re.match(r"(?P<gap>[ \t]*)(?P<value>[^#]*?)(?P<trail>[ \t]+#.*)?$",
                             body[column + len(key) + 1:])
            if match is None or not match["value"]:
                return None
            lines[line] = (body[:column] + f"{key}: {_render(value)}"
                           + (match["trail"] or "") + eol)
        else:
            if not lines[index].endswith("\n"):
                lines[index] += "\n"
            eol = "\r\n" if lines[index].endswith("\r\n") else "\n"
            lines.insert(index + 1, " " * block[2] + f"{key}: {_render(value)}{eol}")
        return "".join(lines)
    return None


def _changes(old: MachinesConfig, new: MachinesConfig) -> list[tuple[str, str, object]] | None:
    if old.orch_host != new.orch_host or [d.name for d in old.devices] != [
        d.name for d in new.devices
    ]:
        return None
    return [
        (before.name, item.name, getattr(after, item.name))
        for before, after in zip(old.devices, new.devices)
        for item in fields(Device)
        if getattr(before, item.name) != getattr(after, item.name)
    ]


def _overlay_sets(machines: Path, device: str, key: str) -> bool:
    data, _ = read_overlay(machines)
    entries = (data or {}).get("devices") or ()
    return any(
        isinstance(entry, dict) and entry.get("name") == device and key in entry
        for entry in entries
    )


def _write(path: Path, data: bytes, mode: int, *, exclusive: bool = False) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags, mode)
    try:
        os.write(fd, data)
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)


def _replace(path: Path, data: bytes, mode: int) -> None:
    temp = path.with_name(f".{path.name}.settings-{os.getpid()}.tmp")
    _write(temp, data, mode)
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)




class Refused(Exception):
    def __init__(self, error: str, previous: object = None):
        super().__init__(error)
        self.error = error
        self.previous = previous


def service_name(service: str) -> str:
    return service.replace(":", "-")


def services(config_dir: Path, state_dir: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(heal service ids to restart, poller first; names of those this
    account does not own). A service held stopped is neither: a restart
    would override the operator's stop, and it reads the file when started."""
    facts = heal.facts_for("", config_dir, state_dir, None, 0.0)
    every = [heal.POLLER_SERVICE, *(f"waiter:{bot}" for bot in OWNING_BOTS)]
    restart = tuple(s for s in every if s in facts.owned and s not in facts.held)
    pending = tuple(service_name(s) for s in every if s not in facts.owned)
    return restart, pending


Held = Callable[[MachinesConfig], Mapping[str, int]]
ClaimLock = Callable[[], AbstractContextManager[Held]]


def claim_lock(db: Path) -> ClaimLock:
    """orch.db's write lock, the one every DevicePool.claim takes, yielding
    the slots held per device under it. Nothing is written."""

    @contextmanager
    def hold() -> Iterator[Held]:
        if not db.is_file():
            yield lambda config: {}
            return
        connection = sqlite3.connect(db, timeout=30, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield lambda config: slots_in_use(connection, config)
        finally:
            connection.close()

    return hold


def scrubbed_env(environ: Mapping[str, str], orch_config: object) -> dict[str, str]:
    """heal's service environment: this one minus the webhook and config secrets."""
    grok = getattr(orch_config, "grok", None)
    secrets = tuple(getattr(orch_config, "secret_env_names", ()))
    webhook = (heal.Webhook(config_secret_envs=secrets) if grok is None else
               heal.Webhook(grok.webhook_url_env, grok.webhook_auth_env,
                            grok.webhook_key_env, secrets))
    return {k: v for k, v in environ.items() if k not in webhook.secret_names}


@dataclass(frozen=True)
class Applier:
    machines: Path
    state_dir: Path
    deploy: Path
    approver: str
    environ: Mapping[str, str]
    restart: tuple[str, ...]
    pending: tuple[str, ...]
    claims: ClaimLock
    run: heal.Runner = heal.run_service
    clock: Callable[[], float] = time.time

    @property
    def audit_path(self) -> Path:
        return self.state_dir / AUDIT_NAME

    def apply(self, request: SettingsRequest) -> AuditEntry:
        try:
            with self.claims() as held:
                old_bytes, new_text, previous = self._plan(request, held)
                mode = stat.S_IMODE(os.stat(self.machines).st_mode)
                backup = self._backup(old_bytes, mode)
                _replace(self.machines, new_text.encode("utf-8"), mode)
        except Refused as refusal:
            return self._entry(request, REFUSED, refusal.previous, refusal.error)
        except sqlite3.Error:
            return self._entry(request, REFUSED, None, "cannot read running jobs")
        return self._restart_all(request, old_bytes, mode, backup, previous)

    def _entry(self, request: SettingsRequest, result: str, previous: object,
               error: str | None = None, **extra: object) -> AuditEntry:
        return AuditEntry(
            id=request.id, actor=_clip(request.actor), requested_at=request.requested_at,
            applied_at=heal.stamp(self.clock()), device=_clip(request.device),
            field=_clip(request.field), previous=_clip(previous), new=_clip(request.value),
            result=result, error=error, **extra,
        )

    def _plan(self, request: SettingsRequest, held: Held) -> tuple[bytes, str, object]:
        if request.actor.casefold() != self.approver.casefold() or is_bot_login(request.actor):
            raise Refused("not-approver")
        old_bytes = self.machines.read_bytes()
        old, _ = load_machines_yml(self.machines)
        if old is None:
            raise Refused("machines.yml is invalid")
        device = old.device(request.device)
        if device is None:
            raise Refused("unknown-device")
        check = FIELDS.get(request.field)
        if check is None:
            raise Refused("bad-field")
        previous = getattr(device, request.field)
        if not check(request.value) or not check(request.expect):
            raise Refused("bad-value", previous)
        if _overlay_sets(self.machines, request.device, request.field):
            raise Refused(f"{request.field} is set in machines.local.yml", previous)
        if request.expect != previous:
            raise Refused("changed", previous)
        if request.value == previous:
            raise Refused("no-change", previous)
        if request.field == "enabled" and request.value is False:
            try:
                running = held(old).get(request.device, 0)
            except sqlite3.Error:
                raise Refused("cannot read running jobs", previous) from None
            if running:
                raise Refused(f"{request.device} holds {running} running job(s); drain it first",
                              previous)
        new_text = edit_device_field(old_bytes.decode("utf-8"), request.device,
                                     request.field, request.value)
        if new_text is None:
            raise Refused("cannot edit the device's block", previous)
        new, findings = self._parse(new_text)
        if new is None:
            error = next((f.message for f in findings if f.severity == "error"), "invalid")
            raise Refused(f"invalid after the edit: {error}"[:200], previous)
        if _changes(old, new) != [(request.device, request.field, request.value)]:
            raise Refused("the edit changed more than that field", previous)
        return old_bytes, new_text, previous

    def _parse(self, text: str):
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / self.machines.name
            probe.write_text(text, encoding="utf-8")
            local = overlay_path(self.machines)
            if local.is_file():
                shutil.copyfile(local, overlay_path(probe))
            return load_machines_yml(probe)

    def _restart_all(self, request: SettingsRequest, old_bytes: bytes, mode: int,
                     backup: Path, previous: object) -> AuditEntry:
        restarted, failure = self._restart(stop_on_failure=True)
        if failure is None:
            return self._entry(request, PENDING if self.pending else APPLIED, previous,
                               backup=backup.name, restarted=restarted, pending=self.pending)
        kept = self._restore(request, old_bytes, mode)
        again, refailure = self._restart(stop_on_failure=False)
        error = (f"{failure}; " + (kept or f"restored {backup.name}")
                 + (f"; then {refailure}" if refailure else ""))
        return self._entry(request, FAILED, previous, error,
                           backup=backup.name,
                           restarted=tuple(dict.fromkeys(restarted + again)),
                           pending=self.pending)

    def _restore(self, request: SettingsRequest, old_bytes: bytes, mode: int) -> str | None:
        """Put the old file back under the claim lock. Undoing an enable is a
        disable, so it is skipped (the reason returned) while the device holds a job."""
        try:
            with self.claims() as held:
                if request.field == "enabled" and request.value is True:
                    now, _ = load_machines_yml(self.machines)
                    running = 0 if now is None else held(now).get(request.device, 0)
                    if running:
                        return f"not restored: {request.device} holds {running} running job(s)"
                _replace(self.machines, old_bytes, mode)
        except sqlite3.Error:
            return "not restored: cannot read running jobs"
        return None

    def _backup(self, data: bytes, mode: int) -> Path:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.clock()))
        base = f"{self.machines.name}.bak-settings-{stamp}"
        for suffix in ("", *(f"-{n}" for n in range(2, 100))):
            path = self.machines.with_name(base + suffix)
            try:
                _write(path, data, mode, exclusive=True)
            except FileExistsError:
                continue
            return path
        raise OSError("no free backup name")

    def _restart(self, *, stop_on_failure: bool) -> tuple[tuple[str, ...], str | None]:
        done: list[str] = []
        failures: list[str] = []
        for service in self.restart:
            env = dict(self.environ)
            if service == heal.POLLER_SERVICE:
                env["ORCH_POLL_STOP_WAIT"] = heal.POLLER_STOP_WAIT
            code = self.run(heal.service_argv(service, self.deploy), env,
                            heal.service_log(service, self.state_dir))
            done.append(service_name(service))
            if code != 0:
                failures.append(f"restart {service_name(service)} exited {code}")
                if stop_on_failure:
                    break
        return tuple(done), "; ".join(failures) or None


def run_pass(applier: Applier, spool: Spool, owner: str) -> list[AuditEntry]:
    """Apply each request in the spool whose id is not in the audit log."""
    done = audited_ids(applier.audit_path)
    listing = spool.read()
    entries = []
    for item in listing.files:
        stem = item.name[: -len(".json")]
        if stem in done:
            continue
        reason = untrusted(listing, item, owner)
        if reason is None:
            try:
                entry = applier.apply(parse_request(item.body, stem))
            except BadRequest as exc:
                reason = str(exc)
        if reason is not None:
            entry = AuditEntry(stem, None, None, heal.stamp(applier.clock()), None, None,
                               None, None, REFUSED, reason)
        append_audit(applier.audit_path, entry)
        done.add(stem)
        entries.append(entry)
    return entries


def describe(entry: AuditEntry) -> str:
    text = (f"settings: {entry.id} {entry.result} {entry.device}.{entry.field} "
            f"{json.dumps(entry.previous)} -> {json.dumps(entry.new)}")
    if entry.error:
        text += f" ({entry.error})"
    if entry.restarted:
        text += f" restarted {','.join(entry.restarted)}"
    if entry.pending:
        text += f" pending {','.join(entry.pending)}"
    return text



USAGE = """\
orch settings apply [--state DIR] [--orch PATH] [--machines PATH] [--deploy DIR]
                    [--config DIR]
"""


def spool_for(config: SettingsConfig, push: object) -> Spool:
    if getattr(push, "transport", "none") == "ssh":
        return SshSpool(push.host, config.spool, ssh_config=push.ssh_config)
    return LocalSpool(Path(config.spool))


def main(argv: list[str], *, environ: Mapping[str, str] | None = None,
         clock: Callable[[], float] = time.time) -> int:
    environ = os.environ if environ is None else environ
    if argv[:1] != ["apply"]:
        print(USAGE, end="", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(prog="orch settings apply", usage=USAGE)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--orch", type=Path, default=ROOT / "orch.yml")
    parser.add_argument("--machines", type=Path, default=ROOT / "machines.yml")
    parser.add_argument("--deploy", type=Path, default=ROOT / "deploy")
    parser.add_argument("--config", type=Path)
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    orch_config, findings = load_orch_yml(args.orch, allow_placeholders=True)
    if orch_config is None:
        error = next((f.message for f in findings if f.severity == "error"), "invalid")
        print(f"settings apply: {args.orch.name}: {error}", file=sys.stderr)
        return 1
    settings = orch_config.dashboard.settings
    if settings is None:
        return 0
    home = Path(environ.get("HOME") or Path.home())
    state_dir = args.state or Path(environ.get("ORCH_STATE_DIR") or home / ".local/state/factory")
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / LOCK_NAME, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        restart, pending = services(args.config or home / ".config/factory", state_dir)
        db = Path(environ.get("ORCH_DB") or state_dir / "orch.db")
        applier = Applier(
            machines=args.machines, state_dir=state_dir, deploy=args.deploy,
            approver=settings.approver, environ=scrubbed_env(environ, orch_config),
            restart=restart, pending=pending, claims=claim_lock(db), clock=clock,
        )
        try:
            entries = run_pass(applier, spool_for(settings, orch_config.dashboard.push),
                               settings.spool_owner)
        except SpoolError as exc:
            print(f"settings apply: {exc}", file=sys.stderr)
            return 1
        except OSError as exc:
            print(f"settings apply: {type(exc).__name__}", file=sys.stderr)
            return 1
    for entry in entries:
        print(describe(entry))
    return 1 if any(entry.result == FAILED for entry in entries) else 0
