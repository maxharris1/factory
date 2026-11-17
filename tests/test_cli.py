import copy
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
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


def test_check_config_refuses_an_unknown_field(tmp_path: Path) -> None:
    data = copy.deepcopy(VALID)
    data["owner_bot"] = "grok"
    path = write_factory(tmp_path, data)
    result = run(["check-config", "--factory", str(path)])
    assert result.returncode == 1
    assert "unknown field 'owner_bot'" in result.stderr
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


def test_poll_refuses_a_sqlite_file_without_a_jobs_table(tmp_path: Path) -> None:
    database = tmp_path / "orch.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE other (id INTEGER)")
    connection.close()
    orch = tmp_path / "orch.yml"
    orch.write_text(
        """
poll_seconds: 60
projects:
  - repo: owner/name
    factory_ref: main
grok:
  webhook_url_env: FACTORY_WEBHOOK_URL
  webhook_key_env: FACTORY_WEBHOOK_KEY
trusted_logins:
  someone:
    - grok-bot
""".lstrip(),
        encoding="utf-8",
    )
    before = database.read_bytes()

    result = run(["poll", "--db", str(database), "--orch", str(orch), "--once"])

    assert result.returncode == 1
    assert "no jobs table" in result.stderr
    assert "Traceback" not in result.stderr
    assert database.read_bytes() == before


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


# --- #108 Unit 1: orch job cancel (#66) -------------------------------------------


def _cancel_queue(tmp_path: Path):
    from lib.queue import JobRequest
    from tests.test_dispatcher import setup

    queue = setup(tmp_path)[1]
    job = queue.enqueue(
        JobRequest(
            repo="owner/repo",
            ticket_id="42",
            action="review",
            recipient="codex",
            source_comment_id="ready-1",
            timeout_seconds=3600,
        ),
        dedupe_key="review:k",
    )
    return queue, job


def _job_state(queue, job_id: str) -> str:
    with queue.connect() as connection:
        return str(
            connection.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
        )


def test_job_cancel_cancels_a_queued_job(tmp_path: Path, capsys) -> None:
    queue, job = _cancel_queue(tmp_path)

    code = cli.main(["job", "cancel", job.id, "--db", str(queue.path), "--reason", "bad replay"])

    assert code == 0
    assert capsys.readouterr().out == (
        f"cancelled {job.id} (owner/repo#42 review -> codex)\n"
    )
    assert _job_state(queue, job.id) == "cancelled"
    with queue.connect() as connection:
        detail = connection.execute(
            "SELECT detail FROM job_events WHERE job_id = ? AND event = 'cancelled'",
            (job.id,),
        ).fetchone()[0]
    assert detail == "bad replay"


def test_job_cancel_refuses_a_running_job(tmp_path: Path, capsys) -> None:
    queue, job = _cancel_queue(tmp_path)
    assert queue.claim("codex") is not None

    code = cli.main(["job", "cancel", job.id, "--db", str(queue.path)])

    assert code == 1
    assert "job is running; stop its worker first" in capsys.readouterr().err
    assert _job_state(queue, job.id) == "running"


def test_job_cancel_of_an_unknown_job_exits_2(tmp_path: Path, capsys) -> None:
    queue, _job = _cancel_queue(tmp_path)

    assert cli.main(["job", "cancel", "nope", "--db", str(queue.path)]) == 2
    assert "unknown job nope" in capsys.readouterr().err


TOKEN_VARIABLES = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
RETRY_GH = """\
import json, os, sys
with open(os.environ["RETRY_GH_LOG"], "a", encoding="utf-8") as handle:
    tokens = sorted(name for name in {tokens!r} if name in os.environ)
    handle.write(json.dumps({{"argv": sys.argv[1:], "tokens": tokens}}) + "\\n")
status = os.environ.get("RETRY_GH_STATUS")
if status:
    print(f"gh: Resource not accessible (HTTP {{status}})", file=sys.stderr)
    sys.exit(1)
print(json.dumps({{"id": 987654}}))
"""


@pytest.fixture
def retry_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    directory = tmp_path / "retry-gh"
    directory.mkdir()
    fallback = None
    script = _write_retry_gh(directory)
    if not os.access(script, os.X_OK):
        fallback = Path(tempfile.mkdtemp(prefix="pytest-retry-gh-", dir="/tmp"))
        directory, script = fallback, _write_retry_gh(fallback)
    log = tmp_path / "gh-calls.jsonl"
    log.write_text("", encoding="utf-8")
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("RETRY_GH_LOG", str(log))
    for name in TOKEN_VARIABLES:
        monkeypatch.setenv(name, "must-not-reach-gh")
    yield lambda: [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    if fallback is not None:
        shutil.rmtree(fallback, ignore_errors=True)


def _write_retry_gh(directory: Path) -> Path:
    body = directory / "retry_gh.py"
    body.write_text(RETRY_GH.format(tokens=TOKEN_VARIABLES), encoding="utf-8")
    script = directory / "gh"
    script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{body}" "$@"\n', encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _retry_db(tmp_path: Path, *, stuck: bool | None, state: str = "fixing"):
    from tests.test_dispatcher import setup

    queue = setup(tmp_path)[1]
    if stuck is not None:
        with sqlite3.connect(queue.path) as connection:
            connection.execute(
                "INSERT INTO ticket_state (repo, ticket_id, stuck, workflow_state, stuck_reason) "
                "VALUES ('owner/repo', '42', ?, ?, ?)",
                (int(stuck), state, "job-failed-2" if stuck else None),
            )
    return queue


def _posted_body(call: dict) -> str:
    argv = call["argv"]
    field = argv[argv.index("-f") + 1]
    assert field.startswith("body=")
    return field[len("body="):]


@pytest.mark.parametrize(
    "ticket",
    [
        "owner/repo#42",
        "https://github.com/owner/repo/pull/42",
        "https://github.com/owner/repo/issues/42",
        "https://github.com/owner/repo/pull/42#issuecomment-1",
    ],
)
def test_retry_posts_on_the_parsed_ticket(tmp_path: Path, retry_gh, capsys, ticket: str) -> None:
    queue = _retry_db(tmp_path, stuck=True)

    code = cli.main(["retry", ticket, "--reason", "Max: try once more", "--db", str(queue.path)])

    assert code == 0
    [call] = retry_gh()
    assert call["argv"][:4] == ["api", "-X", "POST", "repos/owner/repo/issues/42/comments"]
    assert "987654" in capsys.readouterr().out


@pytest.mark.parametrize(
    "ticket",
    [
        "owner/repo",
        "repo#42",
        "owner/repo#",
        "aut0-code/aut0#AUT-7",
        "https://linear.app/aut0/issue/AUT-7",
        "https://gitlab.com/owner/repo/pull/42",
        "https://github.com/owner/repo/commit/42",
    ],
)
def test_retry_rejects_anything_but_a_github_ticket(retry_gh, ticket: str) -> None:
    assert cli.main(["retry", ticket, "--reason", "why", "--force"]) == 2
    assert retry_gh() == []


def test_retry_names_linear_as_out_of_scope(retry_gh, capsys) -> None:
    assert cli.main(["retry", "aut0-code/aut0#AUT-7", "--reason", "why", "--force"]) == 2
    assert "Linear tickets are out of scope" in capsys.readouterr().err


def test_retry_needs_a_reason(retry_gh) -> None:
    assert cli.main(["retry", "owner/repo#42", "--reason", "  ", "--force"]) == 2
    assert cli.main(["retry", "owner/repo#42", "--force"]) == 2
    assert retry_gh() == []


@pytest.mark.parametrize(
    ("stuck", "shown"), [(False, "state fixing"), (None, "state untracked")]
)
def test_retry_refuses_a_ticket_that_is_not_stuck(
    tmp_path: Path, retry_gh, capsys, stuck: bool | None, shown: str
) -> None:
    queue = _retry_db(tmp_path, stuck=stuck)

    code = cli.main(["retry", "Owner/Repo#42", "--reason", "why", "--db", str(queue.path)])

    assert code == 1
    assert f"not stuck ({shown}); nothing to retry" in capsys.readouterr().err
    assert retry_gh() == []


def _fix_running_on_pc(tmp_path: Path, *, failing: bool, pc_enabled: bool) -> tuple[Path, Path]:
    from lib.device_pool import DevicePool
    from lib.queue import JobRequest
    from tests.test_remote_start import config_of, machines_file

    queue = _retry_db(tmp_path, stuck=False)
    queue.enqueue(
        JobRequest(
            repo="owner/repo",
            ticket_id="42",
            action="fix",
            recipient="claude",
            source_comment_id="c-1",
            timeout_seconds=3600,
        ),
        dedupe_key="fix:k",
    )
    pool = DevicePool(queue, config_of(machines_file(tmp_path, box_enabled=False)))
    assert pool.claim("claude") is not None
    if failing:
        pool.record_contact("pc", ok=False, at=100.0)
    (tmp_path / "now").mkdir()
    return queue.path, machines_file(tmp_path / "now", box_enabled=False, pc=pc_enabled)


@pytest.mark.parametrize(
    ("failing", "pc_enabled", "code"),
    [(True, True, 0), (False, False, 0), (False, True, 1)],
    ids=["unreachable", "disabled", "reachable"],
)
def test_retry_posts_for_a_job_stranded_on_its_device(
    tmp_path: Path, retry_gh, capsys, failing: bool, pc_enabled: bool, code: int
) -> None:
    db, machines = _fix_running_on_pc(tmp_path, failing=failing, pc_enabled=pc_enabled)

    assert cli.main(
        ["retry", "owner/repo#42", "--reason", "why", "--db", str(db), "--machines", str(machines)]
    ) == code

    assert len(retry_gh()) == 1 - code
    if code:
        assert "not stuck (state fixing); nothing to retry" in capsys.readouterr().err


def test_retry_reads_device_health_alone_without_a_machines_file(
    tmp_path: Path, retry_gh, capsys
) -> None:
    db, _ = _fix_running_on_pc(tmp_path, failing=True, pc_enabled=True)

    code = cli.main(
        ["retry", "owner/repo#42", "--reason", "why", "--db", str(db),
         "--machines", str(tmp_path / "missing.yml")]
    )

    assert code == 0
    assert len(retry_gh()) == 1
    assert "missing.yml" in capsys.readouterr().err


def _plan_done_with_event(tmp_path: Path, event: str | None):
    from lib.queue import JobRequest

    queue = _retry_db(tmp_path, stuck=False, state="planning")
    queue.enqueue(
        JobRequest(
            repo="owner/repo",
            ticket_id="42",
            action="plan",
            recipient="claude",
            source_comment_id="go-0",
            timeout_seconds=5400,
        ),
        dedupe_key="plan:k",
    )
    plan = queue.claim("claude")
    assert plan is not None
    queue.finish(plan.id, owner="claude", handoff_comment_id="wrong-pr-plan")
    if event is not None:
        with sqlite3.connect(queue.path) as connection:
            connection.execute(
                "INSERT INTO job_events (job_id, repo, ticket_id, action, recipient, event, "
                "detail, recorded_at) VALUES (?, 'owner/repo', '42', 'plan', 'claude', ?, ?, 101)",
                (plan.id, event, "handoff comment wrong-pr-plan is not on the ticket"),
            )
    return queue


def test_retry_allows_a_plan_whose_handoff_never_landed(tmp_path: Path, retry_gh) -> None:
    queue = _plan_done_with_event(tmp_path, "handoff-unverified")

    code = cli.main(["retry", "Owner/Repo#42", "--reason", "why", "--db", str(queue.path)])

    assert code == 0
    assert len(retry_gh()) == 1


@pytest.mark.parametrize("event", ["handoff-verified", None])
def test_retry_refuses_a_planning_ticket_whose_handoff_landed(
    tmp_path: Path, retry_gh, capsys, event: str | None
) -> None:
    queue = _plan_done_with_event(tmp_path, event)

    code = cli.main(["retry", "Owner/Repo#42", "--reason", "why", "--db", str(queue.path)])

    assert code == 1
    assert "not stuck (state planning); nothing to retry" in capsys.readouterr().err
    assert retry_gh() == []


def test_retry_refuses_while_a_harness_job_is_active(
    tmp_path: Path, retry_gh, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lib.queue import JobRequest

    queue = _retry_db(tmp_path, stuck=True)
    job = queue.enqueue(
        JobRequest(
            repo="owner/repo",
            ticket_id="42",
            action="fix",
            recipient="claude",
            source_comment_id="c-1",
            timeout_seconds=3600,
        ),
        dedupe_key="fix:k",
    )
    monkeypatch.setenv("ORCH_DB", str(queue.path))

    assert cli.main(["retry", "owner/repo#42", "--reason", "why"]) == 1
    assert f"has job {job.id} fix -> claude queued" in capsys.readouterr().err
    assert retry_gh() == []


def test_retry_force_posts_without_the_check(tmp_path: Path, retry_gh) -> None:
    queue = _retry_db(tmp_path, stuck=False)

    code = cli.main(
        ["retry", "owner/repo#42", "--reason", "why", "--force", "--db", str(queue.path)]
    )

    assert code == 0
    assert len(retry_gh()) == 1


def test_retry_posts_a_grok_header_with_max_words_and_no_token(
    tmp_path: Path, retry_gh
) -> None:
    from lib.handoff import parse_comment

    queue = _retry_db(tmp_path, stuck=True)

    code = cli.main(
        ["retry", "owner/repo#42", "--reason", "Max: one more go", "--db", str(queue.path)]
    )

    assert code == 0
    [call] = retry_gh()
    assert call["tokens"] == []
    posted = parse_comment(_posted_body(call))
    assert posted.as_dict() == {"kind": "retry", "from": "grok-bot", "role": "orchestrator"}
    assert posted.body == "\nRetry requested: Max: one more go\n"


def test_retry_names_the_trusted_login_on_a_403(
    tmp_path: Path, retry_gh, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _retry_db(tmp_path, stuck=True)
    monkeypatch.setenv("RETRY_GH_STATUS", "403")

    code = cli.main(["retry", "owner/repo#42", "--reason", "why", "--db", str(queue.path)])

    assert code == 1
    assert "trusted_logins login for grok-bot" in capsys.readouterr().err


def test_help_lists_retry(capsys) -> None:
    assert cli.main(["--help"]) == 0
    assert "retry <owner/repo>#<n> --reason TEXT" in capsys.readouterr().out
