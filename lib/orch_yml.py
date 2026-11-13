"""orch.yml schema (docs/SPEC.md section 7).

Required: poll_seconds (the decided interval, 60) and projects[].repo.
job_timeout is optional and measured in minutes. When the file and the
project both omit it, the timeout is 60 minutes. Unknown fields are
refused so v1 stays closed.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import yaml

from lib.dashboard_config import DashboardConfig, parse_dashboard
from lib.job_state import (
    DEFAULT_RELEASE_CLAIMED_MINUTES,
    DEFAULT_STALE_MINUTES,
    JobStateConfig,
)
from lib.load import Finding, is_strict_int, load_yaml

POLL_SECONDS = 60
DEFAULT_JOB_TIMEOUT_MINUTES = 60
DEFAULT_WEBHOOK_AUTH_ENV = "FACTORY_WEBHOOK_AUTH"
DEFAULT_LINEAR_WEBHOOK_PATH = "/linear-webhook"
DEFAULT_LINEAR_RECONCILE_SECONDS = 900
LINEAR_RECONCILE_MIN_SECONDS = 60
LINEAR_RECONCILE_MAX_SECONDS = 3600

_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_LOGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*(?:\[bot\])?$")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_URL_PATH = re.compile(r"^(?:/[A-Za-z0-9._~-]+)+$")
_BIND = re.compile(r"^(?:\[(?P<v6>[0-9A-Fa-f:.]+)\]|(?P<v4>[0-9.]+)):(?P<port>[0-9]{1,5})$")
_TOP_KEYS = {
    "poll_seconds",
    "job_timeout",
    "projects",
    "linear",
    "grok",
    "trusted_logins",
    "trusted_linear_users",
    "dashboard",
    "linear_webhook",
    "job_states",
}
_JOB_STATES_KEYS = {"stale_minutes", "release_claimed_minutes"}
_LINEAR_WEBHOOK_KEYS = {"bind", "path", "secret_file", "reconcile_seconds"}
_PROJECT_KEYS = {
    "repo",
    "job_timeout",
    "factory_ref",
    "linear_project_id",
    "grok_bot",
    "linear_ready_for_review",
    "ready_starts_review",
    "intake",
}
_INTAKE_KEYS = {"label", "linear_state", "trusted"}
DEFAULT_INTAKE_LABEL = "factory:ready"
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/ -]{0,49}$")
_LINEAR_STATE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._/-]{0,59}$")
_TRUSTED_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}(?:\[bot\])?$")


@dataclass(frozen=True)
class Intake:
    """How a project's tickets enter the factory by themselves (issue #85).

    A GitHub project names the label (default factory:ready); a Linear
    project names the workflow state. trusted lists who may apply it (GitHub
    logins or Linear user ids); None means the keys of trusted_logins or
    trusted_linear_users.
    """

    label: str | None = None
    linear_state: str | None = None
    trusted: frozenset[str] | None = None


@dataclass(frozen=True)
class Project:
    repo: str
    job_timeout: int | None = None
    factory_ref: str | None = None
    linear_project_id: str | None = None
    grok_bot: str = "grok-bot"
    linear_ready_for_review: bool = False
    # A trusted ready from factory.md's builder starts review even with no
    # factory-queued build job (PRs built outside the queue). Default off.
    ready_starts_review: bool = False
    # Backlog feeder intake (issue #85). None: the feeder ignores the project.
    intake: Intake | None = None


@dataclass(frozen=True)
class LinearConfig:
    api_key_env: str


@dataclass(frozen=True)
class GrokConfig:
    webhook_url_env: str
    webhook_key_env: str
    webhook_auth_env: str = DEFAULT_WEBHOOK_AUTH_ENV


@dataclass(frozen=True)
class LinearWebhookConfig:
    """Inbound Linear webhook listener (issue #55). Absent means no webhook.

    secret_file names the signing-secret file; check-config never reads it.
    reconcile_seconds is how often the poller still runs the full discovery
    search and rereads every tracked ticket, so a missed webhook costs at
    most one interval.
    """

    host: str
    port: int
    secret_file: Path
    path: str = DEFAULT_LINEAR_WEBHOOK_PATH
    reconcile_seconds: int = DEFAULT_LINEAR_RECONCILE_SECONDS


@dataclass(frozen=True)
class OrchConfig:
    poll_seconds: int
    projects: tuple[Project, ...]
    job_timeout: int | None = None
    linear: LinearConfig | None = None
    grok: GrokConfig | None = None
    trusted_logins: Mapping[str, frozenset[str]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    trusted_linear_users: Mapping[str, frozenset[str]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    linear_webhook: LinearWebhookConfig | None = None
    job_states: JobStateConfig = field(default_factory=JobStateConfig)


def resolve_job_timeout(config: OrchConfig, repo: str) -> int:
    """Project override, then the file override, then 60 minutes."""
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


def load_orch_yml(
    path: Path,
    *,
    allow_placeholders: bool = False,
) -> tuple[OrchConfig | None, list[Finding]]:
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
    return _validate(data, allow_placeholders=allow_placeholders)


def _validate(
    data: dict,
    *,
    allow_placeholders: bool,
) -> tuple[OrchConfig | None, list[Finding]]:
    findings: list[Finding] = []
    for key in sorted(data, key=lambda item: str(item)):
        if key not in _TOP_KEYS:
            findings.append(Finding("error", f"unknown field {str(key)!r}"))

    poll_seconds = _poll_seconds(data, findings)
    job_timeout = _timeout(data.get("job_timeout", None), "job_timeout", "job_timeout" in data, findings)
    projects = _projects(
        data,
        findings,
        allow_placeholders=allow_placeholders,
    )
    linear = _linear(data, findings)
    grok = _grok(data, findings)
    trusted_logins = _trusted_logins(data, findings)
    trusted_linear_users = _trusted_linear_users(data, findings)
    dashboard = parse_dashboard(data.get("dashboard"), findings)
    linear_webhook = _linear_webhook(data, findings)
    job_states = _job_states(data, findings)
    if (
        "linear_webhook" in data
        and projects is not None
        and not any(project.linear_project_id is not None for project in projects)
    ):
        findings.append(
            Finding(
                "error",
                "linear_webhook requires a project with linear_project_id",
            )
        )
    if (
        projects is not None
        and any(project.linear_project_id is not None for project in projects)
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
            trusted_linear_users=MappingProxyType(trusted_linear_users),
            dashboard=dashboard or DashboardConfig(),
            linear_webhook=linear_webhook,
            job_states=job_states or JobStateConfig(),
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


def _projects(
    data: dict,
    findings: list[Finding],
    *,
    allow_placeholders: bool,
) -> tuple[Project, ...] | None:
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
        factory_ref = item.get("factory_ref")
        if factory_ref is not None and (
            not isinstance(factory_ref, str)
            or not factory_ref.strip()
            or len(factory_ref) > 255
        ):
            findings.append(
                Finding(
                    "error",
                    f"projects[{index}].factory_ref must be a branch or ref",
                )
            )
            ok = False
            continue
        linear_project_id = item.get("linear_project_id")
        if linear_project_id is not None and (
            not isinstance(linear_project_id, str)
            or not linear_project_id.strip()
            or len(linear_project_id) > 200
            or (
                linear_project_id.startswith("REPLACE_WITH_")
                and not allow_placeholders
            )
        ):
            findings.append(
                Finding(
                    "error",
                    f"projects[{index}].linear_project_id must be a project id",
                )
            )
            ok = False
            continue
        if (
            isinstance(linear_project_id, str)
            and linear_project_id.startswith("REPLACE_WITH_")
            and allow_placeholders
        ):
            findings.append(
                Finding(
                    "warning",
                    "accepted placeholder "
                    f"projects[{index}].linear_project_id",
                )
            )
        if linear_project_id in seen_linear:
            findings.append(
                Finding("error", f"duplicate Linear project {linear_project_id!r}")
            )
            ok = False
        if linear_project_id is not None:
            seen_linear.add(linear_project_id)
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
        if linear_ready and linear_project_id is None:
            findings.append(
                Finding(
                    "error",
                    f"projects[{index}].linear_ready_for_review requires linear_project_id",
                )
            )
            ok = False
            continue
        ready_starts_review = item.get("ready_starts_review", False)
        if not isinstance(ready_starts_review, bool):
            findings.append(
                Finding(
                    "error",
                    f"projects[{index}].ready_starts_review must be true or false",
                )
            )
            ok = False
            continue
        intake = None
        if "intake" in item:
            intake = _intake(item["intake"], index, linear_project_id, findings)
            if intake is None:
                ok = False
                continue
        projects.append(
            Project(
                repo=repo,
                job_timeout=timeout,
                factory_ref=factory_ref,
                linear_project_id=linear_project_id,
                grok_bot=grok_bot,
                linear_ready_for_review=linear_ready,
                ready_starts_review=ready_starts_review,
                intake=intake,
            )
        )
    if not ok:
        return None
    return tuple(projects)


def _intake(
    value: object,
    index: int,
    linear_project_id: object,
    findings: list[Finding],
) -> Intake | None:
    where = f"projects[{index}].intake"
    if value is None:
        value = {}
    if not isinstance(value, dict):
        findings.append(Finding("error", f"{where} must be a mapping"))
        return None
    ok = True
    for key in sorted(value, key=str):
        if key not in _INTAKE_KEYS:
            findings.append(Finding("error", f"unknown field '{where}.{key}'"))
            ok = False
    linear = linear_project_id is not None
    label = value.get("label")
    state = value.get("linear_state")
    if linear:
        if "label" in value:
            findings.append(
                Finding("error", f"{where}.label is for GitHub projects; a Linear project names linear_state")
            )
            ok = False
        if not isinstance(state, str) or _LINEAR_STATE.fullmatch(state) is None:
            findings.append(
                Finding("error", f"{where}.linear_state must name the Linear workflow state")
            )
            ok = False
    else:
        if "linear_state" in value:
            findings.append(Finding("error", f"{where}.linear_state needs linear_project_id"))
            ok = False
        if label is None:
            label = DEFAULT_INTAKE_LABEL
        if not isinstance(label, str) or _LABEL.fullmatch(label) is None:
            findings.append(Finding("error", f"{where}.label must be a GitHub label name"))
            ok = False
    trusted: frozenset[str] | None = None
    if "trusted" in value:
        raw = value["trusted"]
        if not isinstance(raw, list) or not raw or not all(
            isinstance(entry, str) and _TRUSTED_ID.fullmatch(entry) for entry in raw
        ):
            findings.append(
                Finding("error", f"{where}.trusted must be a list of logins or Linear user ids")
            )
            ok = False
        else:
            trusted = frozenset(raw)
    if not ok:
        return None
    return Intake(
        label=None if linear else label,
        linear_state=state if linear else None,
        trusted=trusted,
    )


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


def parse_bind(value: object) -> tuple[str, int] | None:
    """`127.0.0.1:8787` or `[::1]:8787`: an IP literal and a port 1-65535."""
    if not isinstance(value, str):
        return None
    match = _BIND.fullmatch(value)
    if match is None:
        return None
    host = match.group("v6") or match.group("v4")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if (address.version == 6) != (match.group("v6") is not None):
        return None
    port = int(match.group("port"))
    if not 1 <= port <= 65535:
        return None
    return str(address), port


def _linear_webhook(
    data: dict,
    findings: list[Finding],
) -> LinearWebhookConfig | None:
    if "linear_webhook" not in data:
        return None
    value = data["linear_webhook"]
    if not isinstance(value, dict):
        findings.append(Finding("error", "linear_webhook must be a mapping"))
        return None
    start = len(findings)
    _unknown_keys(value, _LINEAR_WEBHOOK_KEYS, "linear_webhook", findings)
    bind = parse_bind(value.get("bind"))
    if bind is None:
        findings.append(
            Finding("error", "linear_webhook.bind must be host:port")
        )
    secret = value.get("secret_file")
    secret_file: Path | None = None
    if isinstance(secret, str) and secret.strip():
        candidate = Path(secret).expanduser()
        if candidate.is_absolute():
            secret_file = candidate
    if secret_file is None:
        findings.append(
            Finding("error", "linear_webhook.secret_file must be an absolute path")
        )
    path = value.get("path", DEFAULT_LINEAR_WEBHOOK_PATH)
    if not isinstance(path, str) or _URL_PATH.fullmatch(path) is None:
        findings.append(
            Finding("error", "linear_webhook.path must be an absolute URL path")
        )
    reconcile = value.get("reconcile_seconds", DEFAULT_LINEAR_RECONCILE_SECONDS)
    if (
        not is_strict_int(reconcile)
        or not LINEAR_RECONCILE_MIN_SECONDS
        <= reconcile
        <= LINEAR_RECONCILE_MAX_SECONDS
    ):
        findings.append(
            Finding(
                "error",
                "linear_webhook.reconcile_seconds must be an integer "
                f"from {LINEAR_RECONCILE_MIN_SECONDS} to "
                f"{LINEAR_RECONCILE_MAX_SECONDS}",
            )
        )
    if len(findings) > start or bind is None or secret_file is None:
        return None
    return LinearWebhookConfig(
        host=bind[0],
        port=bind[1],
        secret_file=secret_file,
        path=str(path),
        reconcile_seconds=int(reconcile),
    )


def _job_states(data: dict, findings: list[Finding]) -> JobStateConfig | None:
    """job_states (#75): when a running job counts as stale, and how long a
    claimed job that was never started waits before the poller requeues it.
    Whole minutes, at least 1; the stale threshold must stay above `orch job
    run`'s heartbeat (15 seconds by default, an override at most 30)."""
    if "job_states" not in data:
        return None
    value = data["job_states"]
    if not isinstance(value, dict):
        findings.append(Finding("error", "job_states must be a mapping"))
        return None
    start = len(findings)
    _unknown_keys(value, _JOB_STATES_KEYS, "job_states", findings)
    minutes: dict[str, int] = {}
    for key, default in (
        ("stale_minutes", DEFAULT_STALE_MINUTES),
        ("release_claimed_minutes", DEFAULT_RELEASE_CLAIMED_MINUTES),
    ):
        item = value.get(key, default)
        if not is_strict_int(item) or item < 1:
            findings.append(
                Finding("error", f"job_states.{key} must be a positive integer (minutes)")
            )
            continue
        minutes[key] = int(item)
    if len(findings) > start:
        return None
    return JobStateConfig(
        stale_seconds=minutes["stale_minutes"] * 60.0,
        release_claimed_seconds=minutes["release_claimed_minutes"] * 60.0,
    )


def _grok(data: dict, findings: list[Finding]) -> GrokConfig | None:
    value = data.get("grok")
    if value is None:
        return None
    if not isinstance(value, dict):
        findings.append(Finding("error", "grok must be a mapping"))
        return None
    _unknown_keys(
        value,
        {"webhook_url_env", "webhook_key_env", "webhook_auth_env"},
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
    auth_name = _env_name(
        value.get("webhook_auth_env", DEFAULT_WEBHOOK_AUTH_ENV),
        "grok.webhook_auth_env",
        findings,
    )
    if url_name is None or key_name is None or auth_name is None:
        return None
    return GrokConfig(url_name, key_name, auth_name)


def _trusted_logins(
    data: dict,
    findings: list[Finding],
) -> dict[str, frozenset[str]]:
    value = data.get("trusted_logins")
    if value is None:
        return {}
    if not isinstance(value, dict) or not value:
        findings.append(Finding("error", "trusted_logins must be a non-empty mapping"))
        return {}
    result: dict[str, frozenset[str]] = {}
    for login, identities in value.items():
        if (
            not isinstance(login, str)
            or _LOGIN.fullmatch(login) is None
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
        result[login] = frozenset(identities)
    return result


def _trusted_linear_users(
    data: dict,
    findings: list[Finding],
) -> dict[str, frozenset[str]]:
    value = data.get("trusted_linear_users")
    if value is None:
        return {}
    if not isinstance(value, dict) or not value:
        findings.append(
            Finding("error", "trusted_linear_users must be a non-empty mapping")
        )
        return {}
    result: dict[str, frozenset[str]] = {}
    for user_id, identities in value.items():
        if (
            not isinstance(user_id, str)
            or not user_id.strip()
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
                    "trusted_linear_users entries must map stable user id "
                    "to a non-empty bot list",
                )
            )
            continue
        result[f"linear:{user_id}"] = frozenset(identities)
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
