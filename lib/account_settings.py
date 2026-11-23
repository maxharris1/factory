"""Account requests for `orch settings apply`.

A spool body `{id, actor, requested_at, target: "account", op: "set",
account, field, expect, value}` sets `max_jobs` or `paused` on one declared
account through lib/settings_apply.py's lock, backup, replace, restart and
audit. Pause writes `paused: true`; resume deletes the line.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar

from lib.load import is_strict_int
from lib.machines_yml import EXTERNAL, Account, MachinesConfig, default_account
from lib.schema import HARNESSES
from lib.settings_apply import (
    REQUESTED_AT,
    SLOT_MAX_LIMIT,
    BadRequest,
    Refused,
    _changes,
    _is_bool,
    _item,
    _key_lines,
    _overlay_sets,
    _set_key,
)

COMMON_KEYS = frozenset({"id", "actor", "requested_at", "target", "op", "account"})
OPS: Mapping[str, frozenset[str]] = MappingProxyType(
    {"set": frozenset({"field", "expect", "value"})}
)


def _is_max_jobs(value: object) -> bool:
    return is_strict_int(value) and 1 <= value <= SLOT_MAX_LIMIT


def _is_max_jobs_expect(value: object) -> bool:
    return value is None or is_strict_int(value)


FIELDS: Mapping[str, tuple[Callable[[object], bool], Callable[[object], bool]]] = (
    MappingProxyType({"max_jobs": (_is_max_jobs, _is_max_jobs_expect),
                      "paused": (_is_bool, _is_bool)})
)


@dataclass(frozen=True)
class AccountRequest:
    target: ClassVar[str] = "account"
    device: ClassVar[None] = None
    id: str
    actor: str
    requested_at: str
    op: str
    account: str
    field: str
    expect: object
    value: object


def parse(data: dict, stem: str) -> AccountRequest:
    op = data.get("op")
    if not isinstance(op, str) or op not in OPS:
        raise BadRequest("bad-op")
    if set(data) != COMMON_KEYS | OPS[op]:
        raise BadRequest("bad-keys")
    for key in ("id", "actor", "requested_at", "account", "field"):
        if not isinstance(data[key], str):
            raise BadRequest(f"bad-{key}")
    if data["id"] != stem:
        raise BadRequest("id-not-file-name")
    if REQUESTED_AT.fullmatch(data["requested_at"]) is None:
        raise BadRequest("bad-requested_at")
    return AccountRequest(**{k: v for k, v in data.items() if k != "target"})


def edit_account_field(text: str, account: str, key: str, value: object) -> str | None:
    lines = text.splitlines(keepends=True)
    found = _item(lines, "accounts", "id", account)
    if found is None:
        return None
    block, after = found
    if key == "paused" and value is False:
        keys = _key_lines(lines, block, key)
        if len(keys) != 1 or keys[0][0] == block[0]:
            return None
        del lines[keys[0][0]]
        return "".join(lines)
    return _set_key(lines, block, after, key, value)


def _only_change(old: MachinesConfig, new: MachinesConfig, request: AccountRequest) -> bool:
    if _changes(old, new) != [] or [a.id for a in old.accounts] != [a.id for a in new.accounts]:
        return False
    return [
        (before.id, item.name, getattr(after, item.name))
        for before, after in zip(old.accounts, new.accounts)
        for item in fields(Account)
        if getattr(before, item.name) != getattr(after, item.name)
    ] == [(request.account, request.field, request.value)]


def plan(request: AccountRequest, old: MachinesConfig, text: str, machines: Path,
         parse_text: Callable[[str, object], MachinesConfig]) -> tuple[str, object]:
    if request.account in {default_account(harness) for harness in HARNESSES}:
        raise Refused("read-only")
    account = next((item for item in old.accounts if item.id == request.account), None)
    if account is None:
        raise Refused("unknown-account")
    if account.harness == EXTERNAL:
        raise Refused("read-only")
    if request.field not in FIELDS:
        raise Refused("bad-field")
    value_ok, expect_ok = FIELDS[request.field]
    previous = getattr(account, request.field)
    if not value_ok(request.value) or not expect_ok(request.expect):
        raise Refused("bad-value", previous)
    if _overlay_sets(machines, "accounts", "id", request.account, request.field):
        raise Refused(f"{request.field} is set in machines.local.yml", previous)
    if request.expect != previous:
        raise Refused("changed", previous)
    if request.value == previous:
        raise Refused("no-change", previous)
    new_text = edit_account_field(text, request.account, request.field, request.value)
    if new_text is None:
        raise Refused("cannot edit the account's block", previous)
    if not _only_change(old, parse_text(new_text, previous), request):
        raise Refused("the edit changed more than that field", previous)
    return new_text, previous
