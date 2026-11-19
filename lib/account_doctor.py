from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

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
