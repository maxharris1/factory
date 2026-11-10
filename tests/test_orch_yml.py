from pathlib import Path

from lib.orch_yml import (
    DEFAULT_JOB_TIMEOUT_MINUTES,
    OrchConfig,
    POLL_SECONDS,
    approver_login_findings,
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
    assert [project.repo for project in config.projects] == [
        "aut0-code/aut0",
        "maxharris1/Cleffy",
        "maxharris1/tracy-harris-coaching",
    ]
    for project in config.projects:
        assert project.job_timeout is None
        assert resolve_job_timeout(config, project.repo) == DEFAULT_JOB_TIMEOUT_MINUTES == 90


def test_minimal_example_has_no_extra_required_fields() -> None:
    config, findings = load_orch_yml(ROOT / "config/orch.example.yml")
    assert findings == []
    assert config is not None
    assert config.poll_seconds == 60
    assert config.job_timeout is None
    assert config.projects[0].repo == "owner/name"
    assert config.projects[0].job_timeout is None
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


def test_logins_are_optional_and_must_name_a_lowercase_identity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "orch.yml"
    path.write_text(
        "poll_seconds: 60\nprojects:\n  - repo: acme/widgets\n"
        "logins:\n  tracy-harris: tracy-site\n",
        encoding="utf-8",
    )
    config, findings = load_orch_yml(path)
    assert findings == []
    assert config is not None
    assert config.logins == (("tracy-harris", "tracy-site"),)

    path.write_text(
        "poll_seconds: 60\nprojects:\n  - repo: acme/widgets\n"
        "logins:\n  tracy-harris: Tracy Site\n",
        encoding="utf-8",
    )
    config, findings = load_orch_yml(path)
    assert config is None
    assert [item.severity for item in findings] == ["error"]


def test_bot_login_mapped_to_an_after_approver_warns() -> None:
    config = OrchConfig(
        poll_seconds=60,
        projects=(),
        logins=(
            ("tracy-harris", "tracy-site"),
            ("tracy-site[bot]", "tracy-site"),
            ("max-claude-bot", "tracy-site"),
            ("someone", "claude-bot"),
            ("claude-bot-gh", "claude-bot"),
        ),
    )

    findings = approver_login_findings(config, {"tracy-site", "claude-bot"})

    assert [item.severity for item in findings] == ["warning"] * 4
    assert "tracy-harris" not in " ".join(item.message for item in findings)
