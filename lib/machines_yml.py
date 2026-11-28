"""machines.yml schema (docs/SPEC.md sections 13 and 17, F2-01).

One file lists the device pool. Each device has a name, a Tailscale
name (omitted only on the one `local: true` device that runs orch),
slot_max, and harnesses: a mapping of each eligible harness to the bot
OS user whose login Max provisioned on that device. A harness may only
map to its own bot user (claude -> claude-bot, and so on).

Every remote device names a workdir_root, the absolute directory jobs
run under on that device, and an ssh_alias: the Host alias in the box's
own ~/.ssh/config that reaches that device as one bot user. The alias
must contain the token {bot}, resolved per harness to its bot user, so
each bot logs in through its own alias (pc-wsl-{bot} -> pc-wsl-codex-bot).
No key path, user@ or ssh option is ever written here (F2-02). It may not be under /mnt/, so a WSL device
never works on a mounted Windows drive. A whole path component may be
the token {bot}, which resolves to the harness's bot user, so one device
can give each bot its own home (/home/{bot}/factory-work).

Every remote device also names a factory_root: the absolute path of that
device's deployed factory checkout (skills/, bin/orch), which briefs
rendered on the box use to name files that exist on the placed device.
It is not workdir_root, the folder job worktrees are made under, which is
empty at job start, so factory_root may not equal workdir_root or sit
inside it. It follows the workdir_root path rules, {bot} included. The
local device may name one too.

Slots are counted per device name, so no two devices may share a
tailscale_name (compared case-insensitively): that would give one host
two slot budgets.

A device schedules only when it is enabled (the default), has no
REPLACE_WITH_ placeholder, and lists the harness. A device with
`drain: true` takes no new placements and leaves its running jobs be. Unknown fields are
refused so v1 stays closed. Nothing here checks that a device is reachable. A gitignored
machines.local.yml beside the file is merged in before validation
(lib/config_overlay.py). A device's test_suite_max (default 1, a
positive integer) is the most whole local test suites that host runs at
once; only machines.local.yml may set it, so the tracked file refuses it.

The top-level orch_host names the device that runs orch. It must be a
listed device, and a `local: true` device must be it. Without it, the one
local device is the orch host; with no local device it is required. A
remote device may name an ssh_user: the login (`ssh -l`) the box uses for
it in place of the job's bot user, which stays the job's identity.

The top-level accounts list names each subscription: an id
([A-Za-z0-9._-]{1,64}, not `.` or `..`, never an email, org ID or
token), its harness (a factory harness, or `external` for a plan no
factory process runs, such as Grok's), and optional free-text plan and
label. A device's harness value is its bot user, whose own login is the
account `<harness>-default`, which needs no entry, so a file without
accounts loads unchanged.
signed_in_on lists the devices a claude or codex account is also signed
in on; each such device names home, the bot's home folder there (the
workdir_root path rules, but no {bot} beside ssh_user), and the account
runs from <home>/.factory-accounts/<id>. Every declared account other
than an external one must be signed in on some device. An optional
max_jobs must be a positive integer, and an optional paused true or
false. The older {bot, account} harness value, which names the device's
own login account, still loads with a deprecation warning, except for
cursor and gemini: each keeps one login per bot user, so their accounts are
always named as each device's own login this way. The device name
`external` is reserved for external readings.
"""

from __future__ import annotations

import posixpath
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence

import yaml

from lib.config_overlay import apply_overlay, overlay_path
from lib.load import Finding, is_strict_int, load_yaml
from lib.schema import HARNESSES

PLACEHOLDER_PREFIX = "REPLACE_WITH_"
BOT_USERS = MappingProxyType({harness: f"{harness}-bot" for harness in HARNESSES})
FORBIDDEN_WORKDIR_ROOT = "/mnt"
DEVICE_LOGIN_ONLY = frozenset({"cursor", "gemini"})
BOT_TOKEN = "{bot}"
DEFAULT_ORCH_HOST = "box"

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_TOP_KEYS = {"devices", "orch_host", "accounts"}
EXTERNAL = "external"
ACCOUNT_HARNESSES = (*HARNESSES, EXTERNAL)
ACCOUNT_ID = re.compile(r"^(?!\.\.?$)[A-Za-z0-9._-]{1,64}$")
_ACCOUNT_KEYS = {"id", "harness", "plan", "label", "signed_in_on", "max_jobs", "paused"}
ACCOUNTS_DIR = ".factory-accounts"
_ACCOUNT_TEXT_MAX = 100
_HARNESS_VALUE_KEYS = {"bot", "account"}
_DEVICE_KEYS = {
    "name",
    "tailscale_name",
    "local",
    "enabled",
    "drain",
    "slot_max",
    "test_suite_max",
    "harnesses",
    "workdir_root",
    "ssh_alias",
    "factory_root",
    "ssh_user",
    "home",
}
_OVERLAY_ONLY_DEVICE_KEYS = ("test_suite_max",)
# An ssh_alias once {bot} is resolved: a bare Host alias. No user@, no
# leading '-' (it would be read as an option), no spaces or shell syntax.
_SSH_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$")
_SSH_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def default_account(harness: str) -> str:
    """The account a bare bot-user value means."""
    return f"{harness}-default"


@dataclass(frozen=True)
class Account:
    id: str
    harness: str
    plan: str | None = None
    label: str | None = None
    signed_in_on: frozenset[str] = frozenset()
    max_jobs: int | None = None
    paused: bool = False


@dataclass(frozen=True)
class Device:
    name: str
    slot_max: int | None
    harnesses: Mapping[str, str]
    tailscale_name: str | None = None
    local: bool = False
    enabled: bool = True
    drain: bool = False
    workdir_root: str | None = None
    placeholders: tuple[str, ...] = ()
    ssh_alias: str | None = None
    factory_root: str | None = None
    ssh_user: str | None = None
    accounts: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    home: str | None = None
    test_suite_max: int = 1

    @property
    def placeholder(self) -> bool:
        return bool(self.placeholders)

    @property
    def schedulable(self) -> bool:
        return self.enabled and not self.placeholder and self.slot_max is not None

    def eligible(self, harness: str) -> bool:
        return self.schedulable and harness in self.harnesses

    def takes_new(self, harness: str) -> bool:
        return self.eligible(harness) and not self.drain

    def account_for(self, harness: str) -> str | None:
        """The account harness's login uses here; None when not listed."""
        if harness not in self.harnesses:
            return None
        return self.accounts.get(harness, default_account(harness))

    def workdir_for(self, harness: str) -> str | None:
        """workdir_root with {bot} resolved to the harness's bot user.

        None on the local device, when workdir_root is unset (placeholder),
        or when the device does not list the harness.
        """
        if self.workdir_root is None or harness not in self.harnesses:
            return None
        return self.workdir_root.replace(BOT_TOKEN, self.harnesses[harness])

    def ssh_alias_for(self, harness: str) -> str | None:
        """ssh_alias with {bot} resolved to the harness's bot user.

        None on the local device, when ssh_alias is unset (placeholder),
        or when the device does not list the harness.
        """
        if self.ssh_alias is None or harness not in self.harnesses:
            return None
        return self.ssh_alias.replace(BOT_TOKEN, self.harnesses[harness])

    def login_for(self, harness: str) -> str | None:
        """The SSH login for harness: ssh_user, else the harness's bot user."""
        return self.ssh_user or self.harnesses.get(harness)

    def factory_root_for(self, harness: str) -> str | None:
        """factory_root with {bot} resolved to the harness's bot user.

        None when factory_root is unset (or a placeholder), or when the
        device does not list the harness.
        """
        if self.factory_root is None or harness not in self.harnesses:
            return None
        return self.factory_root.replace(BOT_TOKEN, self.harnesses[harness])

    def home_for(self, harness: str) -> str | None:
        """home with {bot} resolved; None when unset or the harness is not listed."""
        if self.home is None or harness not in self.harnesses:
            return None
        return self.home.replace(BOT_TOKEN, self.harnesses[harness])


def tailscale_key(name: str) -> str:
    """One host, one key: Tailscale (DNS) names are case-insensitive."""
    return name.casefold().rstrip(".")


def shared_tailscale_names(devices: Sequence[Device]) -> list[tuple[str, ...]]:
    """Device names grouped by a tailscale_name that more than one shares."""
    groups: dict[str, list[str]] = {}
    for device in devices:
        if device.tailscale_name is not None:
            groups.setdefault(tailscale_key(device.tailscale_name), []).append(
                device.name
            )
    return [tuple(names) for names in groups.values() if len(names) > 1]


@dataclass(frozen=True)
class MachinesConfig:
    devices: tuple[Device, ...] = field(default_factory=tuple)
    orch_host: str = DEFAULT_ORCH_HOST
    accounts: tuple[Account, ...] = field(default_factory=tuple)

    def device(self, name: str) -> Device | None:
        return next((item for item in self.devices if item.name == name), None)

    def account(self, account_id: str) -> Account | None:
        """A declared account, else the implicit `<harness>-default` one."""
        found = next((item for item in self.accounts if item.id == account_id), None)
        if found is not None:
            return found
        for harness in HARNESSES:
            if account_id == default_account(harness):
                return Account(account_id, harness)
        return None

    def accounts_on(self, device_name: str, harness: str) -> tuple[str, ...]:
        """The device's own login account, then those signed in on it, in file order."""
        device = self.device(device_name)
        login = None if device is None else device.account_for(harness)
        if login is None:
            return ()
        return (
            login,
            *(
                item.id
                for item in self.accounts
                if item.harness == harness and device_name in item.signed_in_on
            ),
        )

    def account_home(self, device_name: str, harness: str, account: str) -> str | None:
        """The folder account runs from; None for the device's own login."""
        accounts = self.accounts_on(device_name, harness)
        if account not in accounts:
            raise ValueError(
                f"account {account!r} is not signed in for {harness} on {device_name!r}"
            )
        if account == accounts[0]:
            return None
        home = self.device(device_name).home_for(harness)
        if home is None:
            raise ValueError(f"device {device_name!r} home is an install placeholder")
        return posixpath.join(home, ACCOUNTS_DIR, account)

    def serving(self, harness: str) -> frozenset[str]:
        """The accounts of harness on devices that take new work."""
        return frozenset(
            account
            for device in self.devices
            if device.takes_new(harness)
            for account in self.accounts_on(device.name, harness)
        )

    def external_accounts(self) -> tuple[Account, ...]:
        return tuple(item for item in self.accounts if item.harness == EXTERNAL)

    def factory_root(self, device_name: str, harness: str) -> str | None:
        """The deployed factory checkout for harness on the named device."""
        device = self.device(device_name)
        return None if device is None else device.factory_root_for(harness)


def orch_host(config: MachinesConfig | None) -> str:
    """The device that runs orch; DEFAULT_ORCH_HOST only when machines.yml is missing or invalid."""
    return DEFAULT_ORCH_HOST if config is None else config.orch_host


def is_placeholder(value: object) -> bool:
    return isinstance(value, str) and value.startswith(PLACEHOLDER_PREFIX)


def _readable_stream_or_file(path: Path) -> bool:
    return path.exists() and not path.is_dir()


def load_machines_yml(
    path: Path,
    *,
    overlay: bool = True,
) -> tuple[MachinesConfig | None, list[Finding]]:
    if not _readable_stream_or_file(path):
        return None, [Finding("error", "file not found")]
    text = path.read_text(encoding="utf-8")
    try:
        data = load_yaml(text)
    except yaml.YAMLError as exc:
        text = " ".join(line.strip() for line in str(exc).splitlines() if line.strip())
        return None, [Finding("error", f"invalid YAML: {text or 'invalid YAML'}")]
    if not isinstance(data, dict):
        return None, [Finding("error", "machines.yml must be a mapping")]
    refusals = _overlay_only_refusals(path, data)
    if overlay:
        data, findings = apply_overlay(path, data)
        if data is None:
            return None, refusals + findings
    config, findings = _validate(data)
    if refusals:
        return None, refusals + findings
    return config, findings


def _overlay_only_refusals(path: Path, data: dict) -> list[Finding]:
    devices = data.get("devices")
    if not isinstance(devices, list):
        return []
    local = overlay_path(path).name
    return [
        Finding("error", f"devices[{i}].{key} is set per host: move it to {local}")
        for i, item in enumerate(devices)
        if isinstance(item, dict)
        for key in _OVERLAY_ONLY_DEVICE_KEYS
        if key in item
    ]


def _validate(data: dict) -> tuple[MachinesConfig | None, list[Finding]]:
    findings: list[Finding] = []
    for key in sorted(data, key=lambda item: str(item)):
        if key not in _TOP_KEYS:
            findings.append(Finding("error", f"unknown field {str(key)!r}"))
    if "devices" not in data:
        findings.append(Finding("error", "missing required field 'devices'"))
        return None, findings
    value = data["devices"]
    if not isinstance(value, list) or not value:
        findings.append(Finding("error", "devices must list at least one device"))
        return None, findings

    accounts, account_positions = _accounts(data.get("accounts", []), findings)
    devices: list[Device] = []
    device_positions: dict[str, int] = {}
    seen: set[str] = set()
    local_names: list[str] = []
    for index, item in enumerate(value):
        device = _device(item, f"devices[{index}]", findings, accounts)
        if device is None:
            continue
        if device.name in seen:
            findings.append(Finding("error", f"duplicate device {device.name!r}"))
            continue
        seen.add(device.name)
        device_positions[device.name] = index
        if device.local:
            local_names.append(device.name)
        devices.append(device)
    for names in shared_tailscale_names(devices):
        findings.append(
            Finding(
                "error",
                f"devices {', '.join(names)} share one tailscale_name; "
                "one host has one slot_max, so list it once",
            )
        )
    if len(local_names) > 1:
        findings.append(
            Finding(
                "error",
                "only one device may be local; got " + ", ".join(local_names),
            )
        )
    _check_sign_ins(accounts, account_positions, devices, device_positions, findings)
    if any(item.severity == "error" for item in findings):
        return None, findings
    host = _orch_host(data, seen, local_names, findings)
    if host is None or any(item.severity == "error" for item in findings):
        return None, findings
    return (
        MachinesConfig(
            devices=tuple(devices), orch_host=host, accounts=tuple(accounts.values())
        ),
        findings,
    )


def _accounts(
    value: object, findings: list[Finding]
) -> tuple[dict[str, Account], dict[str, int]]:
    if not isinstance(value, list):
        findings.append(Finding("error", "accounts must be a list of accounts"))
        return {}, {}
    accounts: dict[str, Account] = {}
    positions: dict[str, int] = {}
    for index, item in enumerate(value):
        prefix = f"accounts[{index}]"
        if not isinstance(item, dict):
            findings.append(Finding("error", f"{prefix} must be a mapping"))
            continue
        errors_before = _error_count(findings)
        for key in sorted(item, key=lambda entry: str(entry)):
            if key not in _ACCOUNT_KEYS:
                findings.append(Finding("error", f"unknown field '{prefix}.{key}'"))
        account_id = item.get("id")
        if not isinstance(account_id, str) or ACCOUNT_ID.fullmatch(account_id) is None:
            findings.append(
                Finding(
                    "error",
                    f"{prefix}.id must be 1-64 letters, digits, '.', '_' or '-' "
                    "(a stable name, never an email, org ID or token)",
                )
            )
        harness = item.get("harness")
        if harness not in ACCOUNT_HARNESSES:
            findings.append(
                Finding(
                    "error",
                    f"{prefix}.harness must be one of {', '.join(ACCOUNT_HARNESSES)}",
                )
            )
        for key in ("plan", "label"):
            text = item.get(key)
            if text is not None and not _account_text(text):
                findings.append(
                    Finding(
                        "error",
                        f"{prefix}.{key} must be text of at most "
                        f"{_ACCOUNT_TEXT_MAX} printable characters",
                    )
                )
        signed_in_on = item.get("signed_in_on", [])
        if not (
            isinstance(signed_in_on, list)
            and all(isinstance(name, str) and _NAME.fullmatch(name) for name in signed_in_on)
            and len(set(signed_in_on)) == len(signed_in_on)
        ):
            findings.append(
                Finding("error", f"{prefix}.signed_in_on must list device names")
            )
        elif signed_in_on and harness == EXTERNAL:
            findings.append(
                Finding(
                    "error",
                    f"{prefix}.signed_in_on must be omitted: "
                    "an external account is not signed in on a device",
                )
            )
        elif signed_in_on and harness in DEVICE_LOGIN_ONLY:
            findings.append(
                Finding(
                    "error",
                    f"{prefix}.signed_in_on is not supported for a {harness} account: "
                    f"{harness.capitalize()} keeps its login outside any folder the factory can set, "
                    f"so name the account as each device's {harness} login",
                )
            )
        max_jobs = item.get("max_jobs")
        if "max_jobs" in item and not (is_strict_int(max_jobs) and max_jobs >= 1):
            findings.append(
                Finding("error", f"{prefix}.max_jobs must be a positive integer")
            )
        paused = _bool(item, "paused", False, prefix, findings)
        if _error_count(findings) > errors_before:
            continue
        if account_id in accounts:
            findings.append(Finding("error", f"duplicate account {account_id!r}"))
            continue
        positions[account_id] = index
        accounts[account_id] = Account(
            account_id,
            harness,
            item.get("plan"),
            item.get("label"),
            frozenset(signed_in_on),
            max_jobs,
            paused,
        )
    return accounts, positions


def _check_sign_ins(
    accounts: Mapping[str, Account],
    account_positions: Mapping[str, int],
    devices: Sequence[Device],
    device_positions: Mapping[str, int],
    findings: list[Finding],
) -> None:
    by_name = {device.name: device for device in devices}
    needs_home: dict[str, str] = {}
    for account in accounts.values():
        prefix = f"accounts[{account_positions[account.id]}].signed_in_on"
        for name in sorted(account.signed_in_on):
            device = by_name.get(name)
            if device is None:
                findings.append(
                    Finding("error", f"{prefix} names unknown device {name!r}")
                )
            elif "harnesses" in device.placeholders:
                continue
            elif account.harness not in device.harnesses:
                findings.append(
                    Finding(
                        "error",
                        f"{prefix} names device {name!r}, but device {name!r} "
                        f"does not list harness {account.harness!r}",
                    )
                )
            elif device.account_for(account.harness) == account.id:
                findings.append(
                    Finding(
                        "error",
                        f"{prefix} names device {name!r}, whose {account.harness} "
                        f"login is already {account.id!r}; declare an account "
                        "on a device once",
                    )
                )
            else:
                needs_home.setdefault(name, account.id)
    for device in devices:
        account_id = needs_home.get(device.name)
        if account_id is not None and device.home is None and "home" not in device.placeholders:
            findings.append(
                Finding(
                    "error",
                    f"devices[{device_positions[device.name]}].home is required: "
                    f"account {account_id!r} is signed in on it",
                )
            )
    for account in accounts.values():
        if account.harness == EXTERNAL or account.signed_in_on:
            continue
        if not any(device.account_for(account.harness) == account.id for device in devices):
            findings.append(
                Finding(
                    "error",
                    f"account {account.id!r} is not signed in on any device; "
                    + (
                        f"name it as a device's {account.harness} login"
                        if account.harness in DEVICE_LOGIN_ONLY
                        else "list it in signed_in_on"
                    ),
                )
            )


def _account_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= _ACCOUNT_TEXT_MAX
        and all(unicodedata.category(char)[0] != "C" for char in value)
    )


def _harness_value(
    harness: str,
    raw: object,
    prefix: str,
    accounts: Mapping[str, Account],
    findings: list[Finding],
) -> tuple[str, str] | None:
    qualified = f"{prefix}.harnesses.{harness}"
    account_id = default_account(harness)
    user = raw
    if isinstance(raw, dict):
        for key in sorted(raw, key=lambda entry: str(entry)):
            if key not in _HARNESS_VALUE_KEYS:
                findings.append(Finding("error", f"unknown field '{qualified}.{key}'"))
                return None
        user = raw.get("bot")
        account_id = raw.get("account", account_id)
        if not isinstance(account_id, str):
            findings.append(Finding("error", f"{qualified}.account must be an account id"))
            return None
    expected = BOT_USERS[harness]
    if user != expected:
        findings.append(
            Finding(
                "error",
                f"{qualified} must be {expected!r}, "
                "the bot user Max provisioned for that harness",
            )
        )
        return None
    account = accounts.get(account_id)
    if account is None and account_id in {default_account(name) for name in HARNESSES}:
        account = Account(account_id, account_id.removesuffix("-default"))
    if account is None:
        findings.append(
            Finding(
                "error",
                f"{qualified} names unknown account {account_id!r}; "
                "declare it under accounts",
            )
        )
        return None
    if account.harness != harness:
        findings.append(
            Finding(
                "error",
                f"{qualified} names account {account.id!r} of harness "
                f"{account.harness!r}; a device uses only its own harness's accounts",
            )
        )
        return None
    return user, account.id


def _orch_host(
    data: dict, names: set[str], local_names: list[str], findings: list[Finding]
) -> str | None:
    if "orch_host" not in data:
        if len(local_names) == 1:
            return local_names[0]
        if not local_names:
            findings.append(
                Finding("error", "orch_host is required when no device is local")
            )
        return None
    host = data["orch_host"]
    if not isinstance(host, str) or _NAME.fullmatch(host) is None:
        findings.append(Finding("error", "orch_host must be a device name"))
        return None
    if host not in names:
        findings.append(Finding("error", f"orch_host {host!r} is not a listed device"))
        return None
    for name in local_names:
        if name != host:
            findings.append(
                Finding("error", f"local device {name!r} must be orch_host {host!r}")
            )
    return host


def _device(
    item: object,
    prefix: str,
    findings: list[Finding],
    accounts: Mapping[str, Account],
) -> Device | None:
    if not isinstance(item, dict):
        findings.append(Finding("error", f"{prefix} must be a mapping"))
        return None
    errors_before = _error_count(findings)
    for key in sorted(item, key=lambda entry: str(entry)):
        if key not in _DEVICE_KEYS:
            findings.append(Finding("error", f"unknown field '{prefix}.{key}'"))

    name = item.get("name")
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        findings.append(
            Finding(
                "error",
                f"{prefix}.name must be a lowercase device name "
                "(letters, digits and '-')",
            )
        )
        return None
    if name == EXTERNAL:
        findings.append(
            Finding(
                "error",
                f"{prefix}.name {EXTERNAL!r} is reserved for external usage readings",
            )
        )
        return None
    placeholders: list[str] = []

    def placeholder(field_name: str) -> None:
        qualified = f"{prefix}.{field_name}"
        findings.append(
            Finding(
                "warning",
                f"skipping device {name!r}: {qualified} is an install placeholder; "
                "it will not be scheduled",
            )
        )
        placeholders.append(field_name)

    local = _bool(item, "local", False, prefix, findings)
    enabled = _bool(item, "enabled", True, prefix, findings)
    if not enabled:
        findings.append(
            Finding(
                "warning",
                f"device {name!r} is disabled (enabled: false); "
                "it will not be scheduled",
            )
        )
    drain = _bool(item, "drain", False, prefix, findings)
    if drain:
        findings.append(
            Finding(
                "warning",
                f"device {name!r} is draining (drain: true); "
                "it takes no new jobs and its running jobs finish",
            )
        )

    tailscale_name: str | None = None
    raw_tailscale = item.get("tailscale_name")
    if local:
        if "tailscale_name" in item:
            findings.append(
                Finding(
                    "error",
                    f"{prefix}.tailscale_name must be omitted on the local device",
                )
            )
    elif "tailscale_name" not in item:
        findings.append(
            Finding(
                "error",
                f"missing required field '{prefix}.tailscale_name' "
                "(only the local device omits it)",
            )
        )
    elif is_placeholder(raw_tailscale):
        placeholder("tailscale_name")
    elif _is_tailscale_name(raw_tailscale):
        tailscale_name = str(raw_tailscale)
    else:
        findings.append(
            Finding(
                "error",
                f"{prefix}.tailscale_name must be a Tailscale device name, "
                "not an IP address or URL",
            )
        )

    workdir_root: str | None = None
    raw_workdir = item.get("workdir_root")
    if "workdir_root" not in item:
        if not local:
            findings.append(
                Finding(
                    "error",
                    f"missing required field '{prefix}.workdir_root' "
                    "(every remote device names where jobs run)",
                )
            )
    elif is_placeholder(raw_workdir):
        placeholder("workdir_root")
    else:
        problem = workdir_problem(raw_workdir)
        if problem is None:
            workdir_root = str(raw_workdir)
        else:
            findings.append(Finding("error", f"{prefix}.workdir_root {problem}"))

    factory_root: str | None = None
    raw_factory = item.get("factory_root")
    if "factory_root" not in item:
        if not local:
            findings.append(
                Finding(
                    "error",
                    f"missing required field '{prefix}.factory_root' "
                    "(every remote device names its deployed factory checkout, "
                    "not workdir_root)",
                )
            )
    elif is_placeholder(raw_factory):
        placeholder("factory_root")
    else:
        problem = workdir_problem(raw_factory)
        if problem is None:
            factory_root = str(raw_factory)
        else:
            findings.append(Finding("error", f"{prefix}.factory_root {problem}"))

    ssh_alias: str | None = None
    raw_alias = item.get("ssh_alias")
    if local:
        if "ssh_alias" in item:
            findings.append(
                Finding(
                    "error",
                    f"{prefix}.ssh_alias must be omitted on the local device",
                )
            )
    elif "ssh_alias" not in item:
        findings.append(
            Finding(
                "error",
                f"missing required field '{prefix}.ssh_alias' "
                "(every remote device names the box's SSH alias, with {bot})",
            )
        )
    elif is_placeholder(raw_alias):
        placeholder("ssh_alias")
    else:
        problem = _ssh_alias_problem(raw_alias)
        if problem is None:
            ssh_alias = str(raw_alias)
        else:
            findings.append(Finding("error", f"{prefix}.ssh_alias {problem}"))

    ssh_user: str | None = None
    raw_user = item.get("ssh_user")
    if "ssh_user" in item and local:
        findings.append(
            Finding("error", f"{prefix}.ssh_user must be omitted on the local device")
        )
    elif isinstance(raw_user, str) and _SSH_USER.fullmatch(raw_user):
        ssh_user = raw_user
    elif "ssh_user" in item:
        findings.append(
            Finding(
                "error",
                f"{prefix}.ssh_user must be a login name "
                "(lowercase letters, digits, '_' and '-', at most 32)",
            )
        )

    home: str | None = None
    raw_home = item.get("home")
    if is_placeholder(raw_home):
        placeholder("home")
    elif "home" in item:
        problem = workdir_problem(raw_home)
        if problem is None and ssh_user is not None and BOT_TOKEN in raw_home:
            problem = (
                f"may not use '{BOT_TOKEN}' when ssh_user is set: every bot logs "
                f"in as {ssh_user!r} there, so '{BOT_TOKEN}' names no bot's home"
            )
        if problem is None:
            home = raw_home
        else:
            findings.append(Finding("error", f"{prefix}.home {problem}"))

    slot_max: int | None = None
    raw_slots = item.get("slot_max")
    if "slot_max" not in item:
        findings.append(Finding("error", f"missing required field '{prefix}.slot_max'"))
    elif is_placeholder(raw_slots):
        placeholder("slot_max")
    elif is_strict_int(raw_slots) and raw_slots >= 1:
        slot_max = int(raw_slots)
    else:
        findings.append(
            Finding("error", f"{prefix}.slot_max must be a positive integer")
        )

    test_suite_max = item.get("test_suite_max", 1)
    if not (is_strict_int(test_suite_max) and test_suite_max >= 1):
        findings.append(
            Finding("error", f"{prefix}.test_suite_max must be a positive integer")
        )

    harnesses: dict[str, str] = {}
    device_accounts: dict[str, str] = {}
    raw_harnesses = item.get("harnesses")
    if "harnesses" not in item:
        findings.append(
            Finding("error", f"missing required field '{prefix}.harnesses'")
        )
    elif is_placeholder(raw_harnesses):
        placeholder("harnesses")
    elif not isinstance(raw_harnesses, dict):
        findings.append(
            Finding(
                "error",
                f"{prefix}.harnesses must map each eligible harness to its bot user",
            )
        )
    else:
        for harness in sorted(raw_harnesses, key=lambda entry: str(entry)):
            user = raw_harnesses[harness]
            if harness not in HARNESSES:
                findings.append(
                    Finding(
                        "error",
                        f"{prefix}.harnesses has unknown harness {str(harness)!r}; "
                        f"expected one of {', '.join(HARNESSES)}",
                    )
                )
                continue
            resolved = _harness_value(harness, user, prefix, accounts, findings)
            if resolved is None:
                continue
            harnesses[harness], device_accounts[harness] = resolved
            if isinstance(user, dict) and "account" in user and harness not in DEVICE_LOGIN_ONLY:
                findings.append(
                    Finding(
                        "warning",
                        f"device {name!r} harness {harness!r} names its login account "
                        "inline; {bot, account} is deprecated, but keep {bot, account} "
                        "until account placement (#1063 parts 2 and 3, then #1064) "
                        "runs on every device, then move the account to "
                        "accounts[].signed_in_on",
                    )
                )
        if not raw_harnesses and enabled:
            findings.append(
                Finding(
                    "error",
                    f"{prefix}.harnesses is empty; an enabled device must list "
                    "at least one provisioned harness",
                )
            )

    if (
        factory_root is not None
        and workdir_root is not None
        and _is_within_for_any_bot(factory_root, workdir_root, harnesses.values())
    ):
        findings.append(
            Finding(
                "error",
                f"{prefix}.factory_root must not be workdir_root or inside it "
                "(it is the deployed factory checkout, not where jobs run)",
            )
        )

    if _error_count(findings) > errors_before:
        return None
    return Device(
        name=name,
        slot_max=slot_max,
        harnesses=MappingProxyType(dict(harnesses)),
        tailscale_name=tailscale_name,
        local=local,
        enabled=enabled,
        drain=drain,
        workdir_root=workdir_root,
        placeholders=tuple(placeholders),
        ssh_alias=ssh_alias,
        factory_root=factory_root,
        ssh_user=ssh_user,
        accounts=MappingProxyType(device_accounts),
        home=home,
        test_suite_max=test_suite_max,
    )


def _bool(
    item: dict,
    key: str,
    default: bool,
    prefix: str,
    findings: list[Finding],
) -> bool:
    value = item.get(key, default)
    if not isinstance(value, bool):
        findings.append(Finding("error", f"{prefix}.{key} must be true or false"))
        return default
    return value


def _is_tailscale_name(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > 253:
        return False
    labels = value.split(".")
    if any(_LABEL.fullmatch(label) is None for label in labels):
        return False
    return not all(label.isdigit() for label in labels)


def workdir_problem(value: object) -> str | None:
    if not isinstance(value, str) or not value.startswith("/"):
        return "must be an absolute POSIX path"
    if any(unicodedata.category(char) == "Cc" for char in value):
        return "must not contain NUL or another control character"
    if "`" in value:
        return "must not contain a backtick"
    for part in value.split("/"):
        if BOT_TOKEN in part and part != BOT_TOKEN:
            return f"'{BOT_TOKEN}' must be a whole path component"
        if part != BOT_TOKEN and ("{" in part or "}" in part):
            return f"only '{BOT_TOKEN}' may appear in braces"
    if any(part in (".", "..") for part in value.split("/")):
        return "must not contain '.' or '..'"
    if posixpath.normpath(value) != value or value == "/" or value.startswith("//"):
        return "must be a normalized directory below /"
    if value == FORBIDDEN_WORKDIR_ROOT or value.startswith(FORBIDDEN_WORKDIR_ROOT + "/"):
        return "must not be under /mnt/ (a mounted Windows drive on WSL)"
    return None


def _is_within_for_any_bot(path: str, root: str, bots) -> bool:
    for bot in (BOT_TOKEN, *bots):
        resolved_path = path.replace(BOT_TOKEN, bot)
        resolved_root = root.replace(BOT_TOKEN, bot)
        if resolved_path == resolved_root or resolved_path.startswith(
            resolved_root + "/"
        ):
            return True
    return False


def _ssh_alias_problem(value: object) -> str | None:
    if not isinstance(value, str) or BOT_TOKEN not in value:
        return (
            f"must contain '{BOT_TOKEN}' so each bot logs in through its own "
            "alias (for example pc-wsl-{bot})"
        )
    if _SSH_ALIAS.fullmatch(value.replace(BOT_TOKEN, "bot")) is None:
        return (
            "must be a bare SSH Host alias from the box's ~/.ssh/config "
            "(letters, digits, '.', '_' and '-'; no user@, option or space)"
        )
    return None


def _error_count(findings: list[Finding]) -> int:
    return sum(1 for item in findings if item.severity == "error")
