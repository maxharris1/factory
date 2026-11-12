"""The optional `dashboard` section of orch.yml.

dashboard:
  refresh_seconds: 20        # index.html auto-refresh, 15 to 30
  push:
    transport: none          # none (default) or ssh
    host: pc-wsl-bots        # ssh only: an SSH alias or tailnet name
    remote_dir: /srv/dash    # ssh only: absolute directory on that host
    method: rsync            # ssh only: rsync (default) or scp
    interval_seconds: 20     # 15 to 30

The section holds no secrets. The ssh transport relies on the SSH login
Max provisioned (~/.ssh/config); nothing here names a key or password.
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
# Absolute, conservative characters only, so no remote shell can misread it.
REMOTE_DIR = re.compile(r"^/[A-Za-z0-9._/-]+$")
PLACEHOLDER = "REPLACE_WITH_"

_DASHBOARD_KEYS = {"refresh_seconds", "push"}
_PUSH_KEYS = {"transport", "host", "remote_dir", "method", "interval_seconds"}
_SSH_ONLY = ("host", "remote_dir", "method")


@dataclass(frozen=True)
class PushConfig:
    transport: str = "none"
    host: str | None = None
    remote_dir: str | None = None
    method: str = "rsync"
    interval_seconds: int = REFRESH_DEFAULT


@dataclass(frozen=True)
class DashboardConfig:
    refresh_seconds: int = REFRESH_DEFAULT
    push: PushConfig = field(default_factory=PushConfig)


def in_range(value: object) -> bool:
    return is_strict_int(value) and REFRESH_MIN <= value <= REFRESH_MAX


def valid_host(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= 253
        and HOST.fullmatch(value) is not None
    )


def valid_remote_dir(value: object) -> bool:
    if not isinstance(value, str) or REMOTE_DIR.fullmatch(value) is None:
        return False
    parts = [part for part in value.split("/") if part]
    return bool(parts) and all(part not in (".", "..") for part in parts)


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
    if len(findings) > start and any(
        item.severity == "error" for item in findings[start:]
    ):
        return None
    assert push is not None
    return DashboardConfig(refresh_seconds=int(refresh), push=push)


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
    remote_dir = value.get("remote_dir")
    method = value.get("method", "rsync")
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
    if remote_dir is None:
        findings.append(
            Finding("error", "dashboard.push.remote_dir is required for ssh")
        )
    elif isinstance(remote_dir, str) and PLACEHOLDER in remote_dir:
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
                "dashboard.push.remote_dir must be an absolute path "
                "of letters, digits, '.', '_', '-' and '/'",
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
