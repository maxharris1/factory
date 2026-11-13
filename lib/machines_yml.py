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

Slots are counted per device name, so no two devices may share a
tailscale_name (compared case-insensitively): that would give one host
two slot budgets.

A device schedules only when it is enabled (the default), has no
REPLACE_WITH_ placeholder, and lists the harness. check-config refuses
placeholders unless --allow-placeholders is passed, then names each one;
it always names a disabled device. Unknown fields are refused so v1
stays closed. Nothing here checks that a device is reachable. A gitignored
machines.local.yml beside the file is merged in before validation
(lib/config_overlay.py).
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

from lib.config_overlay import apply_overlay
from lib.load import Finding, is_strict_int, load_yaml
from lib.schema import HARNESSES

PLACEHOLDER_PREFIX = "REPLACE_WITH_"
BOT_USERS = MappingProxyType({harness: f"{harness}-bot" for harness in HARNESSES})
FORBIDDEN_WORKDIR_ROOT = "/mnt"
BOT_TOKEN = "{bot}"

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_TOP_KEYS = {"devices"}
_DEVICE_KEYS = {
    "name",
    "tailscale_name",
    "local",
    "enabled",
    "slot_max",
    "harnesses",
    "workdir_root",
    "ssh_alias",
}
# An ssh_alias once {bot} is resolved: a bare Host alias. No user@, no
# leading '-' (it would be read as an option), no spaces or shell syntax.
_SSH_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$")


@dataclass(frozen=True)
class Device:
    name: str
    slot_max: int | None
    harnesses: Mapping[str, str]
    tailscale_name: str | None = None
    local: bool = False
    enabled: bool = True
    workdir_root: str | None = None
    placeholders: tuple[str, ...] = ()
    ssh_alias: str | None = None

    @property
    def placeholder(self) -> bool:
        return bool(self.placeholders)

    @property
    def schedulable(self) -> bool:
        return self.enabled and not self.placeholder and self.slot_max is not None

    def eligible(self, harness: str) -> bool:
        return self.schedulable and harness in self.harnesses

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

    def device(self, name: str) -> Device | None:
        return next((item for item in self.devices if item.name == name), None)


def is_placeholder(value: object) -> bool:
    return isinstance(value, str) and value.startswith(PLACEHOLDER_PREFIX)


def load_machines_yml(
    path: Path,
    *,
    allow_placeholders: bool = False,
    overlay: bool = True,
) -> tuple[MachinesConfig | None, list[Finding]]:
    if not path.is_file():
        return None, [Finding("error", "file not found")]
    text = path.read_text(encoding="utf-8")
    try:
        data = load_yaml(text)
    except yaml.YAMLError as exc:
        text = " ".join(line.strip() for line in str(exc).splitlines() if line.strip())
        return None, [Finding("error", f"invalid YAML: {text or 'invalid YAML'}")]
    if not isinstance(data, dict):
        return None, [Finding("error", "machines.yml must be a mapping")]
    if overlay:
        data, findings = apply_overlay(path, data)
        if data is None:
            return None, findings
    return _validate(data, allow_placeholders=allow_placeholders)


def _validate(
    data: dict,
    *,
    allow_placeholders: bool,
) -> tuple[MachinesConfig | None, list[Finding]]:
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

    devices: list[Device] = []
    seen: set[str] = set()
    local_names: list[str] = []
    for index, item in enumerate(value):
        device = _device(
            item,
            f"devices[{index}]",
            findings,
            allow_placeholders=allow_placeholders,
        )
        if device is None:
            continue
        if device.name in seen:
            findings.append(Finding("error", f"duplicate device {device.name!r}"))
            continue
        seen.add(device.name)
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
    if any(item.severity == "error" for item in findings):
        return None, findings
    return MachinesConfig(devices=tuple(devices)), findings


def _device(
    item: object,
    prefix: str,
    findings: list[Finding],
    *,
    allow_placeholders: bool,
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
    placeholders: list[str] = []

    def placeholder(field_name: str) -> None:
        qualified = f"{prefix}.{field_name}"
        if allow_placeholders:
            findings.append(
                Finding(
                    "warning",
                    f"accepted placeholder {qualified}; "
                    f"device {name!r} will not be scheduled",
                )
            )
        else:
            findings.append(
                Finding(
                    "error",
                    f"{qualified} is an install placeholder; "
                    "Max must provide this inventory",
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
        problem = _workdir_problem(raw_workdir)
        if problem is None:
            workdir_root = str(raw_workdir)
        else:
            findings.append(Finding("error", f"{prefix}.workdir_root {problem}"))

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

    harnesses: dict[str, str] = {}
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
            expected = BOT_USERS[harness]
            if user != expected:
                findings.append(
                    Finding(
                        "error",
                        f"{prefix}.harnesses.{harness} must be {expected!r}, "
                        "the bot user Max provisioned for that harness",
                    )
                )
                continue
            harnesses[harness] = user
        if not raw_harnesses and enabled:
            findings.append(
                Finding(
                    "error",
                    f"{prefix}.harnesses is empty; an enabled device must list "
                    "at least one provisioned harness",
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
        workdir_root=workdir_root,
        placeholders=tuple(placeholders),
        ssh_alias=ssh_alias,
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


def _workdir_problem(value: object) -> str | None:
    if not isinstance(value, str) or not value.startswith("/"):
        return "must be an absolute POSIX path"
    if any(unicodedata.category(char) == "Cc" for char in value):
        return "must not contain NUL or another control character"
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
