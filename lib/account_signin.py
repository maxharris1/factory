from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from lib.account_doctor import TOKEN_AUTH, Harness, SignIn
from lib.harness_env import ACCOUNT_ISOLATION
from lib.remote import _UNTRACE, _login


@dataclass(frozen=True)
class Prompt:
    url: str
    code: str | None


@dataclass(frozen=True)
class Outcome:
    result: Literal["ok", "mismatch", "failed"]
    error: str | None
    doctor: str | None


@dataclass(frozen=True)
class _Allow:
    url: re.Pattern[str]
    code: re.Pattern[str] | None


_ALLOW: dict[Harness, _Allow] = {
    "claude": _Allow(re.compile(r"https://claude\.com/cai/oauth/authorize(?:\?[A-Za-z0-9._~%&=+-]*)?"), None),
    "codex": _Allow(re.compile(r"https://auth\.openai\.com/codex/device"), re.compile(r"[A-Z0-9]{4}-[A-Z0-9]{4,5}")),
}
_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|.)", re.DOTALL)


def parse_output(harness: Harness, output: bytes) -> Prompt | None:
    allow = _ALLOW[harness]
    tokens = _ESCAPE.sub(" ", output.decode("utf-8", errors="replace")).split()
    url = next((token for token in tokens if allow.url.fullmatch(token)), None)
    code = None if allow.code is None else next((token for token in tokens if allow.code.fullmatch(token)), None)
    if url is None or (allow.code is not None and code is None):
        return None
    return Prompt(url, code)


def judge(new: SignIn, before: Mapping[str, SignIn], after: Mapping[str, SignIn]) -> Outcome:
    shared = new.fingerprint is not None and any(row.fingerprint == new.fingerprint for row in after.values())
    if shared or new.status == "mismatch":
        return Outcome("mismatch", "shared-identity", new.status)
    if any(row.status == "ok" and not _unchanged(row, after.get(account)) for account, row in before.items()):
        return Outcome("mismatch", "disturbed-account", new.status)
    if new.status == "ok" and new.auth_method != "oauth_token":
        return Outcome("ok", None, new.status)
    if new.auth_method == "oauth_token" or new.detail == TOKEN_AUTH:
        return Outcome("failed", "token-auth", new.status)
    return Outcome("failed", "unreachable" if new.status == "unreachable" else "signed-out", new.status)


def _unchanged(row: SignIn, later: SignIn | None) -> bool:
    return later is not None and (later.status, later.fingerprint) == (row.status, row.fingerprint)


_TOKENS = " ".join(name for isolation in ACCOUNT_ISOLATION.values() for name in isolation.tokens)
_LOGIN: dict[Harness, str] = {"claude": "claude auth login --claudeai", "codex": "codex login --device-auth"}
_LOGOUT: dict[Harness, str] = {"claude": "claude auth logout", "codex": "codex logout"}


def login_script(harness: Harness, home: str) -> str:
    return _account_cli(harness, home, _LOGIN[harness], "orch-accounts-login")


def logout_script(harness: Harness, home: str) -> str:
    return _account_cli(harness, home, f"{_LOGOUT[harness]} </dev/null >/dev/null 2>&1", "orch-accounts-logout")


def _account_cli(harness: Harness, home: str, command: str, name: str) -> str:
    inner = (f"{_UNTRACE}unset {_TOKENS} CLAUDE_SECURESTORAGE_CONFIG_DIR; exec env "
             f"{ACCOUNT_ISOLATION[harness].home}={shlex.quote(home)} {command}")
    return _login(inner, name, ())
