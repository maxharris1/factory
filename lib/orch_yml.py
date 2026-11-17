"""orch.yml schema (docs/SPEC.md section 7).

Required: poll_seconds (30 or 60, the GitHub poll interval) and
projects[].repo. Linear keeps its own LINEAR_POLL_SECONDS cadence whatever
poll_seconds is (lib/poll_source.py).
job_timeout is optional and measured in minutes. When the file and the
project both omit it, the timeout is 60 minutes. refused_harnesses
(optional) names harnesses whose jobs are refused, so health expects no
waiter for them. Unknown fields are refused so v1 stays closed. A
gitignored orch.local.yml beside the file is merged in before validation
(lib/config_overlay.py).
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping
from urllib.parse import urlsplit

import yaml

from lib.config_overlay import apply_overlay
from lib.dashboard_config import DashboardConfig, parse_dashboard
from lib.factory_md import HoldConfig, QaConfig
from lib.job_state import (
    DEFAULT_RELEASE_CLAIMED_MINUTES,
    DEFAULT_STALE_MINUTES,
    DEFAULT_UNREACHABLE_LOST_MINUTES,
    JobStateConfig,
)
from lib.load import Finding, is_strict_int, load_yaml
from lib.remote import RemoteError, plain_branch
from lib.schema import HARNESSES

# The seconds `orch poll` sleeps after each cycle. GitHub is read every
# cycle; a cycle's own work adds to it, so this is the gap between cycles,
# not a promise of pickup within that many seconds.
POLL_SECONDS_ALLOWED = (30, 60)
# The default and slowest allowed interval (dashboard poll health uses it).
POLL_SECONDS = 60
# Linear discovery, dirty-row and tracked reads run at most once per this
# many seconds, with the per-window budget sized for it, at any poll_seconds.
LINEAR_POLL_SECONDS = 60
DEFAULT_JOB_TIMEOUT_MINUTES = 60
DEFAULT_WEBHOOK_AUTH_ENV = "FACTORY_WEBHOOK_AUTH"
DEFAULT_LINEAR_WEBHOOK_PATH = "/linear-webhook"
DEFAULT_LINEAR_RECONCILE_SECONDS = 900
LINEAR_RECONCILE_MIN_SECONDS = 60
LINEAR_RECONCILE_MAX_SECONDS = 3600
DEFAULT_TUNNEL_ALERT_MINUTES = 30

_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ISSUE_REF = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]{0,9})$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_LOGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*(?:\[bot\])?$")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_URL_PATH = re.compile(r"^(?:/[A-Za-z0-9._~-]+)+$")
# The listener's Funnel URL (#92): https, a DNS name, an optional port, a plain path.
_PUBLIC_URL = re.compile(
    r"^https://[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})(?:\.[A-Za-z0-9-]{1,63})*"
    r"(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~-]+)+$"
)
_BIND = re.compile(r"^(?:\[(?P<v6>[0-9A-Fa-f:.]+)\]|(?P<v4>[0-9.]+)):(?P<port>[0-9]{1,5})$")
_TOP_KEYS = {
    "poll_seconds",
    "job_timeout",
    "projects",
    "linear",
    "grok",
    "driver",
    "claude",
    "trusted_logins",
    "trusted_linear_users",
    "dashboard",
    "linear_webhook",
    "job_states",
    "refused_harnesses",
    "approver_logins",
    "approver_linear_users",
    "approver_gh_config_dir",
    "github_app",
}
_GITHUB_APP_KEYS = {"enabled", "apps"}
_APP_KEYS = {"bot_login", "app_id", "installations", "private_key_file"}
_MOVED_APP_KEYS = ("bot_login", "app_id", "installations", "private_key_file")
Driver = Literal["grok", "claude"]
APP_BOTS = ("grok-bot", "claude-bot", "codex-bot", "cursor-bot")
_USER_LOGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
_BOT_LOGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*\[bot\]$")
_JOB_STATES_KEYS = {"stale_minutes", "release_claimed_minutes", "unreachable_lost_minutes"}
_LINEAR_WEBHOOK_KEYS = {
    "bind", "path", "secret_file", "reconcile_seconds", "public_url", "tunnel_alert_minutes",
}
_PROJECT_KEYS = {
    "repo",
    "job_timeout",
    "factory_ref",
    "linear_project_id",
    "linear_project_ids",
    "grok_bot",
    "linear_ready_for_review",
    "ready_starts_review",
    "intake",
    "driver_inbox",
    "grok_webhook",
    "qa_storage",
}
_QA_STORAGE_KEYS = {
    "bucket", "region", "endpoint", "access_key_env", "secret_key_env", "evidence_root",
}
_QA_STORAGE_REQUIRED = ("bucket", "region", "access_key_env", "secret_key_env", "evidence_root")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_REGION = re.compile(r"^[a-z0-9-]{1,32}$")
_INTAKE_KEYS = {
    "label",
    "linear_state",
    "trusted",
    "priority_labels",
    "priority_from_linear",
    "critical_path_order",
    "linear_assignees",
    "base_branches",
}
DEFAULT_INTAKE_LABEL = "factory:ready"
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/ -]{0,49}$")
_LINEAR_STATE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._/-]{0,59}$")
_TRUSTED_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}(?:\[bot\])?$")
_LINEAR_USER_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@dataclass(frozen=True)
class Intake:
    """How a project's tickets enter the factory by themselves (issue #85).

    A GitHub project names the label (default factory:ready); a Linear
    project names the workflow state. trusted lists who may apply it (GitHub
    logins or Linear user ids); None means the keys of trusted_logins or
    trusted_linear_users. linear_assignees maps a Linear project id to the
    only assignees whose tickets the feeder takes from that project.
    base_branches maps a Linear project id to the branch its tickets'
    PRs open on, review against and ship to, in place of factory_ref.
    """

    label: str | None = None
    linear_state: str | None = None
    trusted: frozenset[str] | None = None
    priority_labels: tuple[str, ...] = ()
    priority_from_linear: bool = False
    critical_path_order: bool = True
    linear_assignees: Mapping[str, frozenset[str]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    base_branches: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True)
class DriverInbox:
    repo: str
    number: int


@dataclass(frozen=True)
class QaStorage:
    bucket: str
    region: str
    endpoint: str | None
    access_key_env: str
    secret_key_env: str
    evidence_root: Path


@dataclass(frozen=True)
class Project:
    repo: str
    job_timeout: int | None = None
    factory_ref: str | None = None
    linear_project_ids: frozenset[str] = frozenset()
    grok_bot: str = "grok-bot"
    linear_ready_for_review: bool = False
    # A trusted ready from factory.md's builder starts review even with no
    # factory-queued build job (PRs built outside the queue). Default off.
    ready_starts_review: bool = False
    # Backlog feeder intake (issue #85). None: the feeder ignores the project.
    intake: Intake | None = None
    driver_inbox: DriverInbox | None = None
    grok_webhook: bool = True
    qa_storage: QaStorage | None = None

    def base_for(self, linear_project_id: str | None) -> str | None:
        if self.intake is not None and linear_project_id in self.intake.base_branches:
            return self.intake.base_branches[linear_project_id]
        return self.factory_ref


@dataclass(frozen=True)
class LinearConfig:
    api_key_env: str


@dataclass(frozen=True)
class GrokConfig:
    webhook_url_env: str
    webhook_key_env: str
    webhook_auth_env: str = DEFAULT_WEBHOOK_AUTH_ENV


@dataclass(frozen=True)
class ClaudeDriverConfig:
    fire_url_env: str
    fire_token_env: str


@dataclass(frozen=True)
class LinearWebhookConfig:
    """Inbound Linear webhook listener (issue #55). Absent means no webhook.

    secret_file names the signing-secret file; check-config never reads it.
    reconcile_seconds is how often the poller still runs the full discovery
    search and rereads every tracked ticket, so a missed webhook costs at
    most one interval. public_url is the Funnel URL of `path` that the
    listener's tunnel heartbeat GETs (issue #92); without it the tunnel
    shows down. A tunnel down longer than tunnel_alert_minutes is a
    needs-attention item in `orch health` (issue #581).
    """

    host: str
    port: int
    secret_file: Path
    path: str = DEFAULT_LINEAR_WEBHOOK_PATH
    reconcile_seconds: int = DEFAULT_LINEAR_RECONCILE_SECONDS
    public_url: str | None = None
    tunnel_alert_minutes: int = DEFAULT_TUNNEL_ALERT_MINUTES


@dataclass(frozen=True)
class GitHubApp:
    """One bot's GitHub App (#170). private_key_file is a path orch never
    opens."""

    bot: str
    bot_login: str
    app_id: int
    installations: Mapping[str, int]
    private_key_file: Path


@dataclass(frozen=True)
class GitHubApps:
    apps: Mapping[str, GitHubApp]
    enabled: bool = False


@dataclass(frozen=True)
class Approvals:
    """Who may give a capped-ticket go or a hold ok (#122), and which login
    is which bot's App (#170). With app_trust off, the dispatcher keeps
    today's trusted_logins rule. gh_config_dir is Max's approver gh profile,
    a path only."""

    app_trust: bool = False
    logins: frozenset[str] = frozenset()
    gh_config_dir: Path | None = None
    app_logins: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    linear_users: frozenset[str] | None = None

    def may_approve(self, login: str) -> bool:
        return login.casefold() in self.logins

    def may_approve_linear(self, author: str) -> bool:
        return author in (self.linear_users or ())

    def app_bot(self, login: str) -> str | None:
        """The bot whose App posts as login, when the switch is on."""
        if not self.app_trust:
            return None
        return self.app_logins.get(login.casefold())


@dataclass(frozen=True)
class OrchConfig:
    poll_seconds: int
    projects: tuple[Project, ...]
    job_timeout: int | None = None
    linear: LinearConfig | None = None
    grok: GrokConfig | None = None
    driver: Driver = "grok"
    claude: ClaudeDriverConfig | None = None
    trusted_logins: Mapping[str, frozenset[str]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    trusted_linear_users: Mapping[str, frozenset[str]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    linear_webhook: LinearWebhookConfig | None = None
    job_states: JobStateConfig = field(default_factory=JobStateConfig)
    # Harnesses whose jobs the factory refuses (issue #107): health expects
    # no waiter for them.
    refused_harnesses: tuple[str, ...] = ()
    github_app: GitHubApps | None = None
    approver_logins: frozenset[str] | None = None
    approver_linear_users: frozenset[str] | None = None
    approver_gh_config_dir: Path | None = None

    @property
    def secret_env_names(self) -> tuple[str, ...]:
        names: list[str] = []
        if self.linear is not None:
            names.append(self.linear.api_key_env)
        if self.grok is not None:
            names.extend(
                (
                    self.grok.webhook_url_env,
                    self.grok.webhook_key_env,
                    self.grok.webhook_auth_env,
                )
            )
        if self.claude is not None:
            names.extend((self.claude.fire_url_env, self.claude.fire_token_env))
        return tuple(dict.fromkeys(names))


def approvals(config: OrchConfig) -> Approvals:
    return _approvals(
        config.github_app,
        config.approver_logins,
        config.approver_gh_config_dir,
        config.approver_linear_users,
    )


def _approvals(
    github_app: GitHubApps | None,
    approver_logins: frozenset[str] | None,
    gh_config_dir: Path | None,
    approver_linear_users: frozenset[str] | None,
) -> Approvals:
    apps = github_app.apps.values() if github_app is not None else ()
    return Approvals(
        app_trust=github_app is not None and github_app.enabled,
        logins=frozenset(login.casefold() for login in approver_logins or ()),
        gh_config_dir=gh_config_dir,
        app_logins=MappingProxyType({app.bot_login.casefold(): app.bot for app in apps}),
        linear_users=approver_linear_users,
    )


def load_approvals(path: Path) -> tuple[Approvals | None, list[Finding]]:
    """The approval settings alone (#122), for a waiter whose orch.yml has
    an error elsewhere: that error must not turn the approver-profile guard
    off. None when the file or these settings do not load."""
    data, findings = _read(path)
    if data is None:
        return None, findings
    approver_logins = _approver_logins(data, findings)
    approver_linear_users = _approver_linear_users(data, findings)
    gh_config_dir = _absolute_path(
        data, "approver_gh_config_dir", "approver_gh_config_dir", findings
    )
    app = _github_app(data, findings)
    if findings:
        return None, findings
    return _approvals(app, approver_logins, gh_config_dir, approver_linear_users), findings


def linear_approver_findings(config: OrchConfig) -> list[Finding]:
    if config.approver_linear_users is not None or not any(
        project.linear_project_ids for project in config.projects
    ):
        return []
    return [
        Finding("warning", "approver_linear_users is unset: no Linear go lifts a cap")
    ]


def _harness_bot_logins(config: OrchConfig) -> dict[str, tuple[str, ...]]:
    bots = {f"{harness}-bot" for harness in HARNESSES}
    return {
        login.casefold(): tuple(sorted(identities & bots))
        for login, identities in config.trusted_logins.items()
        if identities & bots
    }


def bot_approver_findings(config: OrchConfig) -> list[Finding]:
    carried = _harness_bot_logins(config)
    return [
        Finding(
            "warning",
            f"login {login} carries harness bot {', '.join(carried[login.casefold()])} "
            "and is in approver_logins: that bot could lift its own cap",
        )
        for login in sorted(config.approver_logins or ())
        if login.casefold() in carried
    ]


def hold_approver_findings(config: OrchConfig, hold: HoldConfig | None) -> list[Finding]:
    """A hold whose approver can never clear it (#122): not in
    approver_logins, or, with the switch off, not a trusted_logins key."""
    if hold is None:
        return []
    approver = hold.approver.casefold()
    findings: list[Finding] = []
    bots = _harness_bot_logins(config).get(approver)
    if bots:
        findings.append(
            Finding(
                "warning",
                f"hold.approver {hold.approver} carries harness bot {', '.join(bots)}: "
                "that bot could clear its own hold",
            )
        )
    if config.approver_logins is not None and approver not in {
        login.casefold() for login in config.approver_logins
    }:
        findings.append(
            Finding("error", f"hold.approver {hold.approver} is not in approver_logins")
        )
    if not approvals(config).app_trust and approver not in {
        login.casefold() for login in config.trusted_logins
    }:
        findings.append(
            Finding("error", f"hold.approver {hold.approver} is not a trusted_logins key")
        )
    return findings


def qa_storage_findings(
    config: OrchConfig, repo: str | None, qa: QaConfig | None
) -> list[Finding]:
    if qa is None:
        return []
    if repo is None:
        return [
            Finding(
                "error",
                "qa.enabled is true: pass --factory owner/name=PATH so check-config "
                "can find the project's qa_storage entry",
            )
        ]
    project = next(
        (item for item in config.projects if item.repo.lower() == repo.lower()), None
    )
    if project is None or project.qa_storage is None:
        return [
            Finding(
                "error",
                f"qa.enabled is true but {repo} has no qa_storage entry "
                "(set it under its project in orch.local.yml)",
            )
        ]
    return []


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
    overlay: bool = True,
) -> tuple[OrchConfig | None, list[Finding]]:
    data, findings = _read(path, overlay=overlay)
    if data is None:
        return None, findings
    return _validate(data, allow_placeholders=allow_placeholders)


def _read(path: Path, *, overlay: bool = True) -> tuple[dict | None, list[Finding]]:
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
    if overlay:
        data, findings = apply_overlay(path, data)
        if data is None:
            return None, findings
    return data, []


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
    driver = _driver(data, findings)
    claude = _claude(data, findings)
    if driver == "claude" and data.get("claude") is None:
        findings.append(Finding("error", "driver: claude requires a claude block"))
    trusted_logins = _trusted_logins(data, findings)
    trusted_linear_users = _trusted_linear_users(data, findings)
    dashboard = parse_dashboard(data.get("dashboard"), findings)
    linear_webhook = _linear_webhook(data, findings)
    job_states = _job_states(data, findings)
    refused_harnesses = _refused_harnesses(data, findings)
    approver_logins = _approver_logins(data, findings)
    approver_linear_users = _approver_linear_users(data, findings)
    for author in sorted(approver_linear_users or ()):
        if author not in trusted_linear_users:
            findings.append(
                Finding(
                    "error",
                    f"approver_linear_users {author.removeprefix('linear:')} "
                    "is not a trusted_linear_users key",
                )
            )
    approver_gh_config_dir = _absolute_path(
        data, "approver_gh_config_dir", "approver_gh_config_dir", findings
    )
    github_app = _github_app(data, findings)
    if github_app is not None and github_app.enabled:
        _app_switch(github_app, approver_logins, trusted_logins, projects, findings)
    if (
        "linear_webhook" in data
        and projects is not None
        and not any(project.linear_project_ids for project in projects)
    ):
        findings.append(
            Finding(
                "error",
                "linear_webhook requires a project with linear_project_id",
            )
        )
    if (
        projects is not None
        and any(project.linear_project_ids for project in projects)
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
            driver=driver,
            claude=claude,
            trusted_logins=MappingProxyType(trusted_logins),
            trusted_linear_users=MappingProxyType(trusted_linear_users),
            dashboard=dashboard or DashboardConfig(),
            linear_webhook=linear_webhook,
            job_states=job_states or JobStateConfig(),
            refused_harnesses=refused_harnesses,
            github_app=github_app,
            approver_logins=approver_logins,
            approver_linear_users=approver_linear_users,
            approver_gh_config_dir=approver_gh_config_dir,
        ),
        findings,
    )


def _refused_harnesses(data: dict, findings: list[Finding]) -> tuple[str, ...]:
    value = data.get("refused_harnesses")
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        findings.append(Finding("error", "refused_harnesses must be a list of harness names"))
        return ()
    for item in value:
        if item not in HARNESSES:
            findings.append(
                Finding(
                    "error",
                    f"refused_harnesses has unknown harness {item!r}; "
                    f"expected one of {', '.join(HARNESSES)}",
                )
            )
    if len(set(value)) != len(value):
        findings.append(Finding("error", "refused_harnesses lists a harness twice"))
    return tuple(value)


def _poll_seconds(data: dict, findings: list[Finding]) -> int | None:
    if "poll_seconds" not in data:
        findings.append(Finding("error", "missing required field 'poll_seconds'"))
        return None
    value = data["poll_seconds"]
    if not is_strict_int(value) or value not in POLL_SECONDS_ALLOWED:
        allowed = " or ".join(str(item) for item in POLL_SECONDS_ALLOWED)
        findings.append(Finding("error", f"poll_seconds must be {allowed}"))
        return None
    return int(value)


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
        linear_project_ids = _linear_project_ids(
            item, index, seen_linear, allow_placeholders, findings
        )
        if linear_project_ids is None:
            ok = False
            continue
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
        if linear_ready and not linear_project_ids:
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
            intake = _intake(item["intake"], index, linear_project_ids, findings)
            if intake is None:
                ok = False
                continue
        driver_inbox = None
        if "driver_inbox" in item:
            value = item["driver_inbox"]
            match = _ISSUE_REF.fullmatch(value) if isinstance(value, str) else None
            if match is None:
                findings.append(
                    Finding(
                        "error",
                        f"projects[{index}].driver_inbox must be owner/name#<issue number>",
                    )
                )
                ok = False
                continue
            driver_inbox = DriverInbox(match.group(1), int(match.group(2)))
        grok_webhook = item.get("grok_webhook", True)
        if not isinstance(grok_webhook, bool):
            findings.append(
                Finding("error", f"projects[{index}].grok_webhook must be true or false")
            )
            ok = False
            continue
        if not grok_webhook and driver_inbox is None:
            findings.append(
                Finding(
                    "error",
                    f"projects[{index}].grok_webhook: false requires driver_inbox,"
                    " or its wakes go nowhere",
                )
            )
            ok = False
            continue
        qa_storage = None
        if "qa_storage" in item:
            qa_storage = _qa_storage(item["qa_storage"], f"projects[{index}].qa_storage", findings)
            if qa_storage is None:
                ok = False
                continue
        projects.append(
            Project(
                repo=repo,
                job_timeout=timeout,
                factory_ref=factory_ref,
                linear_project_ids=linear_project_ids,
                grok_bot=grok_bot,
                linear_ready_for_review=linear_ready,
                ready_starts_review=ready_starts_review,
                intake=intake,
                driver_inbox=driver_inbox,
                grok_webhook=grok_webhook,
                qa_storage=qa_storage,
            )
        )
    if not ok:
        return None
    for index, project in enumerate(projects):
        inbox = project.driver_inbox
        if inbox is not None and inbox.repo.lower() not in seen:
            findings.append(
                Finding(
                    "error",
                    f"projects[{index}].driver_inbox repo {inbox.repo} is not a configured project",
                )
            )
            ok = False
    if not ok:
        return None
    return tuple(projects)


def _linear_project_ids(
    item: dict,
    index: int,
    seen_linear: set[str],
    allow_placeholders: bool,
    findings: list[Finding],
) -> frozenset[str] | None:
    where = f"projects[{index}]"
    if "linear_project_id" in item and "linear_project_ids" in item:
        findings.append(
            Finding("error", f"{where} sets both linear_project_id and linear_project_ids")
        )
        return None
    if "linear_project_ids" in item:
        key = "linear_project_ids"
        raw = item[key]
        if not isinstance(raw, list) or not raw:
            findings.append(
                Finding("error", f"{where}.{key} must be a non-empty list of project ids")
            )
            return None
    else:
        key = "linear_project_id"
        single = item.get(key)
        raw = [] if single is None else [single]
    ids: set[str] = set()
    for value in raw:
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 200
            or (value.startswith("REPLACE_WITH_") and not allow_placeholders)
        ):
            findings.append(Finding("error", f"{where}.{key} must be a project id"))
            return None
        if value.startswith("REPLACE_WITH_"):
            findings.append(Finding("warning", f"accepted placeholder {where}.{key}"))
        if value in ids:
            findings.append(Finding("error", f"{where}.{key} names {value!r} twice"))
            return None
        ids.add(value)
    ok = True
    for value in sorted(ids):
        if value in seen_linear:
            findings.append(Finding("error", f"duplicate Linear project {value!r}"))
            ok = False
    seen_linear.update(ids)
    return frozenset(ids) if ok else None


def _intake(
    value: object,
    index: int,
    linear_project_ids: frozenset[str],
    findings: list[Finding],
) -> Intake | None:
    where = f"projects[{index}].intake"
    linear = bool(linear_project_ids)
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
    priority_labels: tuple[str, ...] = ()
    if "priority_labels" in value:
        raw = value["priority_labels"]
        if not isinstance(raw, list) or not all(
            isinstance(entry, str) and _LABEL.fullmatch(entry) for entry in raw
        ):
            findings.append(Finding("error", f"{where}.priority_labels must be a list of label names"))
            ok = False
        else:
            seen: set[str] = set()
            for entry in raw:
                if entry.lower() in seen:
                    findings.append(Finding("error", f"{where}.priority_labels names {entry.lower()} twice"))
                    ok = False
                seen.add(entry.lower())
            priority_labels = tuple(raw)
    from_linear = value.get("priority_from_linear", False)
    critical_path = value.get("critical_path_order", True)
    for key, setting in (("priority_from_linear", from_linear), ("critical_path_order", critical_path)):
        if not isinstance(setting, bool):
            findings.append(Finding("error", f"{where}.{key} must be true or false"))
            ok = False
    if from_linear is True and not linear:
        findings.append(Finding("error", f"{where}.priority_from_linear requires linear_project_id"))
        ok = False
    assignees: Mapping[str, frozenset[str]] = MappingProxyType({})
    if "linear_assignees" in value:
        parsed = _linear_assignees(value["linear_assignees"], where, linear_project_ids, findings)
        if parsed is None:
            ok = False
        else:
            assignees = parsed
    base_branches: Mapping[str, str] = MappingProxyType({})
    if "base_branches" in value:
        branches = _base_branches(value["base_branches"], where, linear_project_ids, findings)
        if branches is None:
            ok = False
        else:
            base_branches = branches
    if not ok:
        return None
    return Intake(
        label=None if linear else label,
        linear_state=state if linear else None,
        trusted=trusted,
        priority_labels=priority_labels,
        priority_from_linear=from_linear,
        critical_path_order=critical_path,
        linear_assignees=assignees,
        base_branches=base_branches,
    )


def _linear_assignees(
    value: object,
    where: str,
    linear_project_ids: frozenset[str],
    findings: list[Finding],
) -> Mapping[str, frozenset[str]] | None:
    if not linear_project_ids:
        findings.append(Finding("error", f"{where}.linear_assignees needs linear_project_id"))
        return None
    if not isinstance(value, dict) or not value:
        findings.append(Finding("error", f"{where}.linear_assignees must be a non-empty mapping"))
        return None
    result: dict[str, frozenset[str]] = {}
    ok = True
    for project_id, users in value.items():
        if project_id not in linear_project_ids:
            findings.append(
                Finding("error", f"{where}.linear_assignees names a project this entry does not watch")
            )
            ok = False
        elif not isinstance(users, list) or not users or not all(
            isinstance(user, str) and _LINEAR_USER_ID.fullmatch(user) for user in users
        ):
            findings.append(
                Finding(
                    "error",
                    f"{where}.linear_assignees.{project_id} must be a non-empty list of Linear user ids",
                )
            )
            ok = False
        else:
            result[project_id] = frozenset(users)
    return MappingProxyType(result) if ok else None


def _base_branches(
    value: object,
    where: str,
    linear_project_ids: frozenset[str],
    findings: list[Finding],
) -> Mapping[str, str] | None:
    if not linear_project_ids:
        findings.append(Finding("error", f"{where}.base_branches needs linear_project_id"))
        return None
    if not isinstance(value, dict) or not value:
        findings.append(Finding("error", f"{where}.base_branches must be a non-empty mapping"))
        return None
    result: dict[str, str] = {}
    ok = True
    for project_id, branch in value.items():
        if project_id not in linear_project_ids:
            findings.append(
                Finding("error", f"{where}.base_branches names a project this entry does not watch")
            )
            ok = False
        elif not _plain_branch(branch):
            findings.append(
                Finding("error", f"{where}.base_branches must map each project to a plain branch name")
            )
            ok = False
        else:
            result[project_id] = branch
    return MappingProxyType(result) if ok else None


def _plain_branch(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return bool(plain_branch(value))
    except RemoteError:
        return False


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
    alert = value.get("tunnel_alert_minutes", DEFAULT_TUNNEL_ALERT_MINUTES)
    if not is_strict_int(alert) or alert < 1:
        findings.append(
            Finding("error", "linear_webhook.tunnel_alert_minutes must be a positive integer")
        )
    public_url = value.get("public_url")
    if public_url is not None and not _public_url_ok(public_url, path):
        findings.append(
            Finding(
                "error",
                "linear_webhook.public_url must be an https URL whose path is "
                "linear_webhook.path",
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
        public_url=public_url,
        tunnel_alert_minutes=int(alert),
    )


def _public_url_ok(url: object, path: object) -> bool:
    if not isinstance(url, str) or _PUBLIC_URL.fullmatch(url) is None:
        return False
    return urlsplit(url).path == path


def _job_states(data: dict, findings: list[Finding]) -> JobStateConfig | None:
    """job_states (#75): when a running job counts as stale, how long a
    claimed job that was never started waits before the poller requeues it,
    and how long a device stays unreachable before its running jobs are lost.
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
        ("unreachable_lost_minutes", DEFAULT_UNREACHABLE_LOST_MINUTES),
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
        unreachable_lost_seconds=minutes["unreachable_lost_minutes"] * 60.0,
    )


def _approver_logins(data: dict, findings: list[Finding]) -> frozenset[str] | None:
    """GitHub user logins whose capped-ticket go or hold ok counts once the
    app switch is on. An app's [bot] login can never approve."""
    if "approver_logins" not in data:
        return None
    value = data["approver_logins"]
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and _USER_LOGIN.fullmatch(item) for item in value)
    ):
        findings.append(
            Finding("error", "approver_logins must list GitHub user logins (no [bot] logins)")
        )
        return None
    return frozenset(value)


def _approver_linear_users(data: dict, findings: list[Finding]) -> frozenset[str] | None:
    if "approver_linear_users" not in data:
        return None
    value = data["approver_linear_users"]
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        findings.append(
            Finding("error", "approver_linear_users must list stable Linear user ids")
        )
        return None
    return frozenset(f"linear:{item}" for item in value)


def _absolute_path(
    data: Mapping[str, object],
    key: str,
    where: str,
    findings: list[Finding],
) -> Path | None:
    """An absolute path, never opened. No ~: it would expand to whichever
    user reads the file."""
    if key not in data:
        return None
    value = data[key]
    if isinstance(value, str) and Path(value).is_absolute():
        return Path(value)
    findings.append(Finding("error", f"{where} must be an absolute path"))
    return None


def _github_app(data: dict, findings: list[Finding]) -> GitHubApps | None:
    if "github_app" not in data:
        return None
    value = data["github_app"]
    if not isinstance(value, dict):
        findings.append(Finding("error", "github_app must be a mapping"))
        return None
    start = len(findings)
    for key in _MOVED_APP_KEYS:
        if key in value:
            findings.append(
                Finding("error", f"github_app.{key} moved to github_app.apps.<bot>.{key} (#170)")
            )
    _unknown_keys(
        {key: item for key, item in value.items() if key not in _MOVED_APP_KEYS},
        _GITHUB_APP_KEYS,
        "github_app",
        findings,
    )
    enabled = value.get("enabled", False)
    if not isinstance(enabled, bool):
        findings.append(Finding("error", "github_app.enabled must be true or false"))
    entries = value.get("apps")
    if not isinstance(entries, dict) or not entries:
        findings.append(
            Finding("error", f"github_app.apps must map bots ({', '.join(APP_BOTS)}) to their Apps")
        )
        return None
    apps: dict[str, GitHubApp] = {}
    for bot in sorted(entries, key=lambda item: str(item)):
        if bot not in APP_BOTS:
            findings.append(Finding("error", f"unknown field 'github_app.apps.{bot}'"))
            continue
        app = _one_app(bot, entries[bot], findings)
        if app is not None:
            apps[bot] = app
    for field_name in ("bot_login", "app_id"):
        seen: dict[object, str] = {}
        for bot, app in apps.items():
            key = getattr(app, field_name)
            key = key.casefold() if isinstance(key, str) else key
            if key in seen:
                findings.append(
                    Finding(
                        "error",
                        f"github_app.apps.{seen[key]} and github_app.apps.{bot} "
                        f"share {field_name}; each bot needs its own App",
                    )
                )
            seen.setdefault(key, bot)
    if len(findings) > start:
        return None
    return GitHubApps(apps=MappingProxyType(apps), enabled=enabled)


def _one_app(bot: str, value: object, findings: list[Finding]) -> GitHubApp | None:
    where = f"github_app.apps.{bot}"
    if not isinstance(value, dict):
        findings.append(Finding("error", f"{where} must be a mapping"))
        return None
    start = len(findings)
    _unknown_keys(value, _APP_KEYS, where, findings)
    bot_login = value.get("bot_login")
    if bot_login is None:
        findings.append(Finding("error", f"{where}.bot_login is required"))
    elif not isinstance(bot_login, str) or _BOT_LOGIN.fullmatch(bot_login) is None:
        findings.append(Finding("error", f"{where}.bot_login must be an app login, <name>[bot]"))
    app_id = value.get("app_id")
    if not is_strict_int(app_id) or app_id < 1:
        findings.append(Finding("error", f"{where}.app_id must be a positive integer"))
    installations = value.get("installations")
    if installations is None or installations == {}:
        findings.append(Finding("error", f"{where}.installations is required"))
    elif not isinstance(installations, dict) or not all(
        isinstance(account, str)
        and _USER_LOGIN.fullmatch(account)
        and is_strict_int(number)
        and number >= 1
        for account, number in installations.items()
    ):
        findings.append(
            Finding(
                "error",
                f"{where}.installations must map an account login to a positive installation id",
            )
        )
    if "private_key_file" not in value:
        findings.append(Finding("error", f"{where}.private_key_file must be an absolute path"))
    private_key_file = _absolute_path(
        value, "private_key_file", f"{where}.private_key_file", findings
    )
    if len(findings) > start:
        return None
    assert private_key_file is not None
    return GitHubApp(
        bot=bot,
        bot_login=bot_login,
        app_id=int(app_id),
        installations=MappingProxyType(dict(installations)),
        private_key_file=private_key_file,
    )


def _app_switch(
    github_app: GitHubApps,
    approver_logins: frozenset[str] | None,
    trusted_logins: Mapping[str, frozenset[str]],
    projects: tuple[Project, ...] | None,
    findings: list[Finding],
) -> None:
    owners = sorted(
        {project.repo.split("/", 1)[0].casefold() for project in projects or ()}
    )
    for bot, app in github_app.apps.items():
        where = f"github_app.apps.{bot}"
        spellings = {
            login: bots
            for login, bots in trusted_logins.items()
            if login.casefold() == app.bot_login.casefold()
        }
        if not spellings:
            findings.append(Finding("error", f"{where}.bot_login must be a trusted_logins key"))
        for login, bots in spellings.items():
            if bots != frozenset({bot}):
                findings.append(
                    Finding(
                        "error",
                        f"trusted_logins {login} must be [{bot}] alone "
                        f"while github_app.enabled is true",
                    )
                )
        installed = {account.casefold() for account in app.installations}
        for owner in owners:
            if owner not in installed:
                findings.append(
                    Finding("error", f"{where}.installations has no installation for {owner}")
                )
    if approver_logins is None:
        findings.append(Finding("error", "github_app.enabled requires approver_logins"))


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


def _driver(data: dict, findings: list[Finding]) -> Driver:
    value = data.get("driver", "grok")
    if value == "claude":
        return "claude"
    if value != "grok":
        findings.append(Finding("error", "driver must be grok or claude"))
    return "grok"


def _claude(data: dict, findings: list[Finding]) -> ClaudeDriverConfig | None:
    value = data.get("claude")
    if value is None:
        return None
    if not isinstance(value, dict):
        findings.append(Finding("error", "claude must be a mapping"))
        return None
    _unknown_keys(value, {"fire_url_env", "fire_token_env"}, "claude", findings)
    url_name = _env_name(value.get("fire_url_env"), "claude.fire_url_env", findings)
    token_name = _env_name(
        value.get("fire_token_env"), "claude.fire_token_env", findings
    )
    if url_name is None or token_name is None:
        return None
    return ClaudeDriverConfig(url_name, token_name)


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


def _qa_storage(value: object, where: str, findings: list[Finding]) -> QaStorage | None:
    if not isinstance(value, dict):
        findings.append(Finding("error", f"{where} must be a mapping"))
        return None
    before = len(findings)
    _unknown_keys(value, _QA_STORAGE_KEYS, where, findings)
    for key in _QA_STORAGE_REQUIRED:
        if key not in value:
            findings.append(Finding("error", f"missing required field '{where}.{key}'"))
    if len(findings) > before:
        return None
    bucket = value["bucket"]
    if not isinstance(bucket, str) or _BUCKET.fullmatch(bucket) is None:
        findings.append(Finding("error", "qa_storage.bucket must be an S3 bucket name"))
    region = value["region"]
    if not isinstance(region, str) or _REGION.fullmatch(region) is None:
        findings.append(Finding("error", "qa_storage.region must be a region name"))
    endpoint = value.get("endpoint")
    if endpoint is not None and not _https_url(endpoint):
        findings.append(Finding("error", "qa_storage.endpoint must be an https URL"))
    access = _env_name(value["access_key_env"], "qa_storage.access_key_env", findings)
    secret = _env_name(value["secret_key_env"], "qa_storage.secret_key_env", findings)
    root = _absolute_path(value, "evidence_root", "qa_storage.evidence_root", findings)
    if len(findings) > before or access is None or secret is None or root is None:
        return None
    return QaStorage(bucket, region, endpoint, access, secret, root)


def _https_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parts = urlsplit(value)
    return (
        parts.scheme == "https"
        and bool(parts.hostname)
        and not parts.username
        and not parts.password
        and not parts.query
        and not parts.fragment
    )


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
