from dataclasses import replace
from pathlib import Path

import pytest

from lib.orch_yml import (
    DEFAULT_JOB_TIMEOUT_MINUTES,
    POLL_SECONDS,
    DriverInbox,
    load_orch_yml,
    resolve_job_timeout,
)

ROOT = Path(__file__).resolve().parents[1]


def test_repo_orch_yml_lists_projects_and_polls_github_every_30_seconds(
    tmp_path: Path,
) -> None:
    configured = tmp_path / "orch.yml"
    configured.write_text(
        (ROOT / "orch.yml").read_text(encoding="utf-8").replace(
            "REPLACE_WITH_LINEAR_PROJECT_ID",
            "test-linear-project-id",
        ),
        encoding="utf-8",
    )
    config, findings = load_orch_yml(configured)
    assert findings == []
    assert config is not None
    assert config.poll_seconds == 30
    assert POLL_SECONDS == 60
    assert config.job_timeout is None
    assert config.linear is not None
    assert config.linear.api_key_env == "LINEAR_API_KEY"
    assert config.grok is not None
    assert config.grok.webhook_url_env == "FACTORY_WEBHOOK_URL"
    assert config.grok.webhook_key_env == "FACTORY_WEBHOOK_KEY"
    assert config.grok.webhook_auth_env == "FACTORY_WEBHOOK_AUTH"
    assert config.trusted_logins == {
        "maxharris1": frozenset(
            {"grok-bot", "claude-bot", "codex-bot", "cursor-bot"}
        )
    }
    assert config.trusted_linear_users == {}
    assert [project.repo for project in config.projects] == [
        "aut0-code/aut0",
        "maxharris1/Cleffy",
        "maxharris1/tracy-harris-coaching",
        "maxharris1/factory",
        "maxharris1/coo",
    ]
    assert config.projects[0].job_timeout == 150
    assert resolve_job_timeout(config, "aut0-code/aut0") == 150
    for project in config.projects[1:3]:
        assert project.job_timeout is None
        assert resolve_job_timeout(config, project.repo) == DEFAULT_JOB_TIMEOUT_MINUTES == 60
    assert config.projects[3].job_timeout == 120
    assert resolve_job_timeout(config, "maxharris1/factory") == 120
    assert config.projects[0].factory_ref == "dev"
    assert config.projects[0].linear_project_ids == frozenset({"test-linear-project-id"})
    assert config.projects[0].linear_ready_for_review is True
    assert config.projects[0].grok_bot == "offer-brief-project"


def test_placeholder_linear_project_id_is_rejected() -> None:
    config, findings = load_orch_yml(ROOT / "orch.yml")

    assert config is None
    assert any(
        "linear_project_id must be a project id" in finding.message
        for finding in findings
    )


def test_placeholder_linear_project_id_can_be_allowed_for_validation() -> None:
    config, findings = load_orch_yml(
        ROOT / "orch.yml",
        allow_placeholders=True,
    )

    assert [finding.message for finding in findings] == [
        "accepted placeholder projects[0].linear_project_id"
    ]
    assert config is not None
    assert (
        config.projects[0].linear_project_ids
        == frozenset({"REPLACE_WITH_LINEAR_PROJECT_ID"})
    )


def test_minimal_example_has_no_extra_required_fields() -> None:
    config, findings = load_orch_yml(ROOT / "config/orch.example.yml")
    assert findings == []
    assert config is not None
    assert config.poll_seconds == 30
    assert config.job_timeout is None
    assert config.projects[0].repo == "owner/name"
    assert config.projects[0].job_timeout is None
    assert config.projects[0].linear_ready_for_review is False
    assert resolve_job_timeout(config, "owner/name") == 60


def test_grok_webhook_auth_env_defaults_and_can_be_renamed(tmp_path: Path) -> None:
    path = tmp_path / "orch.yml"
    base = """
poll_seconds: 60
grok:
  webhook_url_env: FACTORY_WEBHOOK_URL
  webhook_key_env: FACTORY_WEBHOOK_KEY
projects:
  - repo: acme/widgets
""".strip()
    path.write_text(base + "\n", encoding="utf-8")
    config, findings = load_orch_yml(path)
    assert findings == []
    assert config is not None and config.grok is not None
    assert config.grok.webhook_auth_env == "FACTORY_WEBHOOK_AUTH"

    path.write_text(
        base.replace(
            "grok:\n",
            "grok:\n  webhook_auth_env: OTHER_WEBHOOK_AUTH\n",
        )
        + "\n",
        encoding="utf-8",
    )
    config, findings = load_orch_yml(path)
    assert findings == []
    assert config is not None and config.grok is not None
    assert config.grok.webhook_auth_env == "OTHER_WEBHOOK_AUTH"


CLAUDE_BLOCK = """claude:
  fire_url_env: FACTORY_CLAUDE_FIRE_URL
  fire_token_env: FACTORY_CLAUDE_FIRE_TOKEN
"""


def _driver_config(tmp_path: Path, extra: str):
    path = tmp_path / "orch.yml"
    path.write_text(
        "poll_seconds: 60\n" + extra + "projects:\n  - repo: acme/widgets\n",
        encoding="utf-8",
    )
    return load_orch_yml(path)


def test_driver_claude_reads_the_claude_block(tmp_path: Path) -> None:
    config, findings = _driver_config(tmp_path, "driver: claude\n" + CLAUDE_BLOCK)
    assert findings == []
    assert config is not None and config.claude is not None
    assert config.driver == "claude"
    assert config.claude.fire_url_env == "FACTORY_CLAUDE_FIRE_URL"
    assert config.claude.fire_token_env == "FACTORY_CLAUDE_FIRE_TOKEN"


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("driver: codex\n" + CLAUDE_BLOCK, "driver must be grok or claude"),
        ("driver: claude\n", "driver: claude requires a claude block"),
        (
            "claude:\n  fire_url_env: FACTORY_CLAUDE_FIRE_URL\n",
            "claude.fire_token_env must be an environment variable name",
        ),
    ],
    ids=["unknown-driver", "claude-without-block", "claude-without-token"],
)
def test_check_config_refuses_a_bad_driver(
    tmp_path: Path, extra: str, message: str
) -> None:
    config, findings = _driver_config(tmp_path, extra)
    assert config is None
    assert [(f.severity, f.message) for f in findings] == [("error", message)]


def test_project_timeout_overrides_file_and_default(tmp_path: Path) -> None:
    path = tmp_path / "orch.yml"
    path.write_text(
        """
poll_seconds: 60
job_timeout: 120
projects:
  - repo: acme/widgets
    job_timeout: 15
  - repo: acme/other
""".strip()
        + "\n",
        encoding="utf-8",
    )
    config, findings = load_orch_yml(path)
    assert findings == []
    assert config is not None
    assert resolve_job_timeout(config, "acme/widgets") == 15
    assert resolve_job_timeout(config, "acme/other") == 120


def test_project_timeout_overrides_the_60_minute_default(tmp_path: Path) -> None:
    path = tmp_path / "orch.yml"
    path.write_text(
        """
poll_seconds: 60
projects:
  - repo: acme/widgets
    job_timeout: 30
  - repo: acme/other
""".strip()
        + "\n",
        encoding="utf-8",
    )
    config, findings = load_orch_yml(path)
    assert findings == []
    assert config is not None
    assert resolve_job_timeout(config, "acme/widgets") == 30
    assert resolve_job_timeout(config, "acme/other") == 60


def test_default_job_timeout_is_60_minutes_everywhere_it_is_stated() -> None:
    """Factory decided (technical), Nov 11: a job goes stuck after 60 minutes
    with no heartbeat or progress, not 90. Code defaults and every place the
    default is written down must agree."""
    from lib.dispatcher import TrackedTicket

    assert DEFAULT_JOB_TIMEOUT_MINUTES == 60
    default = TrackedTicket.__dataclass_fields__["job_timeout_minutes"].default
    assert default == DEFAULT_JOB_TIMEOUT_MINUTES
    stated = [
        "orch.yml",
        "config/orch.example.yml",
        "README.md",
        "docs/SPEC.md",
        "lib/orch_yml.py",
        "lib/dispatcher.py",
    ]
    for name in stated:
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "90 minutes" not in text, name
        assert "job_timeout: 90" not in text, name
    assert "60 minutes" in (ROOT / "orch.yml").read_text(encoding="utf-8")
    assert "60 minutes" in (ROOT / "config/orch.example.yml").read_text(encoding="utf-8")


def test_poll_seconds_must_be_30_or_60(tmp_path: Path) -> None:
    path = tmp_path / "orch.yml"
    path.write_text("poll_seconds: 45\nprojects:\n  - repo: acme/widgets\n", encoding="utf-8")
    config, findings = load_orch_yml(path)
    assert config is None
    assert any("poll_seconds must be 30 or 60" in finding.message for finding in findings)


def test_unknown_field_is_not_required_and_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "orch.yml"
    path.write_text(
        "poll_seconds: 60\nlog_path: /tmp/orch.log\nprojects:\n  - repo: acme/widgets\n",
        encoding="utf-8",
    )
    config, findings = load_orch_yml(path)
    assert config is None
    assert any("unknown field 'log_path'" in finding.message for finding in findings)


def test_linear_project_requires_api_key_env_name(tmp_path: Path) -> None:
    path = tmp_path / "orch.yml"
    path.write_text(
        "poll_seconds: 60\nprojects:\n  - repo: acme/widgets\n"
        "    factory_ref: main\n"
        "    linear_project_id: project-1\n",
        encoding="utf-8",
    )

    config, findings = load_orch_yml(path)

    assert config is None
    assert any(
        "linear.api_key_env is required" in finding.message
        for finding in findings
    )


def test_trust_maps_accept_github_app_login_and_prefix_linear_id(
    tmp_path: Path,
) -> None:
    path = tmp_path / "orch.yml"
    path.write_text(
        """
poll_seconds: 60
trusted_logins:
  factory-app[bot]: [claude-bot, codex-bot]
trusted_linear_users:
  8f7b-user-id: [grok-bot]
projects:
  - repo: acme/widgets
    factory_ref: main
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config, findings = load_orch_yml(path)

    assert findings == []
    assert config is not None
    assert config.trusted_logins == {
        "factory-app[bot]": frozenset({"claude-bot", "codex-bot"})
    }
    assert config.trusted_linear_users == {
        "linear:8f7b-user-id": frozenset({"grok-bot"})
    }


def test_duplicate_repos_are_case_insensitive(tmp_path: Path) -> None:
    path = tmp_path / "orch.yml"
    path.write_text(
        """
poll_seconds: 60
projects:
  - repo: Acme/Widgets
    job_timeout: 15
  - repo: acme/widgets
    job_timeout: 30
""".strip()
        + "\n",
        encoding="utf-8",
    )
    config, findings = load_orch_yml(path)
    assert config is None
    assert any("duplicate project" in finding.message for finding in findings)


def test_timeout_lookup_is_case_insensitive(tmp_path: Path) -> None:
    path = tmp_path / "orch.yml"
    path.write_text(
        """
poll_seconds: 60
projects:
  - repo: Acme/Widgets
    job_timeout: 15
""".strip()
        + "\n",
        encoding="utf-8",
    )
    config, findings = load_orch_yml(path)
    assert findings == []
    assert config is not None
    assert resolve_job_timeout(config, "acme/widgets") == 15
    assert resolve_job_timeout(config, "ACME/WIDGETS") == 15


def test_job_timeout_must_be_a_positive_integer(tmp_path: Path) -> None:
    path = tmp_path / "orch.yml"
    path.write_text(
        "poll_seconds: 60\njob_timeout: 0\nprojects:\n  - repo: acme/widgets\n",
        encoding="utf-8",
    )
    config, findings = load_orch_yml(path)
    assert config is None
    assert any("job_timeout must be a positive integer" in finding.message for finding in findings)


WEBHOOK_BASE = """
poll_seconds: 60
linear:
  api_key_env: LINEAR_KEY
projects:
  - repo: acme/widgets
    factory_ref: main
    linear_project_id: project-1
""".lstrip()


def _webhook_config(tmp_path: Path, section: str):
    path = tmp_path / "orch.yml"
    path.write_text(WEBHOOK_BASE + section, encoding="utf-8")
    return load_orch_yml(path)


def test_no_linear_webhook_is_configured_by_default() -> None:
    for name in ("orch.yml", "config/orch.example.yml"):
        config, _findings = load_orch_yml(ROOT / name, allow_placeholders=True)
        assert config is not None
        assert config.linear_webhook is None


def test_linear_webhook_section_parses_bind_secret_path_and_defaults(
    tmp_path: Path,
) -> None:
    config, findings = _webhook_config(
        tmp_path,
        "linear_webhook:\n"
        "  bind: 127.0.0.1:8787\n"
        "  secret_file: /home/box/.config/factory/linear-webhook.secret\n",
    )

    assert findings == []
    assert config is not None and config.linear_webhook is not None
    webhook = config.linear_webhook
    assert (webhook.host, webhook.port) == ("127.0.0.1", 8787)
    assert webhook.secret_file == Path(
        "/home/box/.config/factory/linear-webhook.secret"
    )
    assert webhook.path == "/linear-webhook"
    assert webhook.reconcile_seconds == 900
    assert webhook.tunnel_alert_minutes == 30


def test_linear_webhook_accepts_overrides_and_expands_home(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config, findings = _webhook_config(
        tmp_path,
        "linear_webhook:\n"
        "  bind: '[::1]:9000'\n"
        "  path: /hooks/linear\n"
        "  secret_file: ~/.config/factory/linear-webhook.secret\n"
        "  reconcile_seconds: 600\n"
        "  tunnel_alert_minutes: 90\n",
    )

    assert findings == []
    assert config is not None and config.linear_webhook is not None
    webhook = config.linear_webhook
    assert (webhook.host, webhook.port) == ("::1", 9000)
    assert webhook.path == "/hooks/linear"
    assert webhook.secret_file == tmp_path / ".config/factory/linear-webhook.secret"
    assert webhook.reconcile_seconds == 600
    assert webhook.tunnel_alert_minutes == 90


def test_linear_webhook_public_url_is_the_funnel_url_of_its_path(tmp_path: Path) -> None:
    config, findings = _webhook_config(
        tmp_path,
        "linear_webhook:\n"
        "  bind: 127.0.0.1:8787\n"
        "  secret_file: /s\n"
        "  public_url: https://box.tail68624b.ts.net/linear-webhook\n",
    )

    assert findings == []
    assert config is not None and config.linear_webhook is not None
    assert config.linear_webhook.public_url == "https://box.tail68624b.ts.net/linear-webhook"


def test_linear_webhook_check_does_not_read_the_secret_file(tmp_path: Path) -> None:
    config, findings = _webhook_config(
        tmp_path,
        "linear_webhook:\n"
        "  bind: 127.0.0.1:8787\n"
        f"  secret_file: {tmp_path / 'absent.secret'}\n",
    )

    assert findings == []
    assert config is not None


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ("linear_webhook: yes\n", "linear_webhook must be a mapping"),
        (
            "linear_webhook:\n  secret_file: /s\n",
            "linear_webhook.bind must be host:port",
        ),
        (
            "linear_webhook:\n  bind: 8787\n  secret_file: /s\n",
            "linear_webhook.bind must be host:port",
        ),
        (
            "linear_webhook:\n  bind: example.com:8787\n  secret_file: /s\n",
            "linear_webhook.bind must be host:port",
        ),
        (
            "linear_webhook:\n  bind: 127.0.0.1:70000\n  secret_file: /s\n",
            "linear_webhook.bind must be host:port",
        ),
        (
            "linear_webhook:\n  bind: 127.0.0.1:0\n  secret_file: /s\n",
            "linear_webhook.bind must be host:port",
        ),
        (
            "linear_webhook:\n  bind: 127.0.0.1:8787\n",
            "linear_webhook.secret_file must be an absolute path",
        ),
        (
            "linear_webhook:\n  bind: 127.0.0.1:8787\n  secret_file: relative.secret\n",
            "linear_webhook.secret_file must be an absolute path",
        ),
        (
            "linear_webhook:\n  bind: 127.0.0.1:8787\n  secret_file: /s\n"
            "  path: no-slash\n",
            "linear_webhook.path must be an absolute URL path",
        ),
        (
            "linear_webhook:\n  bind: 127.0.0.1:8787\n  secret_file: /s\n"
            "  path: /a?b\n",
            "linear_webhook.path must be an absolute URL path",
        ),
        (
            "linear_webhook:\n  bind: 127.0.0.1:8787\n  secret_file: /s\n"
            "  public_url: http://box.example.ts.net/linear-webhook\n",
            "linear_webhook.public_url must be an https URL whose path is linear_webhook.path",
        ),
        (
            "linear_webhook:\n  bind: 127.0.0.1:8787\n  secret_file: /s\n"
            "  public_url: https://box.example.ts.net/other\n",
            "linear_webhook.public_url must be an https URL whose path is linear_webhook.path",
        ),
        (
            "linear_webhook:\n  bind: 127.0.0.1:8787\n  secret_file: /s\n"
            "  public_url: https://user:pw@box.example.ts.net/linear-webhook\n",
            "linear_webhook.public_url must be an https URL whose path is linear_webhook.path",
        ),
        (
            "linear_webhook:\n  bind: 127.0.0.1:8787\n  secret_file: /s\n"
            "  public_url: https://box.example.ts.net/linear-webhook?k=v\n",
            "linear_webhook.public_url must be an https URL whose path is linear_webhook.path",
        ),
        (
            "linear_webhook:\n  bind: 127.0.0.1:8787\n  secret_file: /s\n"
            "  reconcile_seconds: 30\n",
            "linear_webhook.reconcile_seconds must be an integer from 60 to 3600",
        ),
        (
            "linear_webhook:\n  bind: 127.0.0.1:8787\n  secret_file: /s\n"
            "  reconcile_seconds: true\n",
            "linear_webhook.reconcile_seconds must be an integer from 60 to 3600",
        ),
        (
            "linear_webhook:\n  bind: 127.0.0.1:8787\n  secret_file: /s\n"
            "  tunnel_alert_minutes: 0\n",
            "linear_webhook.tunnel_alert_minutes must be a positive integer",
        ),
        (
            "linear_webhook:\n  bind: 127.0.0.1:8787\n  secret_file: /s\n"
            "  tunnel_alert_minutes: true\n",
            "linear_webhook.tunnel_alert_minutes must be a positive integer",
        ),
        (
            "linear_webhook:\n  bind: 127.0.0.1:8787\n  secret_file: /s\n"
            "  secret: inline\n",
            "unknown field 'linear_webhook.secret'",
        ),
    ],
)
def test_linear_webhook_section_is_validated(
    tmp_path: Path,
    section: str,
    message: str,
) -> None:
    config, findings = _webhook_config(tmp_path, section)

    assert config is None
    assert message in [finding.message for finding in findings]


def test_linear_webhook_requires_a_linear_project(tmp_path: Path) -> None:
    path = tmp_path / "orch.yml"
    path.write_text(
        "poll_seconds: 60\nprojects:\n  - repo: acme/widgets\n"
        "linear_webhook:\n  bind: 127.0.0.1:8787\n  secret_file: /s\n",
        encoding="utf-8",
    )

    config, findings = load_orch_yml(path)

    assert config is None
    assert "linear_webhook requires a project with linear_project_id" in [
        finding.message for finding in findings
    ]


def test_refused_harnesses_defaults_empty_and_the_shipped_file_refuses_cursor(
    tmp_path: Path,
) -> None:
    path = tmp_path / "orch.yml"
    path.write_text("poll_seconds: 30\nprojects:\n  - repo: a/b\n", encoding="utf-8")
    config, findings = load_orch_yml(path)
    assert findings == [] and config is not None
    assert config.refused_harnesses == ()
    shipped, _ = load_orch_yml(ROOT / "orch.yml", allow_placeholders=True)
    assert shipped is not None
    assert shipped.refused_harnesses == ("cursor",)


@pytest.mark.parametrize("value, message", [
    ("[gemini]", "refused_harnesses has unknown harness 'gemini'"),
    ("[cursor, cursor]", "refused_harnesses lists a harness twice"),
    ("cursor", "refused_harnesses must be a list of harness names"),
    ("[1]", "refused_harnesses must be a list of harness names"),
])
def test_refused_harnesses_is_validated(tmp_path: Path, value: str, message: str) -> None:
    path = tmp_path / "orch.yml"
    path.write_text(
        f"poll_seconds: 30\nrefused_harnesses: {value}\nprojects:\n  - repo: a/b\n",
        encoding="utf-8",
    )
    config, findings = load_orch_yml(path)
    assert config is None
    assert any(f.severity == "error" and f.message.startswith(message) for f in findings)


INBOX_BASE = """
poll_seconds: 60
projects:
  - repo: maxharris1/factory
  - repo: aut0-code/aut0
"""


def _inbox_config(tmp_path: Path, factory_keys: str = "", aut0_keys: str = ""):
    text = INBOX_BASE.replace(
        "  - repo: maxharris1/factory\n", f"  - repo: maxharris1/factory\n{factory_keys}"
    ).replace("  - repo: aut0-code/aut0\n", f"  - repo: aut0-code/aut0\n{aut0_keys}")
    path = tmp_path / "orch.yml"
    path.write_text(text, encoding="utf-8")
    return load_orch_yml(path)


def test_driver_inbox_names_one_issue_in_a_configured_project(tmp_path: Path) -> None:
    config, findings = _inbox_config(
        tmp_path,
        aut0_keys="    driver_inbox: maxharris1/factory#737\n    grok_webhook: false\n",
    )
    assert findings == []
    assert config is not None
    factory, aut0 = config.projects
    assert factory.driver_inbox is None and factory.grok_webhook is True
    assert aut0.driver_inbox == DriverInbox("maxharris1/factory", 737)
    assert aut0.grok_webhook is False


@pytest.mark.parametrize(
    "value",
    ["maxharris1/factory", "maxharris1/factory#0", "factory#3", "maxharris1/factory#x", "7"],
)
def test_check_config_refuses_a_malformed_driver_inbox(tmp_path: Path, value: str) -> None:
    config, findings = _inbox_config(tmp_path, factory_keys=f"    driver_inbox: '{value}'\n")
    assert config is None
    assert [f.message for f in findings] == [
        "projects[0].driver_inbox must be owner/name#<issue number>"
    ]


def test_check_config_refuses_a_driver_inbox_outside_the_projects(tmp_path: Path) -> None:
    config, findings = _inbox_config(tmp_path, factory_keys="    driver_inbox: someone/else#3\n")
    assert config is None
    assert [f.message for f in findings] == [
        "projects[0].driver_inbox repo someone/else is not a configured project"
    ]


def test_check_config_refuses_grok_webhook_off_without_an_inbox(tmp_path: Path) -> None:
    config, findings = _inbox_config(tmp_path, factory_keys="    grok_webhook: false\n")
    assert config is None
    assert [f.message for f in findings] == [
        "projects[0].grok_webhook: false requires driver_inbox, or its wakes go nowhere"
    ]


def test_linear_handoff_states_default_override_and_github_only_error(tmp_path: Path) -> None:
    path = tmp_path / "orch.yml"
    base = """
poll_seconds: 60
linear:
  api_key_env: LINEAR_KEY
projects:
  - repo: acme/widgets
    linear_project_id: 11111111-2222-3333-4444-555555555555
""".strip()
    path.write_text(base + "\n", encoding="utf-8")
    config, findings = load_orch_yml(path)
    assert findings == []
    assert config is not None
    assert config.projects[0].linear_handoff_states == ("Ready for Review", "In Review")

    path.write_text(base + "\n    linear_handoff_states: [QA Review]\n", encoding="utf-8")
    config, findings = load_orch_yml(path)
    assert findings == []
    assert config is not None
    assert config.projects[0].linear_handoff_states == ("QA Review",)

    path.write_text(
        base + "\n  - repo: acme/other\n    linear_handoff_states: [In Review]\n",
        encoding="utf-8",
    )
    config, findings = load_orch_yml(path)
    assert config is None
    assert [finding.message for finding in findings] == [
        "projects[1].linear_handoff_states requires linear_project_id"
    ]


_REWORK_BASE = """
poll_seconds: 60
linear:
  api_key_env: LINEAR_KEY
projects:
  - repo: acme/widgets
    linear_project_id: 11111111-2222-3333-4444-555555555555
""".strip()


def test_linear_rework_states_default_empty_and_a_list_parses(tmp_path: Path) -> None:
    path = tmp_path / "orch.yml"
    path.write_text(_REWORK_BASE + "\n", encoding="utf-8")
    config, findings = load_orch_yml(path)
    assert findings == []
    assert config is not None
    assert config.projects[0].linear_rework_states == ()

    path.write_text(
        _REWORK_BASE + "\n    linear_rework_states: [Changes Requested]\n", encoding="utf-8"
    )
    config, findings = load_orch_yml(path)
    assert findings == []
    assert config is not None
    assert config.projects[0].linear_rework_states == ("Changes Requested",)


@pytest.mark.parametrize(
    ("extra", "error"),
    [
        (
            "\n    linear_rework_states: Changes Requested\n",
            "projects[0].linear_rework_states must be a list of Linear state names",
        ),
        (
            "\n    linear_rework_states: [Changes Requested, 3]\n",
            "projects[0].linear_rework_states must be a list of Linear state names",
        ),
        (
            "\n    linear_rework_states: ['  ']\n",
            "projects[0].linear_rework_states must be a list of Linear state names",
        ),
        (
            "\n  - repo: acme/other\n    linear_rework_states: [Changes Requested]\n",
            "projects[1].linear_rework_states requires linear_project_id",
        ),
        (
            "\n    linear_rework_states: [Changes Requested, In Review]\n",
            "projects[0].linear_rework_states names a handoff state: In Review",
        ),
    ],
    ids=["string", "non-string", "blank", "github-only", "overlaps-handoff"],
)
def test_linear_rework_states_errors(tmp_path: Path, extra: str, error: str) -> None:
    path = tmp_path / "orch.yml"
    path.write_text(_REWORK_BASE + extra, encoding="utf-8")
    config, findings = load_orch_yml(path)
    assert config is None
    assert [finding.message for finding in findings] == [error]


@pytest.mark.parametrize(
    ("block", "lanes", "error"),
    [
        ("", 10, None),
        ("qa:\n  max_cloud_lanes: 2\n", 2, None),
        ("qa:\n  max_cloud_lanes: 0\n", None, "qa.max_cloud_lanes must be an integer of at least 1"),
        ("qa:\n  max_cloud_lanes: true\n", None, "qa.max_cloud_lanes must be an integer of at least 1"),
        ("qa:\n  lanes: 2\n", None, "qa.lanes"),
        ("qa: 3\n", None, "qa must be a mapping"),
    ],
)
def test_qa_max_cloud_lanes_default_and_bounds(tmp_path: Path, block: str, lanes: int | None, error: str | None) -> None:
    config, findings = _driver_config(tmp_path, block)
    if error is None:
        assert findings == []
        assert config is not None and config.qa.max_cloud_lanes == lanes
    else:
        assert config is None
        assert any(error in finding.message for finding in findings), findings


def test_qa_shares_usage_with_loads_and_defaults_empty(tmp_path: Path) -> None:
    config, findings = _driver_config(tmp_path, "")
    assert findings == []
    assert config is not None and dict(config.qa.shares_usage_with) == {}

    config, findings = _driver_config(tmp_path, "qa:\n  shares_usage_with:\n    claude-cloud: claude\n")
    assert findings == []
    assert config is not None and dict(config.qa.shares_usage_with) == {"claude-cloud": "claude"}


@pytest.mark.parametrize(
    ("block", "error"),
    [
        ("qa:\n  shares_usage_with: [claude-cloud]\n", "qa.shares_usage_with must be a mapping"),
        (
            "qa:\n  shares_usage_with:\n    gpu-cloud: claude\n",
            "qa.shares_usage_with has unknown runner 'gpu-cloud'; expected one of claude-cloud, cursor-cloud, local-docker",
        ),
    ],
)
def test_qa_shares_usage_with_refuses_an_unknown_runner(tmp_path: Path, block: str, error: str) -> None:
    config, findings = _driver_config(tmp_path, block)
    assert config is None
    assert error in [finding.message for finding in findings if finding.severity == "error"]


def test_qa_shares_usage_with_refuses_an_unknown_recipient(tmp_path: Path) -> None:
    config, findings = _driver_config(tmp_path, "qa:\n  shares_usage_with:\n    claude-cloud: grok\n")
    assert config is None
    assert "qa.shares_usage_with.claude-cloud has unknown harness 'grok'; expected one of claude, codex, cursor" in [
        finding.message for finding in findings if finding.severity == "error"
    ]


def _devices_config(tmp_path: Path, devices: str | None):
    path = tmp_path / "orch.yml"
    line = "" if devices is None else f"    devices: {devices}\n"
    path.write_text(
        "poll_seconds: 30\nprojects:\n  - repo: acme/widgets\n    job_timeout: 15\n" + line,
        encoding="utf-8",
    )
    return load_orch_yml(path)


@pytest.mark.parametrize(
    "devices, expected",
    [("[pc]", frozenset({"pc"})), ("[pc, mac]", frozenset({"pc", "mac"}))],
)
def test_project_devices_lists_the_devices_it_runs_on(
    tmp_path: Path, devices: str, expected: frozenset[str]
) -> None:
    config, findings = _devices_config(tmp_path, devices)

    assert findings == []
    assert config is not None
    assert config.projects[0].devices == expected


def test_project_without_devices_runs_on_every_device(tmp_path: Path) -> None:
    config, findings = _devices_config(tmp_path, None)
    pinned, _ = _devices_config(tmp_path, "[pc]")

    assert findings == []
    assert config is not None and pinned is not None
    assert config.projects[0].devices is None
    assert replace(pinned.projects[0], devices=None) == config.projects[0]
