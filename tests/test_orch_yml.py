from pathlib import Path

import pytest

from lib.orch_yml import (
    DEFAULT_JOB_TIMEOUT_MINUTES,
    POLL_SECONDS,
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
    ]
    assert config.projects[0].job_timeout == 150
    assert resolve_job_timeout(config, "aut0-code/aut0") == 150
    for project in config.projects[1:]:
        assert project.job_timeout is None
        assert resolve_job_timeout(config, project.repo) == DEFAULT_JOB_TIMEOUT_MINUTES == 60
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
        "  reconcile_seconds: 600\n",
    )

    assert findings == []
    assert config is not None and config.linear_webhook is not None
    webhook = config.linear_webhook
    assert (webhook.host, webhook.port) == ("::1", 9000)
    assert webhook.path == "/hooks/linear"
    assert webhook.secret_file == tmp_path / ".config/factory/linear-webhook.secret"
    assert webhook.reconcile_seconds == 600


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
