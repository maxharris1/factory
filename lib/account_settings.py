"""Account requests for `orch settings apply`.

A spool body `{id, actor, requested_at, target: "account", op, account, ...}`
changes one account in machines.yml through lib/settings_apply.py's lock,
backup, replace, restart and audit:

* `set` with `field`, `expect`, `value` sets `max_jobs`, `paused` or
  `signed_in_on` on a declared account. Pause writes `paused: true`; resume
  deletes the line, keeping its comment.
* `add` with `harness`, `max_jobs`, `signed_in_on` appends a new account.
* `remove` with `expect`, the account as shown, deletes a paused account's
  block, keeping its comments as comment lines.

No change may drop an account, or an account's device, that holds a running
job, and a failed restart never restores a file that would.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar, NamedTuple

from lib import account_requests
from lib.account_requests import COMMON_KEYS
from lib.device_pool import RunningPair
from lib.load import is_strict_int, load_yaml
from lib.machines_yml import ACCOUNT_ID, EXTERNAL, Account, MachinesConfig, default_account
from lib.schema import HARNESSES
from lib.settings_apply import (
    _ITEM,
    REQUESTED_AT,
    SLOT_MAX_LIMIT,
    BadRequest,
    Refused,
    _blocks,
    _changes,
    _is_bool,
    _item,
    _key_lines,
    _overlay_sets,
    _render,
    _set_key,
)

ADD_KEYS = ("harness", "max_jobs", "signed_in_on")
_HEAD = re.compile(r"^accounts:(?P<empty>[ 	]*\[\])?(?P<rest>[ 	]*(?:#.*)?)$")


def _is_max_jobs(value: object) -> bool:
    return is_strict_int(value) and 1 <= value <= SLOT_MAX_LIMIT


def _is_max_jobs_expect(value: object) -> bool:
    return value is None or is_strict_int(value)


def _is_devices(value: object) -> bool:
    return (isinstance(value, list) and all(isinstance(item, str) for item in value)
            and len(set(value)) == len(value))


def _as_is(value: object) -> object:
    return value


FIELDS: Mapping[str, tuple[Callable[[object], bool], Callable[[object], bool],
                           Callable[[object], object]]] = MappingProxyType({
    "max_jobs": (_is_max_jobs, _is_max_jobs_expect, _as_is),
    "paused": (_is_bool, _is_bool, _as_is),
    "signed_in_on": (_is_devices, _is_devices, frozenset),
})


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
    if set(data) != COMMON_KEYS | OPS[op].keys:
        raise BadRequest("bad-keys")
    for key in ("id", "actor", "requested_at", "account"):
        if not isinstance(data[key], str):
            raise BadRequest(f"bad-{key}")
    if data["id"] != stem:
        raise BadRequest("id-not-file-name")
    if REQUESTED_AT.fullmatch(data["requested_at"]) is None:
        raise BadRequest("bad-requested_at")
    field, expect, value = OPS[op].request(data)
    if not isinstance(field, str):
        raise BadRequest("bad-field")
    return AccountRequest(id=data["id"], actor=data["actor"], requested_at=data["requested_at"],
                          op=op, account=data["account"], field=field, expect=expect, value=value)


def running_job_blocker(before: Sequence[Account], after: Sequence[Account],
                    pairs: Sequence[RunningPair]) -> str | None:
    kept = {account.id: account for account in after}
    for account in before:
        held = [pair for pair in pairs if pair.account == account.id]
        if not held:
            continue
        if account.id not in kept:
            return f"{account.id} holds {len(held)} running job(s)"
        dropped = account.signed_in_on - kept[account.id].signed_in_on
        busy = sorted(device for device in dropped if any(p.device == device for p in held))
        if busy:
            return f"{account.id} holds a running job on {busy[0]}"
    return None


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
        line, column = keys[0]
        body = lines[line].rstrip("\r\n")
        comment = re.search(r"[ \t](#.*)$", body)
        if comment is None:
            del lines[line]
        else:
            lines[line] = " " * column + comment[1] + lines[line][len(body):]
        return "".join(lines)
    return _set_key(lines, block, after, key, value)


def _head(lines: Sequence[str]) -> int | None:
    return next((index for index, line in enumerate(lines)
                 if re.match(r"accounts:(\s|$)", line)), None)


def _yaml_id(ident: str) -> str:
    # PyYAML reads 1e3 as text but YAML 1.2 tools read it as a number, so any
    # id not starting with a letter is quoted too.
    if ident[:1].isalpha() and load_yaml(ident) == ident:
        return ident
    return json.dumps(ident)


def add_account(text: str, account: Account) -> str | None:
    lines = text.splitlines(keepends=True)
    eol = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += eol
    top = _head(lines)
    if top is None:
        lines.append("accounts:" + eol)
        top = len(lines) - 1
    head = _HEAD.match(lines[top].rstrip("\r\n"))
    if head is None:
        return None
    if head["empty"]:
        lines[top] = "accounts:" + head["rest"] + eol
    blocks = _blocks(lines, "accounts")
    if blocks:
        start, at, keys = blocks[-1]
        dash = _ITEM.match(lines[start].rstrip("\r\n"))["dash"]
    else:
        at, dash, keys = top, "  - ", 4
    indent = " " * keys
    block = [f"{dash}id: {_yaml_id(account.id)}", f"{indent}harness: {account.harness}"]
    if account.signed_in_on:
        block.append(f"{indent}signed_in_on: {_render(sorted(account.signed_in_on))}")
    if account.max_jobs is not None:
        block.append(f"{indent}max_jobs: {account.max_jobs}")
    lines[at + 1:at + 1] = [line + eol for line in block]
    return "".join(lines)


def remove_account(text: str, account: str) -> str | None:
    lines = text.splitlines(keepends=True)
    found = _item(lines, "accounts", "id", account)
    if found is None:
        return None
    (start, end, _), _ = found
    only = len(_blocks(lines, "accounts")) == 1
    kept = []
    for line in lines[start:end + 1]:
        body = line.rstrip("\r\n")
        content = body.lstrip(" ")
        comment = re.search(r"[ \t](#.*)$", body)
        if not content or content.startswith("#"):
            kept.append(line)
        elif comment is not None:
            kept.append(" " * (len(body) - len(content)) + comment[1] + line[len(body):])
    lines[start:end + 1] = kept
    if only:
        top = _head(lines)
        body = lines[top].rstrip("\r\n")
        lines[top] = "accounts: []" + _HEAD.match(body)["rest"] + lines[top][len(body):]
    return "".join(lines)


def _declared(old: MachinesConfig, ident: str) -> Account:
    account = next((item for item in old.accounts if item.id == ident), None)
    if account is None:
        raise Refused("unknown-account")
    if account.harness == EXTERNAL:
        raise Refused("read-only")
    return account


def _check_devices(old: MachinesConfig, harness: str, names: list[str],
                   previous: object) -> None:
    devices = [old.device(name) for name in names]
    if any(device is None for device in devices):
        raise Refused("unknown-device", previous)
    if any(harness not in device.harnesses for device in devices):
        raise Refused("harness-not-on-device", previous)


def _shown(account: Account) -> dict[str, object]:
    return {"harness": account.harness, "max_jobs": account.max_jobs,
            "paused": account.paused, "signed_in_on": sorted(account.signed_in_on)}


Plan = tuple[tuple[Account, ...], object, Callable[[str], str | None]]


def _plan_set(request: AccountRequest, old: MachinesConfig, machines: Path) -> Plan:
    account = _declared(old, request.account)
    if request.field not in FIELDS:
        raise Refused("bad-field")
    value_ok, expect_ok, attr = FIELDS[request.field]
    current = getattr(account, request.field)
    previous = sorted(current) if isinstance(current, frozenset) else current
    if not value_ok(request.value) or not expect_ok(request.expect):
        raise Refused("bad-value", previous)
    if request.field == "signed_in_on":
        _check_devices(old, account.harness, request.value, previous)
    if _overlay_sets(machines, "accounts", "id", request.account, request.field):
        raise Refused(f"{request.field} is set in machines.local.yml", previous)
    if attr(request.expect) != current:
        raise Refused("changed", previous)
    if attr(request.value) == current:
        raise Refused("no-change", previous)
    changed = replace(account, **{request.field: attr(request.value)})
    after = tuple(changed if item.id == account.id else item for item in old.accounts)
    return after, previous, lambda text: edit_account_field(
        text, request.account, request.field, request.value)


def _plan_add(request: AccountRequest, old: MachinesConfig, machines: Path) -> Plan:
    if ACCOUNT_ID.fullmatch(request.account) is None:
        raise Refused("bad-id")
    if any(item.id == request.account for item in old.accounts):
        raise Refused("exists")
    harness, max_jobs, devices = (request.value[key] for key in ADD_KEYS)
    if harness not in HARNESSES:
        raise Refused("unknown-harness")
    if (max_jobs is not None and not _is_max_jobs(max_jobs)) or not _is_devices(devices):
        raise Refused("bad-value")
    _check_devices(old, harness, devices, None)
    account = Account(request.account, harness, signed_in_on=frozenset(devices),
                      max_jobs=max_jobs)
    return (*old.accounts, account), None, lambda text: add_account(text, account)


def _plan_remove(request: AccountRequest, old: MachinesConfig, machines: Path) -> Plan:
    account = _declared(old, request.account)
    previous = _shown(account)
    expect = request.expect
    if not isinstance(expect, dict) or set(expect) != set(previous):
        raise Refused("bad-value", previous)
    devices = expect["signed_in_on"]
    if {**expect, "signed_in_on": sorted(devices) if _is_devices(devices) else devices} != previous:
        raise Refused("changed", previous)
    if _overlay_sets(machines, "accounts", "id", request.account, "id"):
        raise Refused(f"{request.account} is set in machines.local.yml", previous)
    if not account.paused:
        raise Refused("not-paused", previous)
    after = tuple(item for item in old.accounts if item.id != account.id)
    return after, previous, lambda text: remove_account(text, request.account)


class _Op(NamedTuple):
    keys: frozenset[str]
    request: Callable[[dict], tuple[object, object, object]]
    plan: Callable[[AccountRequest, MachinesConfig, Path], Plan]
    overreach: str


OPS: Mapping[str, _Op] = MappingProxyType({
    "set": _Op(account_requests.OPS["set"],
               lambda data: (data["field"], data["expect"], data["value"]),
               _plan_set, "the edit changed more than that field"),
    "add": _Op(account_requests.OPS["add"],
               lambda data: ("add", None, {key: data[key] for key in ADD_KEYS}),
               _plan_add, "the edit changed more than that account"),
    "remove": _Op(account_requests.OPS["remove"],
                  lambda data: ("remove", data["expect"], None),
                  _plan_remove, "the edit changed more than that account"),
})


def plan(request: AccountRequest, old: MachinesConfig, text: str, machines: Path,
         parse_text: Callable[[str, object], MachinesConfig],
         pairs: Callable[[MachinesConfig], list[RunningPair]]) -> tuple[str, object]:
    if request.account in {default_account(harness) for harness in HARNESSES}:
        raise Refused("read-only")
    op = OPS[request.op]
    after, previous, edit = op.plan(request, old, machines)
    reason = running_job_blocker(old.accounts, after, pairs(old))
    if reason is not None:
        raise Refused(reason, previous)
    new_text = edit(text)
    if new_text is None:
        raise Refused("cannot edit the account's block", previous)
    new = parse_text(new_text, previous)
    if (_changes(old, new) != [] or len(new.accounts) != len(after)
            or {item.id: item for item in new.accounts} != {item.id: item for item in after}):
        raise Refused(op.overreach, previous)
    return new_text, previous
