import copy
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from lib import cli

ROOT = Path(__file__).resolve().parents[1]

VALID = {
    "base_branch": "main",
    "tickets": "github",
    "builder": {"harness": "claude"},
    "reviewer": {"harness": "codex"},
    "review": {"rounds": {"low": 1, "high": 3}, "verify": "none"},
    "ship": "merge",
    "skills": [],
}


def run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "bin" / "orch"), *args],
        cwd=cwd or ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def write_factory(directory: Path, data: dict) -> Path:
    path = directory / "factory.md"
    raw = yaml.safe_dump(data, sort_keys=False)
    path.write_text(f"---\n{raw}---\n", encoding="utf-8")
    return path


def write_orch(directory: Path) -> Path:
    path = directory / "orch.yml"
    path.write_text(
        """
poll_seconds: 60
projects:
  - repo: owner/name
    factory_ref: main
grok:
  webhook_url_env: FACTORY_WEBHOOK_URL
  webhook_key_env: FACTORY_WEBHOOK_KEY
""".lstrip(),
        encoding="utf-8",
    )
    return path


def test_check_config_rejects_repo_placeholder_from_another_directory(
    tmp_path: Path,
) -> None:
    result = run(["check-config"], cwd=tmp_path)
    assert result.returncode == 1
    assert "linear_project_id must be a project id" in result.stderr


def test_check_config_allows_repo_placeholder_only_with_explicit_flag() -> None:
    result = run(["check-config", "--allow-placeholders"])

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
    assert (
        "warning: accepted placeholder "
        "projects[0].linear_project_id"
    ) in result.stderr


def test_allow_placeholders_still_runs_other_orch_validation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "orch.yml"
    path.write_text(
        "poll_seconds: 45\nprojects:\n"
        "  - repo: owner/name\n"
        "    linear_project_id: REPLACE_WITH_LINEAR_PROJECT_ID\n",
        encoding="utf-8",
    )

    result = run(
        [
            "check-config",
            "--orch",
            str(path),
            "--allow-placeholders",
        ]
    )

    assert result.returncode == 1
    assert "poll_seconds must be 30 or 60" in result.stderr


def test_check_config_accepts_the_minimal_orch_example() -> None:
    result = run(["check-config", "--orch", str(ROOT / "config" / "orch.example.yml")])
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


@pytest.mark.parametrize("command", ["init-db", "poll"])
def test_runtime_commands_reject_repo_placeholder(
    tmp_path: Path,
    command: str,
) -> None:
    database = tmp_path / "orch.db"
    if command == "poll":
        database.write_bytes(b"not-used")

    result = run([command, "--db", str(database)])

    assert result.returncode == 1
    assert "linear_project_id must be a project id" in result.stderr


def test_check_config_refuses_a_missing_field(tmp_path: Path) -> None:
    data = copy.deepcopy(VALID)
    del data["ship"]
    path = write_factory(tmp_path, data)
    result = run(["check-config", "--factory", str(path)])
    assert result.returncode == 1
    assert "missing required field 'ship'" in result.stderr
    assert "ok" not in result.stdout


def test_check_config_refuses_a_same_family_reviewer(tmp_path: Path) -> None:
    data = copy.deepcopy(VALID)
    data["reviewer"]["harness"] = "claude"
    path = write_factory(tmp_path, data)
    result = run(["check-config", "--factory", str(path)])
    assert result.returncode == 1
    assert "same-family reviewer" in result.stderr


def test_check_config_refuses_linear_with_github_issues(tmp_path: Path) -> None:
    data = copy.deepcopy(VALID)
    data["tickets"] = "linear"
    data["github_issues"] = True
    path = write_factory(tmp_path, data)
    result = run(["check-config", "--factory", str(path)])
    assert result.returncode == 1
    assert "github_issues is true" in result.stderr


def test_check_config_refuses_linear_with_github_labels(tmp_path: Path) -> None:
    data = copy.deepcopy(VALID)
    data["tickets"] = "linear"
    data["github_labels"] = True
    path = write_factory(tmp_path, data)
    result = run(["check-config", "--factory", str(path)])
    assert result.returncode == 1
    assert "github_labels is true" in result.stderr


def test_check_config_warns_on_an_unknown_model_and_exits_zero(tmp_path: Path) -> None:
    data = copy.deepcopy(VALID)
    data["reviewer"]["model"] = "not-a-real-model"
    path = write_factory(tmp_path, data)
    orch = write_orch(tmp_path)
    result = run(
        ["check-config", "--orch", str(orch), "--factory", str(path)]
    )
    assert result.returncode == 0, result.stderr
    assert "warning: unknown model name 'not-a-real-model'" in result.stderr
    assert result.stdout.strip() == "ok"


def test_poll_requires_a_database_path() -> None:
    result = run(["poll"], cwd=ROOT)
    assert result.returncode == 1
    assert "pass --db or set ORCH_DB" in result.stderr
    assert "Traceback" not in result.stderr


def test_poll_refuses_a_missing_database(tmp_path: Path) -> None:
    database = tmp_path / "missing.db"

    result = run(["poll", "--db", str(database), "--once"])

    assert result.returncode == 1
    assert "does not exist; run orch init-db first" in result.stderr
    assert not database.exists()


def test_poll_lock_refuses_a_second_poller(tmp_path: Path) -> None:
    database = tmp_path / "orch.db"
    database.write_bytes(b"database")

    with cli._poll_lock(database):
        with pytest.raises(ValueError, match="another orch poll"):
            with cli._poll_lock(database):
                pass


def test_init_db_creates_dispatcher_schema(tmp_path: Path) -> None:
    database = tmp_path / "state" / "orch.db"
    orch = write_orch(tmp_path)

    result = run(["init-db", "--db", str(database), "--orch", str(orch)])

    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok\n"
    connection = sqlite3.connect(database)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        connection.close()
    assert {"jobs", "tracked_tickets", "handled_comments"} <= tables


def test_doctor_runtime_requires_orch_db(monkeypatch, capsys) -> None:
    monkeypatch.delenv("ORCH_DB", raising=False)

    assert cli.main(["doctor-runtime"]) == 1
    assert "ORCH_DB is not set" in capsys.readouterr().err


def test_doctor_runtime_accepts_existing_sqlite_db(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    database = tmp_path / "orch.db"
    orch_config = tmp_path / "orch.yml"
    orch_config.write_text(
        (ROOT / "orch.yml").read_text(encoding="utf-8").replace(
            "REPLACE_WITH_LINEAR_PROJECT_ID",
            "test-linear-project-id",
        )
        + "\ntrusted_linear_users:\n  test-user-id: [grok-bot]\n",
        encoding="utf-8",
    )
    assert cli.main(
        [
            "init-db",
            "--db",
            str(database),
            "--orch",
            str(orch_config),
        ]
    ) == 0
    capsys.readouterr()
    wrapper = tmp_path / ".local/bin/orch"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
    wrapper.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(wrapper.parent))
    monkeypatch.setenv("ORCH_DB", str(database))

    assert cli.main(["doctor-runtime"]) == 0
    assert capsys.readouterr().out == "ok\n"


def test_doctor_runtime_rejects_database_without_runtime_tables(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    database = tmp_path / "orch.db"
    sqlite3.connect(database).close()
    wrapper = tmp_path / ".local/bin/orch"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
    wrapper.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(wrapper.parent))
    monkeypatch.setenv("ORCH_DB", str(database))

    assert cli.main(["doctor-runtime"]) == 1
    assert "database is missing runtime tables" in capsys.readouterr().err


def test_check_config_accepts_the_minimal_machines_example() -> None:
    result = run(
        [
            "check-config",
            "--orch",
            str(ROOT / "config" / "orch.example.yml"),
            "--machines",
            str(ROOT / "config" / "machines.example.yml"),
        ]
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
    assert result.stderr == ""


def test_check_config_refuses_shipped_machines_placeholders_without_flag() -> None:
    result = run(
        [
            "check-config",
            "--orch",
            str(ROOT / "config" / "orch.example.yml"),
            "--machines",
            "machines.yml",
        ]
    )

    assert result.returncode == 1
    assert (
        "machines.yml: devices[2].workdir_root is an install placeholder"
    ) in result.stderr
    assert "devices[1].workdir_root" not in result.stderr
    assert "machines.yml: warning: device 'pc' is disabled" in result.stderr


def test_check_config_reports_machines_placeholders_and_disabled_devices() -> None:
    result = run(
        [
            "check-config",
            "--orch",
            "orch.yml",
            "--machines",
            "machines.yml",
            "--allow-placeholders",
        ]
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
    assert "accepted placeholder devices[1]" not in result.stderr
    for line in (
        "machines.yml: warning: accepted placeholder devices[2].workdir_root; "
        "device 'mac' will not be scheduled",
        "machines.yml: warning: device 'pc' is disabled (enabled: false); "
        "it will not be scheduled",
        "machines.yml: warning: device 'mac' is disabled (enabled: false); "
        "it will not be scheduled",
    ):
        assert line in result.stderr


def test_bare_check_config_covers_the_shipped_machines_yml() -> None:
    result = run(["check-config", "--allow-placeholders"])

    assert result.returncode == 0, result.stderr
    assert "machines.yml: warning: device 'mac' is disabled" in result.stderr


def test_check_config_refuses_a_machines_schema_error(tmp_path: Path) -> None:
    path = tmp_path / "machines.yml"
    path.write_text(
        "devices:\n"
        "  - name: pc\n"
        "    tailscale_name: pc.example.ts.net\n"
        "    slot_max: 2\n"
        "    harnesses: {claude: codex-bot}\n"
        "    workdir_root: /mnt/c/work\n",
        encoding="utf-8",
    )

    result = run(
        [
            "check-config",
            "--orch",
            str(ROOT / "config" / "orch.example.yml"),
            "--machines",
            str(path),
            "--allow-placeholders",
        ]
    )

    assert result.returncode == 1
    assert "devices[0].workdir_root must not be under /mnt/" in result.stderr
    assert "devices[0].harnesses.claude must be 'claude-bot'" in result.stderr
