from __future__ import annotations

import hashlib
import json
import re
import shlex
import sqlite3
import subprocess
from collections import defaultdict
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Literal

from lib import attention, epoch, lane_limit
from lib.harness_env import account_tokens, harness_env
from lib.harnesses.gemini import AUTH_OVERRIDES as GEMINI_AUTH_OVERRIDES, UNSET as _GEMINI_UNSET
from lib.machines_yml import Device, MachinesConfig
from lib.remote import (
    _UNTRACE,
    PROBE_TIMEOUT_SECONDS,
    SSH_BINARY,
    SSH_OPTIONS,
    SSH_UNREACHABLE,
    ByteTransport,
    _login,
)

Status = Literal["ok", "signed-out", "mismatch", "unreachable"]
Harness = Literal["claude", "codex", "cursor", "gemini"]
Source = Literal["preflight", "runtime", "doctor"]

TABLE = "account_signins"
HOLDS = "signin_holds"
DROPS = "signin_drops"
REASON = "account-signin"
INTERVAL_SECONDS = 30 * 60
HELD_INTERVAL_SECONDS = 2 * 60
SIGNOUTS = "signout_locks"
FENCES = "signin_fences"
SIGNOUT_LEASE_SECONDS = 10 * 60
FENCE_OWNER = "broker-restart"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    account TEXT NOT NULL,
    device TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok','signed-out','mismatch','unreachable')),
    auth_method TEXT,
    subscription_type TEXT,
    fingerprint TEXT,
    checked_at REAL NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (account, device)
)
"""
_HOLDS_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {HOLDS} (
    account TEXT NOT NULL,
    device TEXT NOT NULL,
    since REAL NOT NULL,
    seen REAL NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('preflight','runtime','doctor')),
    reason TEXT NOT NULL,
    PRIMARY KEY (account, device)
)
"""
_DROPS_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {DROPS} (
    account TEXT NOT NULL,
    device TEXT NOT NULL,
    since REAL NOT NULL,
    announced INTEGER NOT NULL CHECK (announced IN (0,1)),
    PRIMARY KEY (account, device)
)
"""
_SIGNOUTS_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {SIGNOUTS} (
    account TEXT NOT NULL,
    device TEXT NOT NULL,
    since REAL NOT NULL,
    owner TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (account, device)
)
"""
_FENCES_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {FENCES} (
    owner TEXT PRIMARY KEY,
    since REAL NOT NULL
)
"""
_HOLD = (f"INSERT INTO {HOLDS} (account, device, since, seen, source, reason) VALUES (?1, ?2, ?3, ?3, ?4, ?5) "
         "ON CONFLICT(account, device) DO UPDATE SET seen = MAX(seen, excluded.seen)")

NOT_LOGGED_IN = "not logged in"
NOT_UNDERSTOOD = "status reply not understood"
EXIT_NONZERO = "status command exited nonzero"
PARTIAL_LOGIN = "partly signed in"
CLI_NOT_FOUND = "harness CLI not found"
COMMAND_NOT_FOUND = 127
_INCONCLUSIVE = frozenset({NOT_UNDERSTOOD, EXIT_NONZERO})
NO_IDENTITY = "sign-in names no org and email"
TOKEN_AUTH = "auth from a token variable, not the account folder"
CODEX_API_KEY = "api key, not a ChatGPT sign-in"
METHOD_NOT_UNDERSTOOD = "auth method not understood"
SHARED_IDENTITY = "same sign-in as account {}"
SPLIT_IDENTITY = "another sign-in on device {}"
DISTURBED_ACCOUNT = "signing in changed account {}"
PLACEHOLDER_DEVICE = "device has install placeholders"
NO_SSH_ALIAS = "no ssh_alias for {} on {}"
SSH_DOWN = "device unreachable over ssh"
NO_REPLY = "no status reply"
AUTHENTICATION_FAILED = "authentication_failed"

_BEGIN = b"ORCH-ACCOUNTS-BEGIN"
_FRAME = re.compile(rb"ORCH-ACCOUNT (\d+) (\d+)")
_STDERR = b"ORCH-ACCOUNT-STDERR"
_SIGNED_OUT = {
    "claude": re.compile(r"OAuth session expired|not logged in|run /login", re.IGNORECASE),
    "codex": re.compile(r"not logged in|401 Unauthorized|Please (?:log out and )?sign in again", re.IGNORECASE),
    "cursor": re.compile(
        r"not logged in|not authenticated|run cursor-agent login|401 Unauthorized|invalid api key",
        re.IGNORECASE,
    ),
    "gemini": re.compile(
        r"Please sign in|not signed in|Launch the CLI without arguments to sign in|authentication required",
        re.IGNORECASE,
    ),
}

SUBSCRIPTION = re.compile(r"[a-z][a-z0-9_]{0,23}")
_CODEX_LINES = (
    ("Logged in using ChatGPT", "chatgpt", True),
    ("Logged in using an API key", "api_key", True),
    ("Not logged in", None, False),
)
_CURSOR_STATES: dict[str, tuple[bool, str]] = {
    "authenticated": (True, ""),
    "partially-authenticated": (False, PARTIAL_LOGIN),
    "unauthenticated": (False, NOT_LOGGED_IN),
    "error": (False, EXIT_NONZERO),
}
GEMINI_SIGNED_OUT = "Please sign in to view available models"
_GEMINI_MODEL_ROW = re.compile(r"(?P<model>[a-z0-9]+(?:\.[a-z0-9]+)*(?:-[a-z0-9]+(?:\.[a-z0-9]+)*)+)\s+(?P<name>\S.*)")


@dataclass(frozen=True)
class Probe:
    account: str
    device: str
    harness: Harness
    reached: bool
    logged_in: bool
    auth_method: str | None
    subscription_type: str | None
    fingerprint: str | None
    detail: str


@dataclass(frozen=True)
class SignIn:
    account: str
    device: str
    status: Status
    auth_method: str | None
    subscription_type: str | None
    fingerprint: str | None
    checked_at: float
    detail: str


@dataclass(frozen=True)
class Hold:
    account: str
    device: str
    since: float
    seen: float
    source: Source
    reason: str


@dataclass(frozen=True)
class Drop:
    account: str
    device: str
    harness: Harness
    since: float
    reading: str


@dataclass(frozen=True)
class _Rule:
    status: Status
    detail: str = ""
    needs_fingerprint: bool = False


_UNKNOWN_METHOD = _Rule("signed-out", METHOD_NOT_UNDERSTOOD)
_TOKEN = _Rule("signed-out", TOKEN_AUTH)
_RULES: dict[Harness, dict[str | None, _Rule]] = {
    "claude": {
        "claude.ai": _Rule("ok", needs_fingerprint=True),
        "oauth_token": _TOKEN,
        "api_key": _TOKEN,
        "auth_token": _TOKEN,
        "apiKeyHelper": _TOKEN,
    },
    "codex": {
        "chatgpt": _Rule("ok"),
        "api_key": _Rule("signed-out", CODEX_API_KEY),
    },
    "cursor": {"login": _Rule("ok")},
    "gemini": {"login": _Rule("ok")},
}


_CLAUDE_METHODS = frozenset(_RULES["claude"]) | {"none"}
AUTH_METHODS: dict[Harness, frozenset[str]] = {
    "claude": _CLAUDE_METHODS | {"unknown"},
    "codex": frozenset(method for _, method, _ in _CODEX_LINES if method is not None),
    "cursor": frozenset(_RULES["cursor"]),
    "gemini": frozenset(_RULES["gemini"]),
}


def _text(data: dict[str, object], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def _claude_method(value: object) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) and value in _CLAUDE_METHODS else "unknown"


def _subscription(value: object) -> str | None:
    return value if isinstance(value, str) and SUBSCRIPTION.fullmatch(value) else None


def _id_text(value: object) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return value if isinstance(value, str) and value else None


def _fingerprint(*parts: str | None) -> str | None:
    if None in parts:
        return None
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:12]


def _signed_in(code: int, logged_in: bool, signed_out: str = NOT_LOGGED_IN) -> tuple[bool, str]:
    if not logged_in:
        return False, signed_out
    if code != 0:
        return False, EXIT_NONZERO
    return True, ""


def _json_object(reply: bytes) -> dict[str, object] | None:
    try:
        data = json.loads(reply)
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def parse_claude(account: str, device: str, code: int, reply: bytes) -> Probe:
    data = _json_object(reply)
    if data is None or not isinstance(data.get("loggedIn"), bool):
        return Probe(account, device, "claude", True, False, None, None, None, NOT_UNDERSTOOD)
    logged_in, detail = _signed_in(code, data["loggedIn"])
    return Probe(
        account=account,
        device=device,
        harness="claude",
        reached=True,
        logged_in=logged_in,
        auth_method=_claude_method(data.get("authMethod")),
        subscription_type=_subscription(data.get("subscriptionType")),
        fingerprint=_fingerprint(_text(data, "orgId"), _text(data, "email")),
        detail=detail,
    )


def parse_codex(account: str, device: str, code: int, reply: bytes) -> Probe:
    lines = reply.decode("utf-8", errors="replace").splitlines()
    first = lines[0].strip() if lines else ""
    for prefix, method, logged_in in _CODEX_LINES:
        if first.startswith(prefix):
            signed_in, detail = _signed_in(code, logged_in)
            return Probe(account, device, "codex", True, signed_in, method, None, None, detail)
    return Probe(account, device, "codex", True, False, None, None, None, NOT_UNDERSTOOD)


def parse_cursor(account: str, device: str, code: int, reply: bytes) -> Probe:
    data = _json_object(reply)
    state = _CURSOR_STATES.get(_text(data, "status") or "") if data is not None else None
    if state is None:
        return Probe(account, device, "cursor", True, False, None, None, None, NOT_UNDERSTOOD)
    logged_in, detail = _signed_in(code, *state)
    user = data.get("userInfo")
    user_id = _id_text(user.get("userId")) if isinstance(user, dict) else None
    return Probe(
        account=account,
        device=device,
        harness="cursor",
        reached=True,
        logged_in=logged_in,
        auth_method="login" if logged_in else None,
        subscription_type=None,
        fingerprint=_fingerprint("cursor", user_id),
        detail=detail,
    )


def _gemini_model_rows(text: str) -> list[tuple[str, str]]:
    rows = [_GEMINI_MODEL_ROW.fullmatch(line.strip()) for line in text.splitlines() if line.strip()]
    return [(row["model"], row["name"]) for row in rows] if all(rows) else []


def parse_gemini(account: str, device: str, code: int, stdout: bytes, stderr: bytes) -> Probe:
    out = stdout.decode("utf-8", errors="replace")
    if code == 0 and _gemini_model_rows(out):
        return Probe(account, device, "gemini", True, True, "login", None, None, "")
    if code == 1 and GEMINI_SIGNED_OUT in out + stderr.decode("utf-8", errors="replace"):
        return Probe(account, device, "gemini", True, False, None, None, None, NOT_LOGGED_IN)
    return Probe(account, device, "gemini", True, False, None, None, None, NOT_UNDERSTOOD)


def _ignoring_stderr(parse: Callable[[str, str, int, bytes], Probe]) -> Callable[[str, str, int, bytes, bytes], Probe]:
    return lambda account, device, code, stdout, _stderr: parse(account, device, code, stdout)


@dataclass(frozen=True)
class _Status:
    command: str
    stderr: Literal["drop", "merge", "split"]
    home_variable: str
    token_variables: tuple[str, ...]
    parse: Callable[[str, str, int, bytes, bytes], Probe]


_STATUS: dict[Harness, _Status] = {
    "claude": _Status("claude auth status", "drop", "CLAUDE_CONFIG_DIR",
                      ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
                      _ignoring_stderr(parse_claude)),
    "codex": _Status("codex login status", "merge", "CODEX_HOME", ("OPENAI_API_KEY", "CODEX_API_KEY"),
                     _ignoring_stderr(parse_codex)),
    "cursor": _Status("cursor-agent status --format json", "drop", "CURSOR_CONFIG_DIR",
                      ("CURSOR_API_KEY", "CURSOR_AUTH_TOKEN"), _ignoring_stderr(parse_cursor)),
    "gemini": _Status("env" + "".join(f" -u {name}" for name in _GEMINI_UNSET) + " agy models", "split",
                      "GEMINI_CLI_HOME", GEMINI_AUTH_OVERRIDES, parse_gemini),
}
ACCOUNT_TOKEN_VARIABLES = tuple(name for status in _STATUS.values() for name in status.token_variables)


def status_script(harness: Harness, homes: Sequence[str | None], *, drop_profile_login: bool = True) -> str:
    status = _STATUS[harness]
    streams = {"drop": "2>/dev/null", "merge": "2>&1", "split": '2>"$err"'}[status.stderr]
    split = status.stderr == "split"
    calls = [
        ('err=$(mktemp); ' if split else "")
        + "code=0; (" + ("" if home is None else f"{status.home_variable}={shlex.quote(home)} ")
        + f"{status.command} </dev/null {streams}) || code=$?; "
        + (f"printf '\\n%s\\n' {_STDERR.decode()}; cat \"$err\"; rm -f \"$err\"; " if split else "")
        + f"printf '\\nORCH-ACCOUNT %d %d\\n' {index} \"$code\";"
        for index, home in enumerate(homes)
    ]
    inner = " ".join([
        f"{_UNTRACE}set +e;",
        *([f"unset {' '.join(ACCOUNT_TOKEN_VARIABLES)} {status.home_variable};"] if drop_profile_login else []),
        f"printf '\\n%s\\n' {_BEGIN.decode()};",
        *calls,
    ])
    return _login(inner, "orch-accounts", ())


def _streams(reply: list[bytes], split: bool) -> tuple[bytes, bytes]:
    if not split or _STDERR not in reply:
        return b"\n".join(reply), b""
    at = reply.index(_STDERR)
    return b"\n".join(reply[:at]), b"\n".join(reply[at + 1:])


def _replies(stdout: bytes, count: int, *, split: bool = False) -> dict[int, tuple[int, bytes, bytes]]:
    lines = stdout.split(b"\n")
    if _BEGIN not in lines:
        return {}
    replies: dict[int, tuple[int, bytes, bytes]] = {}
    reply: list[bytes] = []
    for line in lines[lines.index(_BEGIN) + 1:]:
        if len(replies) == count:
            break
        frame = _FRAME.fullmatch(line)
        if frame and int(frame[1]) == len(replies):
            replies[len(replies)] = (int(frame[2]), *_streams(reply, split))
            reply = []
        else:
            reply.append(line)
    return replies


def _unreached(account: str, device: str, harness: Harness, detail: str) -> Probe:
    return Probe(account, device, harness, False, False, None, None, None, detail)


def no_ssh_path(device: Device, harness: str) -> str | None:
    if device.placeholder:
        return PLACEHOLDER_DEVICE
    if device.ssh_alias_for(harness) is None:
        return NO_SSH_ALIAS.format(harness, device.name)
    return None


def ssh_argv(device: Device, harness: str, script: str, *, tty: bool = False) -> list[str]:
    return [SSH_BINARY, *SSH_OPTIONS, *(["-tt"] if tty else []), "-l", device.login_for(harness), "--",
            device.ssh_alias_for(harness), script]


def probe(transport: ByteTransport, config: MachinesConfig, device: Device, harness: Harness) -> list[Probe]:
    return _probe_accounts(transport, config, device, harness, config.accounts_on(device.name, harness))


def _probe_accounts(
    transport: ByteTransport,
    config: MachinesConfig,
    device: Device,
    harness: Harness,
    accounts: Sequence[str],
    *,
    drop_profile_login: bool = True,
) -> list[Probe]:
    if not accounts:
        return []
    detail = no_ssh_path(device, harness)
    if detail is not None:
        return [_unreached(account, device.name, harness, detail) for account in accounts]
    script = status_script(
        harness, [config.account_home(device.name, harness, account) for account in accounts], drop_profile_login=drop_profile_login
    )
    exchanged = transport.exchange(ssh_argv(device, harness, script), timeout=PROBE_TIMEOUT_SECONDS)
    if exchanged.timed_out or exchanged.code == SSH_UNREACHABLE:
        return [_unreached(account, device.name, harness, SSH_DOWN) for account in accounts]
    replies = _replies(exchanged.stdout, len(accounts), split=_STATUS[harness].stderr == "split")
    return [
        _read(harness, account, device.name, *replies[index]) if index in replies
        else _unreached(account, device.name, harness, NO_REPLY)
        for index, account in enumerate(accounts)
    ]


class SignedOut(Exception):
    def __init__(self, account: str, device: str, harness: Harness, detail: str) -> None:
        super().__init__(f"preflight-signed-out: {account}@{device} ({harness}): {detail}")
        self.account = account
        self.device = device
        self.harness = harness
        self.detail = detail


def _read(harness: Harness, account: str, device: str, code: int, stdout: bytes, stderr: bytes) -> Probe:
    if code == COMMAND_NOT_FOUND:
        return Probe(account, device, harness, True, False, None, None, None, CLI_NOT_FOUND)
    return _STATUS[harness].parse(account, device, code, stdout, stderr)


def _probe_local(
    device: Device, harness: Harness, account: str, home: str | None, environ: Mapping[str, str]
) -> Probe:
    status = _STATUS[harness]
    try:
        done = subprocess.run(
            shlex.split(status.command),
            env={k: v for k, v in environ.items() if k not in account_tokens(harness, home)}
            | harness_env(harness, home),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr={"drop": subprocess.DEVNULL, "merge": subprocess.STDOUT, "split": subprocess.PIPE}[status.stderr],
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        return _read(harness, account, device.name, COMMAND_NOT_FOUND, b"", b"")
    except (OSError, subprocess.TimeoutExpired):
        return _unreached(account, device.name, harness, NO_REPLY)
    return _read(harness, account, device.name, done.returncode, done.stdout, done.stderr or b"")


def check_login(
    transport: ByteTransport,
    config: MachinesConfig,
    device: Device,
    harness: str,
    account: str,
    environ: Mapping[str, str],
) -> None:
    if harness not in _STATUS:
        return
    home = config.account_home(device.name, harness, account)
    if device.local:
        reading = _probe_local(device, harness, account, home, environ)
    else:
        [reading] = _probe_accounts(transport, config, device, harness, (account,), drop_profile_login=home is not None)
    if reading.detail == NOT_LOGGED_IN:
        raise SignedOut(account, device.name, harness, reading.detail)


def _rule(probe: Probe) -> _Rule:
    if not probe.logged_in:
        return _Rule("signed-out", probe.detail)
    rule = _RULES[probe.harness].get(probe.auth_method, _UNKNOWN_METHOD)
    if rule.needs_fingerprint and probe.fingerprint is None:
        return _Rule("signed-out", NO_IDENTITY)
    return rule


def _first_pass(probe: Probe, now: float) -> SignIn:
    if not probe.reached:
        return SignIn(probe.account, probe.device, "unreachable", None, None, None, now, probe.detail)
    rule = _rule(probe)
    return SignIn(
        account=probe.account,
        device=probe.device,
        status=rule.status,
        auth_method=probe.auth_method,
        subscription_type=probe.subscription_type,
        fingerprint=probe.fingerprint if rule.status == "ok" else None,
        checked_at=now,
        detail=rule.detail,
    )


def _mismatch(row: SignIn, accounts: dict[str, set[str]], devices: dict[str, dict[str, str]]) -> SignIn:
    shared = sorted(accounts[row.fingerprint] - {row.account})
    split = sorted(device for device, seen in devices[row.account].items() if seen != row.fingerprint)
    details = []
    if shared:
        details.append(SHARED_IDENTITY.format(", ".join(shared)))
    if split:
        details.append(SPLIT_IDENTITY.format(", ".join(split)))
    if not details:
        return row
    return SignIn(row.account, row.device, "mismatch", row.auth_method, row.subscription_type,
                  row.fingerprint, row.checked_at, "; ".join(details))


def judge(probes: Sequence[Probe], now: float) -> list[SignIn]:
    rows = [_first_pass(probe, now) for probe in probes]
    identified = [row for row in rows if row.status == "ok" and row.fingerprint is not None]
    accounts: dict[str, set[str]] = defaultdict(set)
    devices: dict[str, dict[str, str]] = defaultdict(dict)
    for row in identified:
        accounts[row.fingerprint].add(row.account)
        devices[row.account][row.device] = row.fingerprint
    return [
        _mismatch(row, accounts, devices) if row.status == "ok" and row.fingerprint is not None else row
        for row in rows
    ]


_LOGIN: dict[Harness, tuple[tuple[str, ...], str]] = {
    "claude": (("CLAUDE_CODE_OAUTH_TOKEN",), "claude auth login"),
    "codex": ((), "codex login --device-auth"),
    "cursor": (("CURSOR_API_KEY", "CURSOR_AUTH_TOKEN"), "cursor-agent login"),
    "gemini": (GEMINI_AUTH_OVERRIDES, "agy"),
}
_THEN = ", then orch accounts doctor"
_STEP: dict[Status, str] = {
    "signed-out": "sign in as {login} on {device}: {command}" + _THEN,
    "mismatch": "sign {account} in to its own account as {login} on {device}: {command}" + _THEN,
    "unreachable": "fix ssh to {device} for {harness}" + _THEN,
}
_DETAIL_STEP = {
    TOKEN_AUTH: "remove the token export from {login}'s login profile on {device}" + _THEN,
    CLI_NOT_FOUND: "install the {harness} CLI for {login} on {device}" + _THEN,
}


def login_command(harness: Harness, home: str | None) -> str:
    unset, login = _LOGIN[harness]
    variable = _STATUS[harness].home_variable
    assign = ""
    if home is None:
        unset = (*unset, variable)
    else:
        assign = f"{variable}={shlex.quote(home)} "
    env = "".join(f" -u {name}" for name in unset)
    return (f"env{env} " if env else "") + assign + login


def _step(row: SignIn, config: MachinesConfig) -> str:
    harness = config.account(row.account).harness
    path = None if row.status == "unreachable" else config.account_home(row.device, harness, row.account)
    template = _DETAIL_STEP.get(row.detail, _STEP[row.status])
    return template.format(
        account=row.account,
        device=row.device,
        harness=harness,
        login=config.device(row.device).login_for(harness),
        command=login_command(harness, path),
    )


def _definite(row: SignIn) -> bool:
    return row.status == "mismatch" or (row.status == "signed-out" and row.detail not in _INCONCLUSIVE)


def _shown(row: SignIn, hold: Hold | None) -> SignIn:
    return replace(row, status="signed-out", detail=hold.reason) if hold is not None and not _definite(row) else row


def _reading(row: SignIn, config: MachinesConfig) -> str:
    return f"{row.status}; run: {_step(row, config)}; {row.detail}"


def flags(
    rows: Sequence[SignIn], holds: Mapping[tuple[str, str], Hold], config: MachinesConfig, repo: str
) -> dict[tuple[str, str, str], str]:
    shown = [_shown(row, holds.get((row.account, row.device))) for row in rows]
    return {
        (repo.lower(), f"{row.account}@{row.device}", REASON): _reading(row, config)
        for row in shown
        if row.status != "ok"
    }


def ensure_table(connection: sqlite3.Connection) -> None:
    connection.execute(_SCHEMA)


def hold(connection: sqlite3.Connection, account: str, device: str, source: Source, reason: str, now: float) -> None:
    connection.execute(_HOLDS_SCHEMA)
    connection.execute(_HOLD, (account, device, now, source, reason))


def detect_signout(harness: str, event: dict) -> bool:
    if harness == "claude" and event.get("error") == AUTHENTICATION_FAILED:
        return True
    pattern = _SIGNED_OUT.get(harness)
    text = lane_limit.error_text(harness, event)
    return pattern is not None and text is not None and pattern.search(text) is not None


def _holds(connection: sqlite3.Connection) -> dict[tuple[str, str], Hold]:
    connection.execute(_HOLDS_SCHEMA)
    return {
        (account, device): Hold(account, device, since, seen, source, reason)
        for account, device, since, seen, source, reason in connection.execute(
            f"SELECT account, device, since, seen, source, reason FROM {HOLDS}")
    }


def held(connection: sqlite3.Connection) -> frozenset[tuple[str, str]]:
    return frozenset(_holds(connection))


def _signouts(connection: sqlite3.Connection) -> None:
    connection.execute(_SIGNOUTS_SCHEMA)
    if "owner" not in {row[1] for row in connection.execute(f"PRAGMA table_info({SIGNOUTS})")}:
        connection.execute(f"ALTER TABLE {SIGNOUTS} ADD COLUMN owner TEXT NOT NULL DEFAULT ''")


def _fences(connection: sqlite3.Connection) -> None:
    connection.execute(_FENCES_SCHEMA)


def declared_pairs(config: MachinesConfig) -> list[tuple[str, str]]:
    return sorted({(a.id, d) for a in config.accounts for d in a.signed_in_on})


def reserve_pair(connection: sqlite3.Connection, account: str, device: str, owner: str, now: float) -> bool:
    _signouts(connection)
    _fences(connection)
    if connection.execute(
        f"SELECT 1 FROM {FENCES} WHERE since > ? LIMIT 1",
        (now - SIGNOUT_LEASE_SECONDS,),
    ).fetchone() is not None:
        return False
    taken = connection.execute(
        f"INSERT INTO {SIGNOUTS} (account, device, since, owner) VALUES (?1, ?2, ?3, ?4) "
        "ON CONFLICT(account, device) DO UPDATE SET since = excluded.since, owner = excluded.owner "
        "WHERE owner = excluded.owner OR since <= ?5",
        (account, device, now, owner, now - SIGNOUT_LEASE_SECONDS))
    return taken.rowcount == 1


def renew_pair(connection: sqlite3.Connection, account: str, device: str, owner: str, now: float) -> None:
    _signouts(connection)
    connection.execute(f"UPDATE {SIGNOUTS} SET since = ? WHERE account = ? AND device = ? AND owner = ?",
                       (now, account, device, owner))


def release_pair(connection: sqlite3.Connection, account: str, device: str, owner: str) -> None:
    _signouts(connection)
    connection.execute(f"DELETE FROM {SIGNOUTS} WHERE account = ? AND device = ? AND owner = ?",
                       (account, device, owner))


def signing_out(connection: sqlite3.Connection, now: float) -> frozenset[tuple[str, str]]:
    connection.execute(_SIGNOUTS_SCHEMA)
    epoch.register(connection)
    return frozenset(
        (str(account), str(device))
        for account, device in connection.execute(
            f"SELECT account, device FROM {SIGNOUTS} WHERE {epoch.sql('since')} > ?", (now - SIGNOUT_LEASE_SECONDS,)))


def fence_signins(connection: sqlite3.Connection, pairs: Collection[tuple[str, str]], now: float) -> bool:
    """Reserve every pair a sign-in could take, unless a sign-in, sign-out or
    restart fence already holds one. Any broker's admission then refuses the
    pair as signing out. The caller supplies the transaction."""
    _signouts(connection)
    _fences(connection)
    if signing_out(connection, now):
        return False
    if connection.execute(
        f"SELECT 1 FROM {FENCES} WHERE since > ? LIMIT 1",
        (now - SIGNOUT_LEASE_SECONDS,),
    ).fetchone() is not None:
        return False
    connection.execute(
        f"INSERT OR REPLACE INTO {FENCES} (owner, since) VALUES (?, ?)",
        (FENCE_OWNER, now),
    )
    connection.executemany(
        f"INSERT OR REPLACE INTO {SIGNOUTS} (account, device, since, owner) VALUES (?, ?, ?, ?)",
        [(account, device, now, FENCE_OWNER) for account, device in sorted(pairs)])
    return True


def extend_signin_fence(connection: sqlite3.Connection, pairs: Collection[tuple[str, str]]) -> None:
    """Extend an active restart fence to cover pairs newly added to machines.yml
    so even an un-upgraded broker refuses them under the same BEGIN IMMEDIATE
    transaction. Caller supplies the transaction."""
    _signouts(connection)
    _fences(connection)
    row = connection.execute(
        f"SELECT since FROM {FENCES} WHERE owner = ?", (FENCE_OWNER,)
    ).fetchone()
    if row is None:
        return
    fence_since = float(row[0])
    stale_cutoff = fence_since - SIGNOUT_LEASE_SECONDS
    connection.executemany(
        f"INSERT INTO {SIGNOUTS} (account, device, since, owner) VALUES (?1, ?2, ?3, ?4) "
        "ON CONFLICT(account, device) DO UPDATE SET since = excluded.since, owner = excluded.owner "
        "WHERE owner = excluded.owner OR since <= ?5",
        [(account, device, fence_since, FENCE_OWNER, stale_cutoff) for account, device in sorted(pairs)],
    )


def unfence_signins(connection: sqlite3.Connection) -> None:
    _signouts(connection)
    _fences(connection)
    connection.execute(f"DELETE FROM {FENCES} WHERE owner = ?", (FENCE_OWNER,))
    connection.execute(f"DELETE FROM {SIGNOUTS} WHERE owner = ?", (FENCE_OWNER,))


def newest_hold(connection: sqlite3.Connection) -> float | None:
    connection.execute(_HOLDS_SCHEMA)
    (newest,) = connection.execute(f"SELECT MAX(since) FROM {HOLDS}").fetchone()
    return newest


def due(last: float | None, newest: float | None, now: float) -> bool:
    if last is None or (newest is not None and newest > last):
        return True
    return now - last >= (INTERVAL_SECONDS if newest is None else HELD_INTERVAL_SECONDS)


def _drop_unlisted_holds(connection: sqlite3.Connection, rows: Sequence[SignIn]) -> None:
    listed = {(row.account, row.device) for row in rows}
    connection.executemany(
        f"DELETE FROM {HOLDS} WHERE account = ? AND device = ?",
        [pair for pair in _holds(connection) if pair not in listed],
    )


def _record_drops(
    connection: sqlite3.Connection,
    rows: Sequence[SignIn],
    holds: Mapping[tuple[str, str], Hold],
    probed_at: float,
    deliberate: tuple[str, str] | None,
) -> None:
    connection.executemany(
        f"INSERT INTO {DROPS} (account, device, since, announced) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(account, device) DO UPDATE SET announced = MAX(announced, excluded.announced)",
        [(row.account, row.device, probed_at, int((row.account, row.device) == deliberate)) for row in rows
         if (row.status == "signed-out" and row.detail not in _INCONCLUSIVE)
         or ((row.account, row.device) in holds and row.status != "ok")],
    )
    listed = {(row.account, row.device) for row in rows}
    connection.executemany(
        f"DELETE FROM {DROPS} WHERE account = ? AND device = ?",
        [(row.account, row.device) for row in rows if row.status == "ok"]
        + [(account, device) for account, device in connection.execute(f"SELECT account, device FROM {DROPS}")
           if (account, device) not in listed],
    )


def write(
    connection: sqlite3.Connection,
    rows: Sequence[SignIn],
    config: MachinesConfig,
    repo: str | None,
    probed_at: float,
    *,
    deliberate: tuple[str, str] | None = None,
) -> None:
    ensure_table(connection)
    connection.execute(_HOLDS_SCHEMA)
    connection.execute(_DROPS_SCHEMA)
    attention.ensure_table(connection)
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(f"DELETE FROM {TABLE}")
        connection.executemany(
            f"INSERT INTO {TABLE} (account, device, status, auth_method, subscription_type, "
            "fingerprint, checked_at, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(row.account, row.device, row.status, row.auth_method, row.subscription_type,
              row.fingerprint, row.checked_at, row.detail) for row in rows],
        )
        connection.executemany(_HOLD, [
            (row.account, row.device, probed_at, "doctor", row.detail)
            for row in rows if row.status == "signed-out" and row.detail == NOT_LOGGED_IN
        ])
        connection.executemany(
            f"DELETE FROM {HOLDS} WHERE account = ? AND device = ? AND seen < ?",
            [(row.account, row.device, probed_at) for row in rows if row.status == "ok"],
        )
        _drop_unlisted_holds(connection, rows)
        holds = _holds(connection)
        _record_drops(connection, rows, holds, probed_at, deliberate)
        if repo is not None:
            attention.sync(connection, (REASON,), (repo,), flags(rows, holds, config, repo), probed_at)
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def take_unannounced(connection: sqlite3.Connection, config: MachinesConfig) -> list[Drop]:
    ensure_table(connection)
    connection.execute(_DROPS_SCHEMA)
    declared = {(account, device.name) for device in config.devices for harness in _STATUS
                for account in config.accounts_on(device.name, harness)}
    holds = _holds(connection)
    taken = [
        (since, SignIn(*row)) for since, *row in connection.execute(
            f"SELECT d.since, s.account, s.device, s.status, s.auth_method, s.subscription_type, s.fingerprint, "
            f"s.checked_at, s.detail FROM {DROPS} d JOIN {TABLE} s ON s.account = d.account AND s.device = d.device "
            "WHERE d.announced = 0 ORDER BY d.since, d.account, d.device")
        if (row[0], row[1]) in declared
    ]
    connection.executemany(f"UPDATE {DROPS} SET announced = 1 WHERE account = ? AND device = ?",
                           [(row.account, row.device) for _, row in taken])
    return [
        Drop(row.account, row.device, config.account(row.account).harness, since,
             _reading(_shown(row, holds.get((row.account, row.device))), config))
        for since, row in taken
    ]


def observe(config: MachinesConfig, transport: ByteTransport, now: float) -> list[SignIn]:
    pairs = [(device, harness) for device in config.devices for harness in _STATUS]
    with ThreadPoolExecutor(max_workers=max(len(pairs), 1)) as pool:
        probed = list(pool.map(lambda pair: probe(transport, config, *pair), pairs))
    return judge([item for items in probed for item in items], now)


def run(
    connection: sqlite3.Connection,
    config: MachinesConfig,
    transport: ByteTransport,
    now: float,
    repo: str | None,
    *,
    deliberate: tuple[str, str] | None = None,
) -> list[SignIn]:
    rows = observe(config, transport, now)
    write(connection, rows, config, repo, now, deliberate=deliberate)
    return rows
