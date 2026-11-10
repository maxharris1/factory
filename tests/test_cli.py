import copy
import subprocess
import sys
from pathlib import Path

import yaml

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


def test_unknown_command_is_rejected() -> None:
    result = run(["poll"])
    assert result.returncode == 2
    assert "unknown command: poll" in result.stderr


def test_check_config_warns_on_a_bot_login_mapped_to_an_after_approver(
    tmp_path: Path,
) -> None:
    data = copy.deepcopy(VALID)
    data["ship"] = "after:tracy-site"
    factory = write_factory(tmp_path, data)
    orch = tmp_path / "orch.yml"
    orch.write_text(
        "poll_seconds: 60\nprojects:\n  - repo: acme/widgets\n"
        "logins:\n  tracy-harris: tracy-site\n  'tracy-site[bot]': tracy-site\n",
        encoding="utf-8",
    )

    result = run(["check-config", "--orch", str(orch), "--factory", str(factory)])

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
    assert "warning: bot login 'tracy-site[bot]'" in result.stderr
    assert "tracy-harris" not in result.stderr
