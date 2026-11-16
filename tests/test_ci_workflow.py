from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _workflow() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))


def _changes_step() -> dict:
    steps = [step for step in _workflow()["jobs"]["changes"]["steps"] if "run" in step]
    assert len(steps) == 1
    return steps[0]


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _run_changes(tmp_path: Path, touched: list[str], *, base_sha: str | None = None) -> tuple[int, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    for name in touched:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("change\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "pr")
    head = _git(repo, "rev-parse", "HEAD")
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    step = _changes_step()
    env = {
        **os.environ,
        **{key: str(value) for key, value in step.get("env", {}).items()},
        "BASE_SHA": base_sha or base,
        "HEAD_SHA": head,
        "GITHUB_OUTPUT": str(output),
    }
    result = subprocess.run(
        ["bash", "-c", step["run"]], cwd=repo, env=env, capture_output=True, text=True
    )
    return result.returncode, result.stderr, output.read_text(encoding="utf-8")


MACOS_PATHS = [
    ".github/workflows/ci.yml",
    "lib/job_run.py",
    "lib/remote.py",
    "tests/test_process_discovery.py",
    "requirements.txt",
    "requirements-dev.txt",
]


@pytest.mark.parametrize("path", MACOS_PATHS)
def test_a_pr_touching_a_macos_path_runs_the_macos_job(tmp_path: Path, path: str) -> None:
    code, stderr, output = _run_changes(tmp_path, ["lib/cli.py", path])
    assert code == 0, stderr
    assert output.splitlines() == ["macos=true"]


@pytest.mark.parametrize(
    "touched",
    [["lib/cli.py"], ["docs/notes.md", "tests/test_cli.py"], ["lib/job_run_helpers.py"], []],
)
def test_a_pr_touching_no_macos_path_skips_the_macos_job(tmp_path: Path, touched: list[str]) -> None:
    code, stderr, output = _run_changes(tmp_path, touched)
    assert code == 0, stderr
    assert output.splitlines() == ["macos=false"]


def test_a_failed_diff_fails_the_changes_job_instead_of_skipping_macos(tmp_path: Path) -> None:
    code, _, output = _run_changes(tmp_path, ["lib/job_run.py"], base_sha="0" * 40)
    assert code != 0
    assert output == ""


def test_the_changes_step_reads_the_pr_shas_from_env_and_fetches_full_history() -> None:
    changes = _workflow()["jobs"]["changes"]
    assert changes["runs-on"] == "ubuntu-latest"
    assert changes["outputs"] == {"macos": "${{ steps.diff.outputs.macos }}"}
    step = _changes_step()
    assert step["id"] == "diff"
    assert step["env"] == {
        "BASE_SHA": "${{ github.event.pull_request.base.sha }}",
        "HEAD_SHA": "${{ github.event.pull_request.head.sha }}",
    }
    assert "${{" not in step["run"]
    checkout = changes["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"] == {"fetch-depth": 0}
    assert "if" not in changes
    for each in changes["steps"]:
        assert each["if"] == "github.event_name == 'pull_request'"


def test_macos_runs_on_every_non_pr_event_and_on_prs_the_changes_job_flags() -> None:
    workflow = _workflow()
    assert workflow[True]["push"] == {"branches": ["main"]}
    macos = workflow["jobs"]["macos"]
    assert macos["needs"] == "changes"
    assert macos["if"] == "github.event_name != 'pull_request' || needs.changes.outputs.macos == 'true'"
    assert macos["runs-on"] == "macos-latest"
    assert "pytest tests/test_process_discovery.py -p no:xdist -s -rs" in [
        step.get("run") for step in macos["steps"]
    ]
