from __future__ import annotations

import hashlib
import json
import re
import shlex
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

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
Harness = Literal["claude", "codex"]

TABLE = "account_signins"

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

NOT_LOGGED_IN = "not logged in"
NOT_UNDERSTOOD = "status reply not understood"
EXIT_NONZERO = "status command exited nonzero"
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

_BEGIN = b"ORCH-ACCOUNTS-BEGIN"
_FRAME = re.compile(rb"ORCH-ACCOUNT (\d+) (\d+)")

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
    accounts = config.accounts_on(device.name, harness)
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


def ensure_table(connection: sqlite3.Connection) -> None:
    connection.execute(_SCHEMA)


def write(connection: sqlite3.Connection, rows: Sequence[SignIn]) -> None:
    ensure_table(connection)
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(f"DELETE FROM {TABLE}")
        connection.executemany(
            f"INSERT INTO {TABLE} (account, device, status, auth_method, subscription_type, "
            "fingerprint, checked_at, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(row.account, row.device, row.status, row.auth_method, row.subscription_type,
              row.fingerprint, row.checked_at, row.detail) for row in rows],
        )
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def run(connection: sqlite3.Connection, config: MachinesConfig, transport: ByteTransport, now: float) -> list[SignIn]:
    probes = [
        item
        for device in config.devices
        for harness in _STATUS
        for item in probe(transport, config, device, harness)
    ]
    rows = judge(probes, now)
    write(connection, rows)
    return rows
