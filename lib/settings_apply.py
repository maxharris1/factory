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
  the file under it: no job lands between the check and the write. The new
  text is a line edit inside that device's block, so comments and other
  devices stay byte-for-byte; both texts are parsed with load_machines_yml
  and must differ in exactly that one field.
* machines.yml is copied to `machines.yml.bak-settings-<UTC stamp>` beside
  it, then replaced atomically, and still under the lock the new file is
  written into the shared settings row (lib/live_settings.py, #852): a
  narrowing reaches every claim at once, a widening once the poller adopts
  the file and every machines.yml the row holds allows it. The entry records the row's `written` then as its `version`, and
  the dashboard shows the change pending until the poller has adopted it.
  The poller and then each waiter this account owns and has not held
  stopped are restarted through their service scripts (lib/heal.py
  service_argv) with heal's scrubbed environment, after the lock
  is released; services another account owns are listed as pending, and the
  result is then `pending`, not `applied`. A failed restart restores the
  backup under the same lock and writes it into the shared settings row (not
  when that would disable a device now holding a job), restarts them again
  and records `failed`.
* A body with `target: "account"` is an account request (lib/account_settings.py)
  and takes this same path, with its own checks and line edit. A prepare or
  doctor request for one account on one device takes none of it: it runs
  `orch accounts prepare` over the doctor's ssh path with this machines.yml
  on stdin, then the doctor, and records the pair's
  account_signins status before and after. A signout request runs the
  harness's logout there instead, refused while the pair holds a running job.
  A signin request is the sign-in broker's (lib/signin_broker.py): the
  applier skips it, untrusted or malformed ones too, and writes no line.
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
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, closing, contextmanager
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, ClassVar, Protocol

from lib import (
    account_doctor,
    account_requests,
    account_signin,
    dashboard_push,
    heal,
    live_settings,
    self_deploy,
)
from lib.config_overlay import overlay_path, read_overlay
from lib.dashboard_config import SettingsConfig, is_bot_login
from lib.device_pool import RunningPair, running_pairs, slots_in_use
from lib.load import is_strict_int, load_yaml
from lib.machines_yml import Device, MachinesConfig, load_machines_yml
from lib.orch_yml import load_orch_yml
from lib.remote import _UNTRACE, PROBE_TIMEOUT_SECONDS, SSH_UNREACHABLE, ByteTransport, SshTransport, _login
from lib.wait import OWNING_BOTS

if TYPE_CHECKING:
    from lib.account_settings import AccountRequest, PairRequest

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
SIGNED_OUT = "signed-out"

REQUEST_NAME = re.compile(r"^([A-Za-z0-9_-]{16,64})\.json$")
REQUESTED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
REQUEST_KEYS = frozenset({"id", "actor", "requested_at", "device", "field", "expect", "value"})
PREPARE_PROG = "orch accounts prepare"


def _is_bool(value: object) -> bool:
    return isinstance(value, bool)


def _is_slot_max(value: object) -> bool:
    return is_strict_int(value) and 1 <= value <= SLOT_MAX_LIMIT


FIELDS: Mapping[str, Callable[[object], bool]] = MappingProxyType(
    {"enabled": _is_bool, "drain": _is_bool, "slot_max": _is_slot_max}
)


@dataclass(frozen=True)
class SettingsRequest:
    target: ClassVar[str | None] = None
    account: ClassVar[str | None] = None
    id: str
    actor: str
    requested_at: str
    device: str
    field: str
    expect: object
    value: object


class BadRequest(ValueError):
    pass


def parse_request(body: bytes, stem: str) -> SettingsRequest | AccountRequest | PairRequest:
    """The request in a spool file named `<stem>.json`; values are checked
    against FIELDS later, so a bad value is refused with its device named."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BadRequest("not-json") from None
    if not isinstance(data, dict):
        raise BadRequest("bad-keys")
    if "target" in data:
        if data["target"] != "account":
            raise BadRequest("bad-target")
        from lib import account_settings

        return account_settings.parse(data, stem)
    if set(data) != REQUEST_KEYS:
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
    version: int | None = None
    target: str | None = None
    account: str | None = None

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
    if isinstance(value, list):
        return [_clip(item) for item in value]
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
        fcntl.flock(out, fcntl.LOCK_EX)
        out.write(entry.line() + "\n")
        out.flush()
        os.fsync(out.fileno())


def is_broker_request(body: bytes) -> bool:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return (isinstance(data, dict) and data.get("target") == "account"
            and isinstance(data.get("op"), str) and data["op"] in account_requests.BROKER_OPS)




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




_ITEM = re.compile(r"^(?P<dash>[ ]*-[ ]+)(?P<rest>\S.*)$")


def _scalar(text: str) -> str:
    value = text.split(" #", 1)[0].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _blocks(lines: Sequence[str], section: str) -> list[tuple[int, int, int]]:
    """(first line, last content line, key indent) of each `section` item."""
    head = re.compile(rf"^{section}:[ \t]*(#.*)?$")
    top = next((i for i, line in enumerate(lines) if head.match(line.rstrip("\r\n"))), None)
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


def _yaml_str(text: str) -> str:
    # PyYAML reads 1e3 as text but YAML 1.2 tools read it as a number, so any
    # string not starting with a letter is quoted too.
    if text[:1].isalpha() and load_yaml(text) == text:
        return text
    return json.dumps(text)


def _render(value: object) -> str:
    if isinstance(value, list):
        return f"[{', '.join(map(_yaml_str, value))}]"
    return ("true" if value else "false") if isinstance(value, bool) else str(value)


def _item(lines: Sequence[str], section: str, id_key: str,
          ident: str) -> tuple[tuple[int, int, int], int] | None:
    for block in _blocks(lines, section):
        found = _key_lines(lines, block, id_key)
        if len(found) != 1:
            continue
        index, column = found[0]
        if _scalar(lines[index].rstrip("\r\n")[column + len(id_key) + 1:]) == ident:
            return block, index
    return None


def _set_key(lines: list[str], block: tuple[int, int, int], after: int, key: str,
             value: object) -> str | None:
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
        if not lines[after].endswith("\n"):
            lines[after] += "\n"
        eol = "\r\n" if lines[after].endswith("\r\n") else "\n"
        lines.insert(after + 1, " " * block[2] + f"{key}: {_render(value)}{eol}")
    return "".join(lines)


def edit_device_field(text: str, device: str, key: str, value: object) -> str | None:
    """text with `key: value` set in the device's block, or None when the
    block cannot be found or edited by line."""
    lines = text.splitlines(keepends=True)
    found = _item(lines, "devices", "name", device)
    return None if found is None else _set_key(lines, *found, key, value)


def _comments(text: str) -> list[str]:
    return [found[1] for line in text.splitlines()
            if (found := re.search(r"(?:^|[ \t])(#.*)$", line)) is not None]


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


def _overlay_sets(machines: Path, section: str, id_key: str, ident: str, key: str) -> bool:
    data, _ = read_overlay(machines)
    entries = (data or {}).get(section) or ()
    return any(
        isinstance(entry, dict) and entry.get(id_key) == ident and key in entry
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


@dataclass(frozen=True)
class Held:
    running: Callable[[MachinesConfig], Mapping[str, int]]
    commit_version: Callable[[Path], int | None]
    pairs: Callable[[MachinesConfig], list[RunningPair]]
    hold: Callable[[str, str, str, float], bool]


def approve(request: SettingsRequest | AccountRequest | PairRequest, approver: str) -> None:
    if request.actor.casefold() != approver.casefold() or is_bot_login(request.actor):
        raise Refused("not-approver")


def resolve_pair(request: PairRequest, machines: Path, approver: str,
                 db: Path | None) -> tuple[MachinesConfig, Device, str]:
    """The pair's (config, device, harness), or Refused naming why it may not run."""
    approve(request, approver)
    config, _ = load_machines_yml(machines)
    if config is None:
        raise Refused("machines.yml is invalid")
    account = config.account(request.account)
    if account is None:
        raise Refused("unknown-account")
    device = config.device(request.device)
    if device is None:
        raise Refused("unknown-device")
    if request.account not in config.accounts_on(device.name, account.harness):
        raise Refused(f"{request.account} is not signed in on {device.name}")
    if db is None or not db.is_file():
        raise Refused("no orch.db to record the doctor in")
    return config, device, account.harness


def prepare_pair(transport: ByteTransport, machines: Path, config: MachinesConfig, device: Device,
                 harness: str, account: str) -> str | None:
    """Run `orch accounts prepare` for the pair on its device; the error, or None."""
    unreachable = account_doctor.no_ssh_path(device, harness)
    if unreachable is not None:
        return unreachable
    if config.accounts_on(device.name, harness)[:1] == (account,):
        return None
    command = shlex.join([f"{device.factory_root_for(harness)}/bin/orch", "accounts", "prepare",
                          "--device", device.name, "--account", account,
                          "--machines", "/dev/stdin"])
    exchanged = transport.exchange(
        account_doctor.ssh_argv(device, harness,
                                _login(f"{_UNTRACE}exec {command}", "orch-accounts-prepare", ())),
        timeout=PROBE_TIMEOUT_SECONDS, input=machines.read_bytes())
    if exchanged.code == 0:
        return None
    said = _orch_lines_only(exchanged.stderr)
    return f"prepare exited {exchanged.code}" + (f": {_clip(said[-1])}" if said else "")


ClaimLock = Callable[[], AbstractContextManager[Held]]
NO_DATABASE = Held(running=lambda config: {}, commit_version=lambda machines: None,
                   pairs=lambda config: [], hold=lambda account, device, owner, now: True)


def claim_lock(db: Path) -> ClaimLock:
    @contextmanager
    def hold() -> Iterator[Held]:
        if not db.is_file():
            yield NO_DATABASE
            return
        connection = sqlite3.connect(db, timeout=30, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")

            def commit_version(machines: Path) -> int | None:
                try:
                    live = live_settings.requested(connection, machines)
                    updated, _ = load_machines_yml(machines)
                    if updated is not None:
                        account_doctor.extend_signin_fence(
                            connection, account_doctor.declared_pairs(updated),
                        )
                    connection.execute("COMMIT")
                except sqlite3.Error:
                    return None
                return live.written

            def hold_pair(account: str, device: str, owner: str, now: float) -> bool:
                taken = account_doctor.reserve_pair(connection, account, device, owner, now)
                connection.execute("COMMIT")
                return taken

            yield Held(running=lambda config: slots_in_use(connection, config),
                       commit_version=commit_version,
                       pairs=lambda config: running_pairs(connection, config),
                       hold=hold_pair)
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
    db: Path | None = None
    transport: ByteTransport = field(default_factory=SshTransport)
    repo: str | None = None

    @property
    def audit_path(self) -> Path:
        return self.state_dir / AUDIT_NAME

    def apply(self, request: SettingsRequest | AccountRequest | PairRequest) -> AuditEntry:
        from lib.account_settings import PairRequest

        if isinstance(request, PairRequest):
            return self._apply_signout(request) if request.op == "signout" else self._apply_pair(request)
        try:
            with self.claims() as held:
                old_bytes, new_text, previous = self._plan(request, held)
                mode = stat.S_IMODE(os.stat(self.machines).st_mode)
                backup = self._backup(old_bytes, mode)
                _replace(self.machines, new_text.encode("utf-8"), mode)
                version = held.commit_version(self.machines)
        except Refused as refusal:
            return self._entry(request, REFUSED, refusal.previous, refusal.error)
        except sqlite3.Error:
            return self._entry(request, REFUSED, None, "cannot read running jobs")
        return self._restart_all(request, old_bytes, mode, backup, previous, version)

    def _entry(self, request: SettingsRequest | AccountRequest | PairRequest, result: str,
               previous: object, error: str | None = None, **extra: object) -> AuditEntry:
        return AuditEntry(
            id=request.id, actor=_clip(request.actor), requested_at=request.requested_at,
            applied_at=heal.stamp(self.clock()), device=_clip(request.device),
            field=_clip(request.field), previous=_clip(previous), new=_clip(request.value),
            result=result, error=error, target=request.target,
            account=_clip(request.account), **extra,
        )

    def _plan(self, request: SettingsRequest | AccountRequest,
              held: Held) -> tuple[bytes, str, object]:
        approve(request, self.approver)
        old_bytes = self.machines.read_bytes()
        old, _ = load_machines_yml(self.machines)
        if old is None:
            raise Refused("machines.yml is invalid")
        text = old_bytes.decode("utf-8")
        if request.target == "account":
            from lib import account_settings

            new_text, previous = account_settings.plan(request, old, text, self.machines,
                                                       self._parse, held.pairs)
        else:
            new_text, previous = self._plan_device(request, old, text, held)
        if _comments(new_text) != _comments(text):
            raise Refused("the edit changed a comment", previous)
        return old_bytes, new_text, previous

    def _apply_pair(self, request: PairRequest) -> AuditEntry:
        try:
            config, device, harness = resolve_pair(request, self.machines, self.approver, self.db)
        except Refused as refusal:
            return self._entry(request, REFUSED, None, refusal.error)
        previous = error = None
        try:
            with closing(sqlite3.connect(self.db, timeout=30, isolation_level=None)) as connection:
                account_doctor.ensure_table(connection)
                previous = _signin(connection, request)
                error = prepare_pair(self.transport, self.machines, config, device, harness, request.account)
                account_doctor.run(connection, config, self.transport, self.clock(), self.repo)
                new = _signin(connection, request)
        except sqlite3.Error as exc:
            error = "; ".join(filter(None, (error, f"doctor: {type(exc).__name__}")))
            return self._entry(request, FAILED, previous, error)
        entry = self._entry(request, FAILED if error else APPLIED, previous, error)
        return replace(entry, new=_clip(new))

    def _apply_signout(self, request: PairRequest) -> AuditEntry:
        try:
            config, device, harness = resolve_pair(request, self.machines, self.approver, self.db)
        except Refused as refusal:
            return self._entry(request, REFUSED, None, refusal.error)
        if request.account == config.accounts_on(device.name, harness)[0]:
            return self._entry(request, REFUSED, None, "default-account")
        blocked = account_doctor.no_ssh_path(device, harness)
        previous = None
        try:
            with closing(sqlite3.connect(self.db, timeout=30, isolation_level=None)) as connection:
                account_doctor.ensure_table(connection)
                previous = _signin(connection, request)
                try:
                    with self.claims() as held:
                        busy = any(pair.account == request.account and pair.device == device.name
                                   for pair in held.pairs(config))
                        taken = busy or blocked is not None or held.hold(
                            request.account, device.name, request.id, self.clock())
                except sqlite3.Error:
                    return self._entry(request, REFUSED, previous, "cannot read running jobs")
                if busy:
                    return self._entry(request, REFUSED, previous, "running-job")
                if not taken:
                    return self._entry(request, REFUSED, previous, "signing-in")
                try:
                    error = blocked or self._logout(
                        device, harness, config.account_home(device.name, harness, request.account))
                    account_doctor.run(connection, config, self.transport, self.clock(), self.repo,
                                       deliberate=(request.account, device.name))
                    new = _signin(connection, request)
                finally:
                    account_doctor.release_pair(connection, request.account, device.name, request.id)
        except sqlite3.Error:
            return self._entry(request, FAILED, previous, "doctor-db")
        if error is None and new != "signed-out":
            error = "unreachable" if new == "unreachable" else "still-signed-in"
        entry = self._entry(request, FAILED if error else SIGNED_OUT, previous, error)
        return replace(entry, new=_clip(new))

    def _logout(self, device: Device, harness: str, home: str) -> str | None:
        exchanged = self.transport.exchange(
            account_doctor.ssh_argv(device, harness, account_signin.logout_script(harness, home)),
            timeout=PROBE_TIMEOUT_SECONDS)
        if exchanged.timed_out:
            return "logout-timeout"
        if exchanged.code == SSH_UNREACHABLE:
            return "unreachable"
        return None if exchanged.code == 0 else "logout-exit"

    def _plan_device(self, request: SettingsRequest, old: MachinesConfig, text: str,
                     held: Held) -> tuple[str, object]:
        device = old.device(request.device)
        if device is None:
            raise Refused("unknown-device")
        check = FIELDS.get(request.field)
        if check is None:
            raise Refused("bad-field")
        previous = getattr(device, request.field)
        if not check(request.value) or not check(request.expect):
            raise Refused("bad-value", previous)
        if _overlay_sets(self.machines, "devices", "name", request.device, request.field):
            raise Refused(f"{request.field} is set in machines.local.yml", previous)
        if request.expect != previous:
            raise Refused("changed", previous)
        if request.value == previous:
            raise Refused("no-change", previous)
        if request.field == "enabled" and request.value is False:
            try:
                running = held.running(old).get(request.device, 0)
            except sqlite3.Error:
                raise Refused("cannot read running jobs", previous) from None
            if running:
                raise Refused(f"{request.device} holds {running} running job(s); drain it first",
                              previous)
        new_text = edit_device_field(text, request.device, request.field, request.value)
        if new_text is None:
            raise Refused("cannot edit the device's block", previous)
        new = self._parse(new_text, previous)
        if _changes(old, new) != [(request.device, request.field, request.value)]:
            raise Refused("the edit changed more than that field", previous)
        return new_text, previous

    def _parse(self, text: str, previous: object) -> MachinesConfig:
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / self.machines.name
            probe.write_text(text, encoding="utf-8")
            local = overlay_path(self.machines)
            if local.is_file():
                shutil.copyfile(local, overlay_path(probe))
            new, findings = load_machines_yml(probe)
        if new is None:
            error = next((f.message for f in findings if f.severity == "error"), "invalid")
            raise Refused(f"invalid after the edit: {error}"[:200], previous)
        return new

    def _restart_all(self, request: SettingsRequest | AccountRequest, old_bytes: bytes, mode: int,
                     backup: Path, previous: object, version: int | None) -> AuditEntry:
        restarted, failure = self._restart(stop_on_failure=True)
        if failure is None:
            return self._entry(request, PENDING if self.pending else APPLIED, previous,
                               backup=backup.name, restarted=restarted, pending=self.pending,
                               version=version)
        kept = self._restore(request, old_bytes, mode)
        again, refailure = self._restart(stop_on_failure=False)
        error = (f"{failure}; " + (kept or f"restored {backup.name}")
                 + (f"; then {refailure}" if refailure else ""))
        return self._entry(request, FAILED, previous, error,
                           backup=backup.name,
                           restarted=tuple(dict.fromkeys(restarted + again)),
                           pending=self.pending)

    def _restore(self, request: SettingsRequest | AccountRequest, old_bytes: bytes,
                 mode: int) -> str | None:
        try:
            with self.claims() as held:
                now, _ = load_machines_yml(self.machines)
                if request.field == "enabled" and request.value is True:
                    running = 0 if now is None else held.running(now).get(request.device, 0)
                    if running:
                        return f"not restored: {request.device} holds {running} running job(s)"
                if now is not None:
                    from lib import account_settings

                    before = self._parse(old_bytes.decode("utf-8"), None)
                    reason = account_settings.running_job_blocker(now.accounts, before.accounts,
                                                              held.pairs(now))
                    if reason is not None:
                        return f"not restored: {reason}"
                _replace(self.machines, old_bytes, mode)
                held.commit_version(self.machines)
        except sqlite3.Error:
            return "not restored: cannot read running jobs"
        except Refused as refusal:
            return f"not restored: {refusal.error}"
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
            self._prune_backups(preserve=path)
            return path
        raise OSError("no free backup name")

    def _prune_backups(self, keep: int = 20, *, preserve: Path | None = None) -> None:
        prefix = f"{self.machines.name}.bak-settings-"
        try:
            backups = sorted(
                p for p in self.machines.parent.iterdir()
                if p.is_file() and not p.is_symlink() and p.name.startswith(prefix)
            )
        except OSError:
            return
        if preserve is not None:
            backups = [p for p in backups if p != preserve]
            keep = max(0, keep - 1)
        stale_items = backups[:-keep] if keep > 0 and len(backups) > keep else (backups if keep == 0 else ())
        for stale in stale_items:
            stale.unlink(missing_ok=True)

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


def _orch_lines_only(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if line.startswith(f"{PREPARE_PROG}:")]


def _signin(connection: sqlite3.Connection, request: PairRequest) -> str | None:
    row = connection.execute(
        f"SELECT status FROM {account_doctor.TABLE} WHERE account = ? AND device = ?",
        (request.account, request.device)).fetchone()
    return None if row is None else row[0]


def run_pass(applier: Applier, spool: Spool, owner: str) -> list[AuditEntry]:
    """Apply each request in the spool whose id is not in the audit log."""
    done = audited_ids(applier.audit_path)
    listing = spool.read()
    entries = []
    for item in listing.files:
        stem = item.name[: -len(".json")]
        if stem in done or is_broker_request(item.body):
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
    subject = entry.device
    if entry.target == "account":
        subject = f"account {entry.account}" + (f"@{entry.device}" if entry.device else "")
    text = (f"settings: {entry.id} {entry.result} {subject}.{entry.field} "
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
            db=db, repo=self_deploy.own_repo(ROOT),
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
