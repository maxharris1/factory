from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
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


@pytest.mark.parametrize("source", ["lib/remote.py", "tests/test_process_discovery.py"])
def test_a_pr_renaming_a_macos_path_away_runs_the_macos_job(tmp_path: Path, source: str) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "lib").mkdir()
    (repo / "tests").mkdir()
    (repo / source).write_text("def body():\n    return 1\n" * 20, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "mv", source, "lib/renamed.py")
    _git(repo, "commit", "-q", "-m", "rename")
    head = _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "diff", "--name-status", "-M", f"{base}...{head}").startswith("R100")
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    env = {**os.environ, "BASE_SHA": base, "HEAD_SHA": head, "GITHUB_OUTPUT": str(output)}
    result = subprocess.run(
        ["bash", "-c", _changes_step()["run"]], cwd=repo, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8").splitlines() == ["macos=true"]


FAKE_GH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GH_LOG"
[[ "$1" == api ]] || exit 0
calls=$(grep -c '^api ' "$GH_LOG")
total=$(wc -l < "$GH_RESPONSES")
response=$(sed -n "$(( calls < total ? calls : total ))p" "$GH_RESPONSES")
[[ "$response" == FAIL ]] && { echo "HTTP 502" >&2; exit 1; }
while [[ $# -gt 0 && "$1" != --jq ]]; do shift; done
jq -r "$2" <<<"$response"
"""


FAKE_DATE = """#!/usr/bin/env bash
[[ "$1" == +%s ]] && exec cat "$CLOCK"
exec "$REAL_DATE" "$@"
"""

FAKE_SLEEP = """#!/usr/bin/env bash
echo $(( $(cat "$CLOCK") + $1 )) > "$CLOCK"
"""


def _pickup_step() -> dict:
    steps = _workflow()["jobs"]["macos-pickup"]["steps"]
    assert len(steps) == 1
    return steps[0]


def _jobs(macos: tuple[str, datetime] | None, conclusion: str | None = None) -> str:
    jobs = [
        {"name": "macos-pickup", "status": "in_progress", "conclusion": None, "created_at": "2020-01-01T00:00:00Z"}
    ]
    if macos is not None:
        status, created = macos
        jobs.append(
            {
                "name": "macos",
                "status": status,
                "conclusion": conclusion,
                "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )
    return json.dumps({"total_count": len(jobs), "jobs": jobs})


def _run_pickup(
    tmp_path: Path, responses: list[str], *, clock: datetime | None = None
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fakes = {"gh": FAKE_GH}
    clock_env = {}
    if clock is not None:
        fakes |= {"date": FAKE_DATE, "sleep": FAKE_SLEEP}
        (tmp_path / "clock").write_text(f"{int(clock.timestamp())}\n", encoding="utf-8")
        clock_env = {"CLOCK": str(tmp_path / "clock"), "REAL_DATE": shutil.which("date")}
    for name, script in fakes.items():
        fake = bin_dir / name
        fake.write_text(script, encoding="utf-8")
        fake.chmod(0o755)
    (tmp_path / "responses").write_text("\n".join(responses) + "\n", encoding="utf-8")
    log = tmp_path / "gh.log"
    log.write_text("", encoding="utf-8")
    step = _pickup_step()
    env = {
        **os.environ,
        **{key: str(value) for key, value in step.get("env", {}).items()},
        **({} if clock is not None else {"POLL_SECONDS": "0"}),
        **clock_env,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GH_LOG": str(log),
        "GH_RESPONSES": str(tmp_path / "responses"),
        "GITHUB_REPOSITORY": "acme/factory",
        "GITHUB_RUN_ID": "4242",
        "GITHUB_RUN_ATTEMPT": "2",
    }
    result = subprocess.run(
        ["bash", "-c", step["run"]], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30
    )
    return result, log.read_text(encoding="utf-8").splitlines()


def test_the_pickup_watchdog_passes_once_a_picked_up_macos_succeeds(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    result, calls = _run_pickup(
        tmp_path,
        [
            _jobs(None),
            _jobs(("queued", now)),
            _jobs(("in_progress", now)),
            _jobs(("completed", now), "success"),
        ],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "::error" not in result.stdout
    assert len(calls) == 4
    assert all(
        call.startswith("api repos/acme/factory/actions/runs/4242/attempts/2/jobs?per_page=100 --jq ")
        for call in calls
    )


def test_the_pickup_watchdog_cancels_the_run_when_macos_stays_queued(tmp_path: Path) -> None:
    created = datetime.now(timezone.utc) - timedelta(seconds=601)
    result, calls = _run_pickup(tmp_path, [_jobs(("queued", created))])
    assert result.returncode == 1
    error = [line for line in result.stdout.splitlines() if line.startswith("::error title=macos runner pickup::")]
    assert len(error) == 1, result.stdout
    assert "within 10 minutes" in error[0]
    assert created.strftime("%Y-%m-%dT%H:%M:%SZ") in error[0]
    assert calls[-1] == "run cancel 4242 --repo acme/factory"


def test_the_pickup_watchdog_tolerates_a_failed_api_call(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    result, calls = _run_pickup(tmp_path, ["FAIL", _jobs(("completed", now), "success")])
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(calls) == 2
    assert not any(call.startswith("run cancel") for call in calls)


def test_a_failed_macos_fails_the_watchdog_so_a_failed_jobs_rerun_reruns_both(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    result, calls = _run_pickup(
        tmp_path, [_jobs(("in_progress", now)), _jobs(("completed", now), "failure")]
    )
    assert result.returncode == 1, result.stdout + result.stderr
    error = [line for line in result.stdout.splitlines() if line.startswith("::error title=macos failed::")]
    assert len(error) == 1, result.stdout
    assert "failure" in error[0]
    assert len(calls) == 2
    assert not any(call.startswith("run cancel") for call in calls)


def test_a_failed_poll_past_the_pickup_deadline_never_cancels_the_run(tmp_path: Path) -> None:
    created = datetime(2026, 11, 16, 15, 49, 22, tzinfo=timezone.utc)
    result, calls = _run_pickup(
        tmp_path,
        [
            _jobs(("queued", created)),
            "FAIL",
            _jobs(("in_progress", created)),
            _jobs(("completed", created), "success"),
        ],
        clock=created + timedelta(seconds=585),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "::error" not in result.stdout
    assert len(calls) == 4
    assert not any(call.startswith("run cancel") for call in calls)


def test_the_pickup_watchdog_mirrors_the_macos_job_and_fires_before_its_timeout() -> None:
    jobs = _workflow()["jobs"]
    pickup = jobs["macos-pickup"]
    assert pickup["needs"] == jobs["macos"]["needs"]
    assert pickup["if"] == jobs["macos"]["if"]
    assert pickup["runs-on"] == "ubuntu-latest"
    assert pickup["permissions"] == {"actions": "write"}
    step = _pickup_step()
    pickup_seconds = int(step["env"]["PICKUP_SECONDS"])
    assert pickup_seconds == 600
    assert pickup["timeout-minutes"] > pickup_seconds / 60 + jobs["macos"]["timeout-minutes"]
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "${{" not in step["run"]
