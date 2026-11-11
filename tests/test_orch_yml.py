from pathlib import Path

from lib.orch_yml import (
    DEFAULT_JOB_TIMEOUT_MINUTES,
    POLL_SECONDS,
    load_orch_yml,
    resolve_job_timeout,
)

ROOT = Path(__file__).resolve().parents[1]


def test_repo_orch_yml_lists_projects_and_polls_every_60_seconds() -> None:
    config, findings = load_orch_yml(ROOT / "orch.yml")
    assert findings == []
    assert config is not None
    assert config.poll_seconds == POLL_SECONDS == 60
    assert config.job_timeout is None
    assert config.linear is not None
    assert config.linear.api_key_env == "LINEAR_API_KEY"
    assert config.grok is not None
    assert config.grok.webhook_url_env == "FACTORY_WEBHOOK_URL"
    assert config.grok.webhook_key_env == "FACTORY_WEBHOOK_KEY"
    assert config.trusted_logins["github"]["maxharris1"] == frozenset(
        {"grok-bot", "claude-bot", "codex-bot", "cursor-bot"}
    )
    assert [project.repo for project in config.projects] == [
        "aut0-code/aut0",
        "maxharris1/Cleffy",
        "maxharris1/tracy-harris-coaching",
    ]
    assert config.projects[0].job_timeout == 150
    assert resolve_job_timeout(config, "aut0-code/aut0") == 150
    for project in config.projects[1:]:
        assert project.job_timeout is None
        assert resolve_job_timeout(config, project.repo) == DEFAULT_JOB_TIMEOUT_MINUTES == 90
    assert config.projects[0].linear_project == "Aut0"
    assert config.projects[0].linear_ready_for_review is True
    assert config.projects[0].grok_bot == "offer-brief-project"


def test_minimal_example_has_no_extra_required_fields() -> None:
    config, findings = load_orch_yml(ROOT / "config/orch.example.yml")
    assert findings == []
    assert config is not None
    assert config.poll_seconds == 60
    assert config.job_timeout is None
    assert config.projects[0].repo == "owner/name"
    assert config.projects[0].job_timeout is None
    assert config.projects[0].linear_ready_for_review is False
    assert resolve_job_timeout(config, "owner/name") == 90


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


def test_project_timeout_overrides_the_90_minute_default(tmp_path: Path) -> None:
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
    assert resolve_job_timeout(config, "acme/other") == 90


def test_poll_seconds_must_be_60(tmp_path: Path) -> None:
    path = tmp_path / "orch.yml"
    path.write_text("poll_seconds: 30\nprojects:\n  - repo: acme/widgets\n", encoding="utf-8")
    config, findings = load_orch_yml(path)
    assert config is None
    assert any("poll_seconds must be 60" in finding.message for finding in findings)


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
        "    linear_project: Widgets\n",
        encoding="utf-8",
    )

    config, findings = load_orch_yml(path)

    assert config is None
    assert any(
        "linear.api_key_env is required" in finding.message
        for finding in findings
    )


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
