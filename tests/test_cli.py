import copy
import sqlite3
import subprocess
import sys
from pathlib import Path

import yaml
import pytest

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


def test_check_config_accepts_the_repo_from_another_directory(tmp_path: Path) -> None:
    result = run(["check-config"], cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
    assert result.stderr == ""


def test_check_config_accepts_the_minimal_orch_example() -> None:
    result = run(["check-config", "--orch", str(ROOT / "config" / "orch.example.yml")])
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


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
    result = run(["check-config", "--factory", str(path)])
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

    result = run(["init-db", "--db", str(database)])

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
    sqlite3.connect(database).close()
    monkeypatch.setenv("ORCH_DB", str(database))

    assert cli.main(["doctor-runtime"]) == 0
    assert capsys.readouterr().out == "ok\n"
