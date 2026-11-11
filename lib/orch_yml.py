"""orch.yml schema (docs/SPEC.md section 7).

Required: poll_seconds (the decided interval, 60) and projects[].repo.
job_timeout is optional and measured in minutes. When the file and the
project both omit it, the timeout is 90 minutes. Unknown fields are
refused so v1 stays closed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import yaml

from lib.load import Finding, is_strict_int, load_yaml

POLL_SECONDS = 60
DEFAULT_JOB_TIMEOUT_MINUTES = 90

_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_TOP_KEYS = {
    "poll_seconds",
    "job_timeout",
    "projects",
    "linear",
    "grok",
    "trusted_logins",
}
_PROJECT_KEYS = {
    "repo",
    "job_timeout",
    "linear_project",
    "grok_bot",
    "linear_ready_for_review",
}


@dataclass(frozen=True)
class Project:
    repo: str
    job_timeout: int | None = None
    linear_project: str | None = None
    grok_bot: str = "grok-bot"
    linear_ready_for_review: bool = False


@dataclass(frozen=True)
class LinearConfig:
    api_key_env: str


@dataclass(frozen=True)
class GrokConfig:
    webhook_url_env: str
    webhook_key_env: str


@dataclass(frozen=True)
class OrchConfig:
    poll_seconds: int
    projects: tuple[Project, ...]
    job_timeout: int | None = None
    linear: LinearConfig | None = None
    grok: GrokConfig | None = None
    trusted_logins: Mapping[str, Mapping[str, frozenset[str]]] = field(
        default_factory=lambda: MappingProxyType({})
    )


def resolve_job_timeout(config: OrchConfig, repo: str) -> int:
    """Project override, then the file override, then 90 minutes."""
    wanted = repo.lower()
    project = next(
        (item for item in config.projects if item.repo.lower() == wanted),
        None,
    )
    if project is None:
        raise KeyError(repo)
    if project.job_timeout is not None:
        return project.job_timeout
    if config.job_timeout is not None:
        return config.job_timeout
    return DEFAULT_JOB_TIMEOUT_MINUTES


def load_orch_yml(path: Path) -> tuple[OrchConfig | None, list[Finding]]:
    if not path.is_file():
        return None, [Finding("error", "file not found")]
    text = path.read_text(encoding="utf-8")
    try:
        data = load_yaml(text)
    except yaml.YAMLError as exc:
        text = " ".join(line.strip() for line in str(exc).splitlines() if line.strip())
        return None, [Finding("error", f"invalid YAML: {text or 'invalid YAML'}")]
    if not isinstance(data, dict):
        return None, [Finding("error", "orch.yml must be a mapping")]
    return _validate(data)


def _validate(data: dict) -> tuple[OrchConfig | None, list[Finding]]:
    findings: list[Finding] = []
    for key in sorted(data, key=lambda item: str(item)):
        if key not in _TOP_KEYS:
            findings.append(Finding("error", f"unknown field {str(key)!r}"))

    poll_seconds = _poll_seconds(data, findings)
    job_timeout = _timeout(data.get("job_timeout", None), "job_timeout", "job_timeout" in data, findings)
    projects = _projects(data, findings)
    linear = _linear(data, findings)
    grok = _grok(data, findings)
    trusted_logins = _trusted_logins(data, findings)
    if (
        projects is not None
        and any(project.linear_project is not None for project in projects)
        and linear is None
        and "linear" not in data
    ):
        findings.append(
            Finding("error", "linear.api_key_env is required for Linear projects")
        )

    if any(item.severity == "error" for item in findings):
        return None, findings
    assert poll_seconds is not None
    assert projects is not None
    return (
        OrchConfig(
            poll_seconds=poll_seconds,
            projects=projects,
            job_timeout=job_timeout,
            linear=linear,
            grok=grok,
            trusted_logins=MappingProxyType(trusted_logins),
        ),
        findings,
    )


def _poll_seconds(data: dict, findings: list[Finding]) -> int | None:
    if "poll_seconds" not in data:
        findings.append(Finding("error", "missing required field 'poll_seconds'"))
        return None
    value = data["poll_seconds"]
    if value != POLL_SECONDS:
        findings.append(Finding("error", "poll_seconds must be 60"))
        return None
    return POLL_SECONDS


def _timeout(
    value: object,
    field: str,
    present: bool,
    findings: list[Finding],
) -> int | None:
    if not present:
        return None
    if not is_strict_int(value) or value < 1:
        findings.append(
            Finding("error", f"{field} must be a positive integer (minutes)")
        )
        return None
    return int(value)


def _projects(data: dict, findings: list[Finding]) -> tuple[Project, ...] | None:
    if "projects" not in data:
        findings.append(Finding("error", "missing required field 'projects'"))
        return None
    value = data["projects"]
    if not isinstance(value, list) or not value:
        findings.append(Finding("error", "projects must list at least one repo"))
        return None
    projects: list[Project] = []
    seen: set[str] = set()
    seen_linear: set[str] = set()
    ok = True
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            findings.append(Finding("error", f"projects[{index}] must be a mapping"))
            ok = False
            continue
        for key in sorted(item, key=lambda entry: str(entry)):
            if key not in _PROJECT_KEYS:
                findings.append(
                    Finding("error", f"unknown field 'projects[{index}].{key}'")
                )
                ok = False
        repo = item.get("repo")
        if not isinstance(repo, str) or _REPO.fullmatch(repo) is None:
            findings.append(Finding("error", f"projects[{index}].repo must be owner/name"))
            ok = False
            continue
        identity = repo.lower()
        if identity in seen:
            findings.append(Finding("error", f"duplicate project {repo!r}"))
            ok = False
        seen.add(identity)
        timeout = _timeout(
            item.get("job_timeout"),
            f"projects[{index}].job_timeout",
            "job_timeout" in item,
            findings,
        )
        if "job_timeout" in item and timeout is None:
            ok = False
            continue
        linear_project = item.get("linear_project")
        if linear_project is not None and (
            not isinstance(linear_project, str)
            or not linear_project.strip()
            or len(linear_project) > 200
        ):
            findings.append(
                Finding(
                    "error",
                    f"projects[{index}].linear_project must be a project name or id",
                )
            )
            ok = False
            continue
        if linear_project in seen_linear:
            findings.append(
                Finding("error", f"duplicate Linear project {linear_project!r}")
            )
            ok = False
        if linear_project is not None:
            seen_linear.add(linear_project)
        grok_bot = item.get("grok_bot", "grok-bot")
        if not isinstance(grok_bot, str) or _TOKEN.fullmatch(grok_bot) is None:
            findings.append(
                Finding("error", f"projects[{index}].grok_bot must be a bot name")
            )
            ok = False
            continue
        linear_ready = item.get("linear_ready_for_review", False)
        if not isinstance(linear_ready, bool):
            findings.append(
                Finding(
                    "error",
                    f"projects[{index}].linear_ready_for_review must be true or false",
                )
            )
            ok = False
            continue
        if linear_ready and linear_project is None:
            findings.append(
                Finding(
                    "error",
                    f"projects[{index}].linear_ready_for_review requires linear_project",
                )
            )
            ok = False
            continue
        projects.append(
            Project(
                repo=repo,
                job_timeout=timeout,
                linear_project=linear_project,
                grok_bot=grok_bot,
                linear_ready_for_review=linear_ready,
            )
        )
    if not ok:
        return None
    return tuple(projects)


def _linear(data: dict, findings: list[Finding]) -> LinearConfig | None:
    value = data.get("linear")
    if value is None:
        return None
    if not isinstance(value, dict):
        findings.append(Finding("error", "linear must be a mapping"))
        return None
    _unknown_keys(value, {"api_key_env"}, "linear", findings)
    name = _env_name(value.get("api_key_env"), "linear.api_key_env", findings)
    return LinearConfig(name) if name is not None else None


def _grok(data: dict, findings: list[Finding]) -> GrokConfig | None:
    value = data.get("grok")
    if value is None:
        return None
    if not isinstance(value, dict):
        findings.append(Finding("error", "grok must be a mapping"))
        return None
    _unknown_keys(
        value,
        {"webhook_url_env", "webhook_key_env"},
        "grok",
        findings,
    )
    url_name = _env_name(
        value.get("webhook_url_env"),
        "grok.webhook_url_env",
        findings,
    )
    key_name = _env_name(
        value.get("webhook_key_env"),
        "grok.webhook_key_env",
        findings,
    )
    if url_name is None or key_name is None:
        return None
    return GrokConfig(url_name, key_name)


def _trusted_logins(
    data: dict,
    findings: list[Finding],
) -> dict[str, Mapping[str, frozenset[str]]]:
    value = data.get("trusted_logins")
    if value is None:
        return {}
    if not isinstance(value, dict) or not value:
        findings.append(Finding("error", "trusted_logins must be a non-empty mapping"))
        return {}
    result: dict[str, Mapping[str, frozenset[str]]] = {}
    for tracker, entries in value.items():
        if tracker not in ("github", "linear") or not isinstance(entries, dict):
            findings.append(
                Finding(
                    "error",
                    "trusted_logins must map github/linear to login mappings",
                )
            )
            continue
        tracker_result: dict[str, frozenset[str]] = {}
        for login, identities in entries.items():
            if (
                not isinstance(login, str)
                or not login
                or not isinstance(identities, list)
                or not identities
                or any(
                    not isinstance(identity, str)
                    or _TOKEN.fullmatch(identity) is None
                    for identity in identities
                )
            ):
                findings.append(
                    Finding(
                        "error",
                        "trusted_logins entries must map login to a non-empty bot list",
                    )
                )
                continue
            tracker_result[login] = frozenset(identities)
        result[tracker] = MappingProxyType(tracker_result)
    return result


def _env_name(
    value: object,
    field: str,
    findings: list[Finding],
) -> str | None:
    if not isinstance(value, str) or _ENV_NAME.fullmatch(value) is None:
        findings.append(Finding("error", f"{field} must be an environment variable name"))
        return None
    return value


def _unknown_keys(
    value: dict,
    allowed: set[str],
    prefix: str,
    findings: list[Finding],
) -> None:
    for key in sorted(value, key=lambda item: str(item)):
        if key not in allowed:
            findings.append(Finding("error", f"unknown field '{prefix}.{key}'"))
