from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from lib.ci_route import SLOTS, grant_step
from lib.factory_md import load_factory_md

ROOT = Path(__file__).resolve().parents[1]
FORK_GUARD = "github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository"
MACOS_LABELS = ["self-hosted", "macOS", "factory-mac"]
POOL = ["self-hosted", "Linux", "X64", "factory-linux"]
SHARD_SLOT = "format('test-{0}', matrix.shard)"
ROUTED = re.compile(
    r"^\$\{\{ contains\(fromJSON\(needs\.changes\.outputs\.hosted \|\| '\[\]'\), "
    r"(?:'(?P<slot>[\w-]+)'|(?P<shard>format\('test-\{0\}', matrix\.shard\)))\) "
    r"&& 'ubuntu-latest' \|\| fromJSON\('(?P<fallback>\[[^']*\])'\) \}\}$"
)
WORKERS = re.compile(
    r"^pytest -n \$\{\{ runner\.environment == 'self-hosted' && (?P<local>\d+) \|\| (?P<hosted>\d+) \}\} "
)
ROUTE_ENV = {"GITHUB_TOKEN": "${{ github.token }}", "FACTORY_LINUX_POOL": "${{ vars.FACTORY_LINUX_POOL }}"}


def _workflow() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))


def _changes_step() -> dict:
    steps = [step for step in _workflow()["jobs"]["changes"]["steps"] if step.get("id") == "diff"]
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
    "lib/device_monitor.py",
    "lib/job_run.py",
    "lib/process_tree.py",
    "lib/remote.py",
    "tests/test_process_discovery.py",
    "tests/test_remote_start.py",
    "tests/test_remote_tree.py",
    "tests/test_job_run.py",
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
    assert changes["outputs"] == {
        "macos": "${{ steps.diff.outputs.macos }}",
        "hosted": "${{ steps.route.outputs.hosted }}",
    }
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
    assert "if" not in checkout
    assert changes["if"] == FORK_GUARD
    route_at = next(index for index, each in enumerate(changes["steps"]) if each.get("id") == "route")
    assert [each for each in changes["steps"][:route_at] if "if" in each] == [step]
    assert step["if"] == "github.event_name == 'pull_request'"
    route = changes["steps"][route_at]
    assert route == {"id": "route", "env": ROUTE_ENV, "run": "python3 -I lib/ci_route.py"}
    assert not any(each.get("uses", "").startswith("astral-sh/setup-uv@") for each in changes["steps"])


def test_the_changes_job_ends_by_marking_each_slot_the_route_kept_local() -> None:
    steps = _workflow()["jobs"]["changes"]["steps"]
    route_at = next(index for index, each in enumerate(steps) if each.get("id") == "route")
    assert steps[route_at + 1:] == [
        {"name": grant_step(slot),
         "if": f"${{{{ !contains(fromJSON(steps.route.outputs.hosted || '[]'), '{slot}') }}}}",
         "run": "true"}
        for slot in SLOTS
    ]


def test_macos_runs_on_every_non_pr_event_and_on_prs_the_changes_job_flags() -> None:
    workflow = _workflow()
    assert workflow[True]["push"] == {"branches": ["main"]}
    macos = workflow["jobs"]["macos"]
    runs = [step.get("run") for step in macos["steps"]]
    assert macos["needs"] == "changes"
    assert macos["if"] == f"({FORK_GUARD}) && (github.event_name != 'pull_request' || needs.changes.outputs.macos == 'true')"
    assert (
        "pytest tests/test_process_discovery.py tests/test_remote_tree.py "
        "tests/test_remote_start.py::test_a_lost_box_jobs_untagged_orphan_born_after_the_last_look_is_killed "
        "-p no:xdist -s -rs"
    ) in runs
    assert "pytest tests/test_job_run.py -k macos -p no:xdist -s -rs -v" in runs


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
    assert pickup["permissions"] == {"actions": "write"}
    step = _pickup_step()
    pickup_seconds = int(step["env"]["PICKUP_SECONDS"])
    assert pickup_seconds == 600
    assert pickup["timeout-minutes"] > pickup_seconds / 60 + jobs["macos"]["timeout-minutes"]
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "${{" not in step["run"]


SECRET_REF = re.compile(r"\bsecrets\s*(?:\.(?!GITHUB_TOKEN\b)|\[)")
TOKEN_SHAPE = re.compile(r"\bA[A-Z0-9]{28}\b|--token\s+\S")
WORKFLOWS = sorted((ROOT / ".github/workflows").glob("*.yml"))


def _routed_slots(job: dict) -> list[str]:
    routed = ROUTED.match(job.get("runs-on", ""))
    if routed is None:
        raise ValueError(f"not a routed runs-on: {job.get('runs-on')!r}")
    if json.loads(routed["fallback"]) != POOL:
        raise ValueError(f"routed runs-on falls back off the pool: {routed['fallback']}")
    if routed["shard"]:
        slots = [f"test-{shard}" for shard in job.get("strategy", {}).get("matrix", {}).get("shard", [])]
    else:
        slots = [routed["slot"]]
    if not slots or not set(slots) <= set(SLOTS):
        raise ValueError(f"routed runs-on names a slot outside SLOTS: {slots}")
    return slots


def _labels(job: dict) -> list[str]:
    runs_on = job.get("runs-on", [])
    if not isinstance(runs_on, str):
        return runs_on
    if "${{" not in runs_on:
        return [runs_on]
    _routed_slots(job)
    return POOL


def _routed_runs_on(slot: str, fallback: list[str] = POOL) -> str:
    named = slot if slot == SHARD_SLOT else f"'{slot}'"
    return (
        f"${{{{ contains(fromJSON(needs.changes.outputs.hosted || '[]'), {named}) && 'ubuntu-latest' "
        f"|| fromJSON('{json.dumps(fallback, separators=(',', ':'))}') }}}}"
    )


def _needs(job: dict) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else needs


def _grants_id_token(permissions: object) -> bool:
    return permissions == "write-all" or (
        isinstance(permissions, dict) and permissions.get("id-token") == "write"
    )


def _self_hosted_credential_jobs(workflow: dict) -> list[str]:
    return [
        name
        for name, job in workflow.get("jobs", {}).items()
        if "self-hosted" in _labels(job)
        and (
            SECRET_REF.search(yaml.safe_dump(job)) is not None
            or "environment" in job
            or _grants_id_token(job.get("permissions"))
            or _grants_id_token(workflow.get("permissions"))
        )
    ]


def _ci_runner_pool() -> list[str]:
    config, findings = load_factory_md(ROOT / "factory.md")
    assert config is not None, findings
    assert config.ci_runner == "factory-linux"
    return ["self-hosted", "Linux", "X64", config.ci_runner]


def test_every_linux_job_defaults_to_the_ci_runner_pool_and_macos_stays_on_the_mac() -> None:
    pool = _ci_runner_pool()
    assert pool == POOL
    for name, job in _workflow()["jobs"].items():
        if name == "macos":
            assert job["runs-on"] == MACOS_LABELS
        elif name == "changes":
            assert job["runs-on"] == "ubuntu-latest"
        else:
            assert _labels(job) == pool, name


def test_routed_slots_follow_the_router_rank_order_and_wait_for_changes() -> None:
    jobs = _workflow()["jobs"]
    routed = {name: job for name, job in jobs.items() if name not in {"changes", "macos"}}
    assert sorted((slot for job in routed.values() for slot in _routed_slots(job)), key=SLOTS.index) == list(SLOTS)
    for name, job in routed.items():
        assert "changes" in _needs(job), name
        assert job["runs-on"] == _routed_runs_on(SHARD_SLOT if name == "test" else name), name


@pytest.mark.parametrize("name", ["test", "statechart", "checks", "py311-compile"])
def test_every_routed_build_job_logs_which_runner_took_it(name: str) -> None:
    first = _workflow()["jobs"][name]["steps"][0]
    assert first == {"name": "Runner identity", "run": 'echo "runner $RUNNER_NAME as $(id -un)"'}


@pytest.mark.parametrize(
    "job",
    [
        {"runs-on": "${{ fromJSON(vars.FACTORY_LINUX_RUNNER) }}"},
        {"runs-on": "${{ vars.RUNNER }}"},
        {"runs-on": _routed_runs_on("checks", ["ubuntu-latest"])},
        {"runs-on": _routed_runs_on("checks", ["self-hosted", "Linux"])},
        {"runs-on": _routed_runs_on("deploy")},
        {"runs-on": _routed_runs_on(SHARD_SLOT), "strategy": {"matrix": {"shard": [1, 2, 3, 4]}}},
        {"runs-on": _routed_runs_on(SHARD_SLOT)},
    ],
)
def test_labels_refuses_a_runs_on_expression_it_cannot_prove_lands_on_the_pool(job: dict) -> None:
    with pytest.raises(ValueError):
        _labels(job)


@pytest.mark.parametrize(
    "doc", ["docs/F2-03-RUNNER-CHECKLIST.md", "docs/decisions/711-self-hosted-runners.md"]
)
def test_the_runner_docs_name_a_dedicated_user_for_the_mac_and_no_bot_runner(doc: str) -> None:
    text = " ".join((ROOT / doc).read_text(encoding="utf-8").split())
    assert "`maxs-macbook-pro` runs as `gharunner`" in text
    assert re.search(r"runs as `(claude|codex|cursor|grok)-bot`", text) is None


@pytest.mark.parametrize(
    "doc", ["docs/F2-03-RUNNER-CHECKLIST.md", "docs/decisions/711-self-hosted-runners.md"]
)
def test_the_runner_docs_put_the_sync_on_the_pc_and_no_box_runner_in_the_pool(doc: str) -> None:
    text = " ".join((ROOT / doc).read_text(encoding="utf-8").split())
    assert "runs on the orch host (the PC) as the `factory` user" in text
    assert "crontab" in text
    assert "`pc-wsl-bots-1` to `-3`" in text
    assert "`maxs-macbook-pro`" in text
    for gone in ("box-1", "factory-box", "box-runner-ensure"):
        assert gone not in text, gone


def test_only_the_changes_job_runs_hosted_and_it_holds_only_a_read_token() -> None:
    jobs = _workflow()["jobs"]
    assert {name for name, job in jobs.items() if "self-hosted" not in _labels(job)} == {"changes"}
    changes = jobs["changes"]
    assert changes["permissions"] == {"actions": "read", "contents": "read"}
    text = yaml.safe_dump(changes, width=10**6)
    expressions = re.findall(r"\$\{\{\s*(.*?)\s*\}\}", text)
    assert [expr for expr in expressions if re.search(r"token|secret", expr, re.I)] == ["github.token"]
    assert SECRET_REF.search(text) is None


def test_no_self_hosted_job_reads_the_pool_or_runs_the_router() -> None:
    for name, job in _workflow()["jobs"].items():
        if "self-hosted" in _labels(job):
            text = yaml.safe_dump(job, width=10**6)
            for needle in ("FACTORY_LINUX_POOL", "actions/runners", "ci_route"):
                assert needle not in text, (name, needle)


def test_every_python_job_provisions_python_with_setup_uv_not_setup_python() -> None:
    for name, job in _workflow()["jobs"].items():
        if "self-hosted" not in _labels(job):
            continue
        uses = [step.get("uses", "") for step in job["steps"]]
        runs = " ".join(step.get("run", "") for step in job["steps"])
        assert not any(use.startswith("actions/setup-python@") for use in uses), name
        if re.search(r"\b(python|pytest|pip)\b", runs):
            setup = [step for step in job["steps"] if step.get("uses", "").startswith("astral-sh/setup-uv@")]
            assert len(setup) == 1, name
            assert setup[0]["with"]["activate-environment"] is True, name
            assert not re.search(r"(?<!uv )\bpip install\b", runs), name


def test_every_ci_job_skips_pull_requests_from_forks() -> None:
    for name, job in _workflow()["jobs"].items():
        assert FORK_GUARD in job.get("if", ""), name


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
def test_a_self_hosted_workflow_never_runs_on_privileged_triggers(path: Path) -> None:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not any("self-hosted" in _labels(job) for job in workflow["jobs"].values()):
        return
    triggers = workflow[True]
    names = {triggers} if isinstance(triggers, str) else set(triggers)
    assert names.isdisjoint({"pull_request_target", "workflow_run"})


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
def test_no_self_hosted_job_holds_a_credential(path: Path) -> None:
    assert _self_hosted_credential_jobs(yaml.safe_load(path.read_text(encoding="utf-8"))) == []


@pytest.mark.parametrize("runs_on", [["self-hosted", "Linux"], _routed_runs_on("checks")])
@pytest.mark.parametrize(
    "workflow",
    [
        {"jobs": {"deploy": {"env": {"KEY": "${{ secrets.DEPLOY_KEY }}"}}}},
        {"jobs": {"deploy": {"steps": [{"run": "echo ${{ secrets['DEPLOY_KEY'] }}"}]}}},
        {"jobs": {"deploy": {"environment": "production"}}},
        {"jobs": {"deploy": {"permissions": {"id-token": "write"}}}},
        {"jobs": {"deploy": {"permissions": "write-all"}}},
        {"permissions": {"id-token": "write"}, "jobs": {"deploy": {}}},
    ],
)
def test_a_self_hosted_job_holding_a_credential_is_flagged(workflow: dict, runs_on: object) -> None:
    workflow = copy.deepcopy(workflow)
    workflow["jobs"]["deploy"]["runs-on"] = runs_on
    assert _self_hosted_credential_jobs(workflow) == ["deploy"]


@pytest.mark.parametrize(
    "job",
    [
        {"runs-on": "ubuntu-latest", "env": {"KEY": "${{ secrets.DEPLOY_KEY }}"}},
        {"runs-on": ["self-hosted", "Linux"], "env": {"GH_TOKEN": "${{ secrets.GITHUB_TOKEN }}"}},
    ],
)
def test_a_hosted_job_or_a_github_token_is_not_a_held_credential(job: dict) -> None:
    assert _self_hosted_credential_jobs({"jobs": {"build": job}}) == []


def test_no_runner_registration_token_is_committed() -> None:
    paths = [ROOT / "factory.md"] + [
        path for root in (".github", "docs", "deploy") for path in (ROOT / root).rglob("*") if path.is_file()
    ]
    hits = [
        f"{path.relative_to(ROOT)}:{text.count(chr(10), 0, match.start()) + 1}"
        for path in paths
        for text in [path.read_text(encoding="utf-8", errors="ignore")]
        for match in TOKEN_SHAPE.finditer(text)
    ]
    assert hits == []


def test_the_test_shards_start_a_lingering_user_manager_before_pytest() -> None:
    steps = _workflow()["jobs"]["test"]["steps"]
    runs = [step.get("run", "") for step in steps]
    linger = next(i for i, run in enumerate(runs) if "enable-linger" in run)
    pytest_step = next(i for i, run in enumerate(runs) if run.startswith("pytest"))
    assert linger < pytest_step
    assert "XDG_RUNTIME_DIR" in runs[linger]
    assert "systemd-run --user --scope" in runs[linger]


def _test_workers() -> tuple[int, int]:
    steps = _workflow()["jobs"]["test"]["steps"]
    run = next(step["run"] for step in steps if step.get("run", "").startswith("pytest"))
    workers = WORKERS.match(run)
    assert workers, run
    return int(workers["local"]), int(workers["hosted"])


def test_the_local_test_shards_start_at_most_6_pytest_workers_and_hosted_shards_keep_4() -> None:
    job = _workflow()["jobs"]["test"]
    local, hosted = _test_workers()
    assert local == 2
    assert local * len(job["strategy"]["matrix"]["shard"]) <= 6
    assert hosted == 4
    assert job["timeout-minutes"] == 25


def test_the_runner_decision_states_the_local_worker_count_and_the_load_behind_it() -> None:
    text = " ".join((ROOT / "docs/decisions/711-self-hosted-runners.md").read_text(encoding="utf-8").split())
    local, _ = _test_workers()
    assert f"`pytest -n {local}`" in text
    assert "12 workers" in text
    assert "load of about 14" in text
