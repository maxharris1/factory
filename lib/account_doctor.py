from __future__ import annotations

import contextlib
import hashlib
import json
import re
import shlex
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from lib import attention, lane_limit
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

if TYPE_CHECKING:
    from lib.queue import SQLiteJobQueue

Status = Literal["ok", "signed-out", "mismatch", "unreachable"]
Harness = Literal["claude", "codex"]
Source = Literal["preflight", "runtime", "doctor"]

TABLE = "account_signins"
HOLDS = "signin_holds"
REASON = "account-signin"
INTERVAL_SECONDS = 30 * 60
HELD_INTERVAL_SECONDS = 2 * 60

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
_HOLD = (f"INSERT INTO {HOLDS} (account, device, since, seen, source, reason) VALUES (?1, ?2, ?3, ?3, ?4, ?5) "
         "ON CONFLICT(account, device) DO UPDATE SET seen = MAX(seen, excluded.seen)")

NOT_LOGGED_IN = "not logged in"
NOT_UNDERSTOOD = "status reply not understood"
EXIT_NONZERO = "status command exited nonzero"
_INCONCLUSIVE = frozenset({NOT_UNDERSTOOD, EXIT_NONZERO})
NO_IDENTITY = "sign-in names no org and email"
TOKEN_AUTH = "auth from a token variable, not the account folder"
CODEX_API_KEY = "api key, not a ChatGPT sign-in"
METHOD_NOT_UNDERSTOOD = "auth method not understood"
SHARED_IDENTITY = "same sign-in as account {}"
SPLIT_IDENTITY = "another sign-in on device {}"
PLACEHOLDER_DEVICE = "device has install placeholders"
NO_SSH_ALIAS = "no ssh_alias for {} on {}"
SSH_DOWN = "device unreachable over ssh"
NO_REPLY = "no status reply"
AUTHENTICATION_FAILED = "authentication_failed"

_BEGIN = b"ORCH-ACCOUNTS-BEGIN"
_FRAME = re.compile(rb"ORCH-ACCOUNT (\d+) (\d+)")
_SIGNED_OUT = {
    "claude": re.compile(r"OAuth session expired|not logged in|run /login", re.IGNORECASE),
    "codex": re.compile(r"not logged in|401 Unauthorized|Please (?:log out and )?sign in again", re.IGNORECASE),
}

_SUBSCRIPTION = re.compile(r"[a-z][a-z0-9_]{0,23}")
_CODEX_LINES = (
    ("Logged in using ChatGPT", "chatgpt", True),
    ("Logged in using an API key", "api_key", True),
    ("Not logged in", None, False),
)


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
}


_CLAUDE_METHODS = frozenset(_RULES["claude"]) | {"none"}


def _text(data: dict[str, object], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def _claude_method(value: object) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) and value in _CLAUDE_METHODS else "unknown"


def _subscription(value: object) -> str | None:
    return value if isinstance(value, str) and _SUBSCRIPTION.fullmatch(value) else None


def _fingerprint(org: str | None, email: str | None) -> str | None:
    if org is None or email is None:
        return None
    return hashlib.sha256(f"{org}\0{email}".encode()).hexdigest()[:12]


def _signed_in(code: int, logged_in: bool) -> tuple[bool, str]:
    if not logged_in:
        return False, NOT_LOGGED_IN
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


@dataclass(frozen=True)
class _Status:
    command: str
    streams: str
    home_variable: str
    token_variables: tuple[str, ...]
    parse: Callable[[str, str, int, bytes], Probe]


_STATUS: dict[Harness, _Status] = {
    "claude": _Status("claude auth status", "2>/dev/null", "CLAUDE_CONFIG_DIR",
                      ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"), parse_claude),
    "codex": _Status("codex login status", "2>&1", "CODEX_HOME", ("OPENAI_API_KEY", "CODEX_API_KEY"), parse_codex),
}
_TOKEN_VARIABLES = tuple(name for status in _STATUS.values() for name in status.token_variables)


def status_script(harness: Harness, homes: Sequence[str | None]) -> str:
    status = _STATUS[harness]
    calls = [
        "code=0; (" + ("" if home is None else f"{status.home_variable}={shlex.quote(home)} ")
        + f"{status.command} </dev/null {status.streams}) || code=$?; printf '\\nORCH-ACCOUNT %d %d\\n' {index} \"$code\";"
        for index, home in enumerate(homes)
    ]
    inner = " ".join([
        f"{_UNTRACE}set +e; unset {' '.join(_TOKEN_VARIABLES)} {status.home_variable};",
        f"printf '\\n%s\\n' {_BEGIN.decode()};",
        *calls,
    ])
    return _login(inner, "orch-accounts", ())


def _replies(stdout: bytes, count: int) -> dict[int, tuple[int, bytes]]:
    lines = stdout.split(b"\n")
    if _BEGIN not in lines:
        return {}
    replies: dict[int, tuple[int, bytes]] = {}
    reply: list[bytes] = []
    for line in lines[lines.index(_BEGIN) + 1:]:
        if len(replies) == count:
            break
        frame = _FRAME.fullmatch(line)
        if frame and int(frame[1]) == len(replies):
            replies[len(replies)] = (int(frame[2]), b"\n".join(reply))
            reply = []
        else:
            reply.append(line)
    return replies


def _unreached(account: str, device: str, harness: Harness, detail: str) -> Probe:
    return Probe(account, device, harness, False, False, None, None, None, detail)


def probe(transport: ByteTransport, config: MachinesConfig, device: Device, harness: Harness) -> list[Probe]:
    return _probe_accounts(transport, config, device, harness, config.accounts_on(device.name, harness))


def _probe_accounts(
    transport: ByteTransport, config: MachinesConfig, device: Device, harness: Harness, accounts: Sequence[str]
) -> list[Probe]:
    if not accounts:
        return []
    alias = device.ssh_alias_for(harness)
    if device.placeholder or alias is None:
        detail = PLACEHOLDER_DEVICE if device.placeholder else NO_SSH_ALIAS.format(harness, device.name)
        return [_unreached(account, device.name, harness, detail) for account in accounts]
    script = status_script(harness, [config.account_home(device.name, harness, account) for account in accounts])
    exchanged = transport.exchange(
        [SSH_BINARY, *SSH_OPTIONS, "-l", device.login_for(harness), "--", alias, script],
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    if exchanged.timed_out or exchanged.code == SSH_UNREACHABLE:
        return [_unreached(account, device.name, harness, SSH_DOWN) for account in accounts]
    replies = _replies(exchanged.stdout, len(accounts))
    return [
        _STATUS[harness].parse(account, device.name, *replies[index]) if index in replies
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


def check_login(transport: ByteTransport, config: MachinesConfig, device: Device, harness: str, account: str) -> None:
    """Raise SignedOut when the account's login on device reads not logged in."""
    if harness not in _STATUS:
        return
    [reading] = _probe_accounts(transport, config, device, harness, (account,))
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
        fingerprint=probe.fingerprint if rule.needs_fingerprint and rule.status == "ok" else None,
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


_LOGIN: dict[Harness, str] = {"claude": "claude auth login", "codex": "codex login"}
_THEN = ", then orch accounts doctor"
_STEP: dict[Status, str] = {
    "signed-out": "sign in as {login} on {device}: {home}{command}" + _THEN,
    "mismatch": "sign {account} in to its own account as {login} on {device}: {home}{command}" + _THEN,
    "unreachable": "fix ssh to {device} for {harness}" + _THEN,
}
_TOKEN_STEP = "remove the token export from {login}'s login profile on {device}" + _THEN


def _home(variable: str, path: str | None) -> str:
    return f"env -u {variable} " if path is None else f"{variable}={shlex.quote(path)} "


def _step(row: SignIn, config: MachinesConfig) -> str:
    harness = config.account(row.account).harness
    path = None if row.status == "unreachable" else config.account_home(row.device, harness, row.account)
    template = _TOKEN_STEP if row.detail == TOKEN_AUTH else _STEP[row.status]
    return template.format(
        account=row.account,
        device=row.device,
        harness=harness,
        login=config.device(row.device).login_for(harness),
        home=_home(_STATUS[harness].home_variable, path),
        command=_LOGIN[harness],
    )


def _definite(row: SignIn) -> bool:
    return row.status == "mismatch" or (row.status == "signed-out" and row.detail not in _INCONCLUSIVE)


def flags(
    rows: Sequence[SignIn], holds: Mapping[tuple[str, str], Hold], config: MachinesConfig, repo: str
) -> dict[tuple[str, str, str], str]:
    shown = [
        replace(row, status="signed-out", detail=holds[row.account, row.device].reason)
        if (row.account, row.device) in holds and not _definite(row) else row
        for row in rows
    ]
    return {
        (repo.lower(), f"{row.account}@{row.device}", REASON):
            f"{row.status}; run: {_step(row, config)}; {row.detail}"
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


def write(
    connection: sqlite3.Connection,
    rows: Sequence[SignIn],
    config: MachinesConfig,
    repo: str | None,
    probed_at: float,
) -> None:
    ensure_table(connection)
    connection.execute(_HOLDS_SCHEMA)
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
        if repo is not None:
            attention.sync(connection, (REASON,), (repo,), flags(rows, _holds(connection), config, repo), probed_at)
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def run(
    connection: sqlite3.Connection,
    config: MachinesConfig,
    transport: ByteTransport,
    now: float,
    repo: str | None,
) -> list[SignIn]:
    pairs = [(device, harness) for device in config.devices for harness in _STATUS]
    with ThreadPoolExecutor(max_workers=max(len(pairs), 1)) as pool:
        probed = list(pool.map(lambda pair: probe(transport, config, *pair), pairs))
    rows = judge([item for items in probed for item in items], now)
    write(connection, rows, config, repo, now)
    return rows


def check(
    queue: SQLiteJobQueue, transport: ByteTransport, repo: str | None, config: MachinesConfig, now: float
) -> None:
    with contextlib.closing(queue.connect()) as connection:
        run(connection, config, transport, now, repo)
