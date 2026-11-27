"""The optional `dashboard` section of orch.yml.

dashboard:
  refresh_seconds: 20                  # index.html auto-refresh, 15 to 30
  push:
    transport: none                    # none (default) or ssh
    host: pc-wsl-cursor-bot            # ssh only: an SSH alias or tailnet name
    ssh_config: /home/box/.ssh/config  # ssh only, optional: passed as ssh -F
    remote_dir: factory-dashboard      # ssh only: relative to the remote
                                       #   login's home (the default), or absolute
    method: scp                        # ssh only: rsync (default) or scp
    every_minutes: 5                   # ssh only, optional: `orch poll` pushes
                                       #   at most every N minutes (1 to 1440);
                                       #   omitted, it pushes after every cycle
    live: true                         # ssh only, optional (default false):
                                       #   also push status.json whenever
                                       #   orch.db changes, at most every 5 s
                                       #   and 5 s after each push ends
    qa_budget_gib: 5                   # ssh only, optional: past this many GiB
                                       #   of QA evidence on the PC, the oldest
                                       #   closed tickets' runs are removed;
                                       #   omitted, none are removed for budget
    interval_seconds: 20               # `orch dashboard push` loop, 15 to 30
  settings:                            # optional: Mission Control device settings
    spool: /home/dash/settings-spool   # absolute: the :8090 server's request
                                       #   spool, on dashboard.push.host when
                                       #   push is ssh, else on this host
    spool_owner: dash                  # the OS user the server runs as; only
                                       #   request files it owns are read
    approver: maxharris1               # the GitHub login whose requests apply
    relay: /home/dash/signin-relay     # optional: the sign-in relay directory
                                       #   (lib/signin_relay.py); without it
                                       #   dashboard sign-in is off

Without `settings`, `orch settings apply` does nothing (lib/settings_apply.py).
The section holds no secrets. The ssh transport relies on the SSH login
Max provisioned; `ssh_config` names his ssh_config file, never a key, and
nothing here names a key or password.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lib.load import Finding, is_strict_int

REFRESH_MIN = 15
REFRESH_MAX = 30
REFRESH_DEFAULT = 20
TRANSPORTS = ("none", "ssh")
METHODS = ("rsync", "scp")

# An SSH alias or DNS name only: no user@, no port, no leading dash.
HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})(?:\.[A-Za-z0-9-]{1,63})*$")
# Absolute, or relative to the remote login's home; conservative characters
# only and no leading '-', so no remote shell or option parser can misread it.
REMOTE_DIR = re.compile(r"^[A-Za-z0-9._/][A-Za-z0-9._/-]*$")
# The local ssh_config file handed to ssh/scp as -F: absolute, plain.
SSH_CONFIG = re.compile(r"^/[A-Za-z0-9._/-]+$")
# A file name that looks like a private or public key, not an ssh_config.
KEY_NAME = re.compile(r"^(id_.*|.*\.(pem|key|pub|ppk))$", re.IGNORECASE)
PLACEHOLDER = "REPLACE_WITH_"
DEFAULT_REMOTE_DIR = "factory-dashboard"
EVERY_MINUTES_MAX = 1440

_DASHBOARD_KEYS = {"refresh_seconds", "push", "settings"}
_SETTINGS_KEYS = {"spool", "spool_owner", "approver", "relay"}
OS_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
GITHUB_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
BOT_LOGINS = frozenset({"claude-bot", "codex-bot", "cursor-bot", "gemini-bot", "grok", "grok-bot"})
_PUSH_KEYS = {
    "transport",
    "host",
    "ssh_config",
    "remote_dir",
    "method",
    "every_minutes",
    "interval_seconds",
    "live",
    "qa_budget_gib",
}
_SSH_ONLY = ("host", "ssh_config", "remote_dir", "method", "every_minutes", "live", "qa_budget_gib")


@dataclass(frozen=True)
class PushConfig:
    transport: str = "none"
    host: str | None = None
    remote_dir: str | None = None
    method: str = "rsync"
    interval_seconds: int = REFRESH_DEFAULT
    ssh_config: str | None = None
    # None: `orch poll` pushes after every cycle.
    every_minutes: int | None = None
    # True: `orch poll` also pushes status.json whenever orch.db changes.
    live: bool = False
    qa_budget_gib: int | None = None


@dataclass(frozen=True)
class SettingsConfig:
    spool: str
    spool_owner: str
    approver: str
    relay: str | None = None


@dataclass(frozen=True)
class DashboardConfig:
    refresh_seconds: int = REFRESH_DEFAULT
    push: PushConfig = field(default_factory=PushConfig)
    settings: SettingsConfig | None = None


def is_bot_login(login: str) -> bool:
    return login.endswith("[bot]") or login.casefold() in BOT_LOGINS


def in_range(value: object) -> bool:
    return is_strict_int(value) and REFRESH_MIN <= value <= REFRESH_MAX


def valid_host(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= 253
        and HOST.fullmatch(value) is not None
    )


def _plain_parts(value: str) -> bool:
    parts = [part for part in value.split("/") if part]
    return bool(parts) and all(part not in (".", "..") for part in parts)


def valid_remote_dir(value: object) -> bool:
    """Absolute, or relative to the remote login's home directory."""
    if not isinstance(value, str) or REMOTE_DIR.fullmatch(value) is None:
        return False
    return _plain_parts(value)


def valid_ssh_config(value: object) -> bool:
    if not isinstance(value, str) or SSH_CONFIG.fullmatch(value) is None:
        return False
    return _plain_parts(value)


def looks_like_key(value: str) -> bool:
    return KEY_NAME.fullmatch(value.rstrip("/").rsplit("/", 1)[-1]) is not None


def valid_every_minutes(value: object) -> bool:
    return is_strict_int(value) and 1 <= value <= EVERY_MINUTES_MAX


def valid_qa_budget(value: object) -> bool:
    return is_strict_int(value) and value >= 1


def parse_dashboard(
    value: object,
    findings: list[Finding],
) -> DashboardConfig | None:
    """Validate the section; return None and add errors when it is invalid."""
    start = len(findings)
    if value is None:
        return DashboardConfig()
    if not isinstance(value, dict):
        findings.append(Finding("error", "dashboard must be a mapping"))
        return None
    _unknown(value, _DASHBOARD_KEYS, "dashboard", findings)
    refresh = value.get("refresh_seconds", REFRESH_DEFAULT)
    if not in_range(refresh):
        findings.append(
            Finding(
                "error",
                "dashboard.refresh_seconds must be an integer from 15 to 30",
            )
        )
    push = _push(value.get("push"), findings)
    settings = _settings(value.get("settings"), findings)
    if len(findings) > start and any(
        item.severity == "error" for item in findings[start:]
    ):
        return None
    assert push is not None
    return DashboardConfig(refresh_seconds=int(refresh), push=push, settings=settings)


def _settings(value: object, findings: list[Finding]) -> SettingsConfig | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        findings.append(Finding("error", "dashboard.settings must be a mapping"))
        return None
    start = len(findings)
    _unknown(value, _SETTINGS_KEYS, "dashboard.settings", findings)
    spool = value.get("spool")
    owner = value.get("spool_owner")
    approver = value.get("approver")
    relay = value.get("relay")
    for key in ("spool", "spool_owner", "approver", "relay"):
        item = value.get(key)
        if item is None:
            if key != "relay":
                findings.append(Finding("error", f"dashboard.settings.{key} is required"))
        elif isinstance(item, str) and PLACEHOLDER in item:
            findings.append(
                Finding("error", f"dashboard.settings.{key} is a REPLACE_WITH_ placeholder")
            )
    if len(findings) > start:
        return None
    if not valid_ssh_config(spool):
        findings.append(
            Finding(
                "error",
                "dashboard.settings.spool must be an absolute path "
                "of letters, digits, '.', '_', '-' and '/'",
            )
        )
    if relay is not None and not valid_ssh_config(relay):
        findings.append(
            Finding(
                "error",
                "dashboard.settings.relay must be an absolute path "
                "of letters, digits, '.', '_', '-' and '/'",
            )
        )
    if not isinstance(owner, str) or OS_USER.fullmatch(owner) is None:
        findings.append(Finding("error", "dashboard.settings.spool_owner must be an OS user name"))
    if not isinstance(approver, str) or GITHUB_LOGIN.fullmatch(approver) is None:
        findings.append(Finding("error", "dashboard.settings.approver must be a GitHub login"))
    elif is_bot_login(approver):
        findings.append(
            Finding("error", "dashboard.settings.approver must be a person, not a bot login")
        )
    if len(findings) > start:
        return None
    return SettingsConfig(spool=spool, spool_owner=owner, approver=approver, relay=relay)


def _push(value: object, findings: list[Finding]) -> PushConfig | None:
    if value is None:
        return PushConfig()
    if not isinstance(value, dict):
        findings.append(Finding("error", "dashboard.push must be a mapping"))
        return None
    _unknown(value, _PUSH_KEYS, "dashboard.push", findings)
    transport = value.get("transport", "none")
    interval = value.get("interval_seconds", REFRESH_DEFAULT)
    if not in_range(interval):
        findings.append(
            Finding(
                "error",
                "dashboard.push.interval_seconds must be an integer from 15 to 30",
            )
        )
    if transport not in TRANSPORTS:
        findings.append(
            Finding("error", "dashboard.push.transport must be none or ssh")
        )
        return None
    if transport == "none":
        for key in _SSH_ONLY:
            if key in value:
                findings.append(
                    Finding(
                        "error",
                        f"dashboard.push.{key} is only for transport ssh",
                    )
                )
        return PushConfig(interval_seconds=interval if in_range(interval) else REFRESH_DEFAULT)

    host = value.get("host")
    remote_dir = value.get("remote_dir", DEFAULT_REMOTE_DIR)
    ssh_config = value.get("ssh_config")
    every_minutes = value.get("every_minutes")
    method = value.get("method", "rsync")
    live = value.get("live", False)
    qa_budget_gib = value.get("qa_budget_gib")
    if qa_budget_gib is not None and not valid_qa_budget(qa_budget_gib):
        findings.append(
            Finding("error", "dashboard.push.qa_budget_gib must be a positive integer")
        )
    if not isinstance(live, bool):
        findings.append(
            Finding("error", "dashboard.push.live must be true or false")
        )
    if host is None:
        findings.append(Finding("error", "dashboard.push.host is required for ssh"))
    elif isinstance(host, str) and PLACEHOLDER in host:
        findings.append(
            Finding("error", "dashboard.push.host is a REPLACE_WITH_ placeholder")
        )
    elif not valid_host(host):
        findings.append(
            Finding(
                "error",
                "dashboard.push.host must be an SSH host alias or tailnet name",
            )
        )
    if isinstance(remote_dir, str) and PLACEHOLDER in remote_dir:
        findings.append(
            Finding(
                "error",
                "dashboard.push.remote_dir is a REPLACE_WITH_ placeholder",
            )
        )
    elif not valid_remote_dir(remote_dir):
        findings.append(
            Finding(
                "error",
                "dashboard.push.remote_dir must be a plain path (absolute, or "
                "relative to the remote home) of letters, digits, '.', '_', "
                "'-' and '/', not starting with '-' and without '.' or '..' parts",
            )
        )
    if ssh_config is not None:
        if isinstance(ssh_config, str) and PLACEHOLDER in ssh_config:
            findings.append(
                Finding(
                    "error",
                    "dashboard.push.ssh_config is a REPLACE_WITH_ placeholder",
                )
            )
        elif not valid_ssh_config(ssh_config):
            findings.append(
                Finding(
                    "error",
                    "dashboard.push.ssh_config must be an absolute path "
                    "of letters, digits, '.', '_', '-' and '/'",
                )
            )
        elif looks_like_key(ssh_config):
            findings.append(
                Finding(
                    "error",
                    "dashboard.push.ssh_config must name an ssh_config file, "
                    "not a key",
                )
            )
    if every_minutes is not None and not valid_every_minutes(every_minutes):
        findings.append(
            Finding(
                "error",
                "dashboard.push.every_minutes must be an integer from 1 to "
                f"{EVERY_MINUTES_MAX}",
            )
        )
    if method not in METHODS:
        findings.append(
            Finding("error", "dashboard.push.method must be rsync or scp")
        )
    return PushConfig(
        transport="ssh",
        host=host if isinstance(host, str) else None,
        remote_dir=remote_dir if isinstance(remote_dir, str) else None,
        method=method if method in METHODS else "rsync",
        interval_seconds=interval if in_range(interval) else REFRESH_DEFAULT,
        ssh_config=ssh_config if isinstance(ssh_config, str) else None,
        every_minutes=every_minutes if valid_every_minutes(every_minutes) else None,
        live=live is True,
        qa_budget_gib=qa_budget_gib if valid_qa_budget(qa_budget_gib) else None,
    )


def _unknown(
    value: dict,
    allowed: set[str],
    prefix: str,
    findings: list[Finding],
) -> None:
    for key in sorted(value, key=lambda item: str(item)):
        if key not in allowed:
            findings.append(Finding("error", f"unknown field '{prefix}.{key}'"))
