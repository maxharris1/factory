"""The bots' real harness commands get their stream flags (issue #264).

claude-bot runs `claude-harness -p ...` (a wrapper that runs `claude "$@"`)
and codex-bot runs `codex-review-harness exec ...`, on the box and on remote
devices. Every form goes through `stream_flags`, logs one fixed line, and its
summary (tokens included) reaches the box's job_telemetry. A CLI that reports
no usage is stored as unknown, never as 0 tokens.
"""

from __future__ import annotations

import io
import json
import shlex
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from lib import dashboard_telemetry, telemetry
from lib.telemetry import STREAM_REASONS, Scrubber, Summary, stream_flags, stream_flags_line
from tests.test_telemetry import _orch, _row, _running_job, _session, fixture_lines

CLAUDE_FLAGS = ["--output-format", "stream-json", "--verbose"]
CLAUDE_BOT = ["-p", "--model", "opus", "--effort", "medium", "--dangerously-skip-permissions"]
CODEX_BOT = ["--skip-git-repo-check", "--sandbox", "workspace-write", "-"]


@pytest.mark.parametrize(
    "command, bot, expected, reason",
    [
        (["claude", *CLAUDE_BOT], "claude", ["claude", *CLAUDE_FLAGS, *CLAUDE_BOT], "claude"),
        (
            ["/home/box/.local/bin/claude-harness", *CLAUDE_BOT],
            "claude",
            ["/home/box/.local/bin/claude-harness", *CLAUDE_FLAGS, *CLAUDE_BOT],
            "claude wrapper",
        ),
        (["claude-harness", *CLAUDE_BOT], "claude", ["claude-harness", *CLAUDE_FLAGS, *CLAUDE_BOT], "claude wrapper"),
        (["codex", "exec", *CODEX_BOT], "codex", ["codex", "exec", "--json", *CODEX_BOT], "codex"),
        (
            ["codex-review-harness", "exec", *CODEX_BOT],
            "codex",
            ["codex-review-harness", "exec", "--json", *CODEX_BOT],
            "codex wrapper",
        ),
    ],
    ids=["claude-bare", "claude-wrapper-path", "claude-wrapper", "codex-bare", "codex-wrapper"],
)
def test_each_configured_command_form_gets_its_stream_flags(
    command: list[str], bot: str, expected: list[str], reason: str
) -> None:
    flags = stream_flags(command, bot)
    assert (flags.argv, flags.harness, flags.added, flags.reason) == (expected, bot, True, reason)
    assert stream_flags_line(flags) == f"telemetry: stream flags added ({reason})"


@pytest.mark.parametrize(
    "command, bot, reason",
    [
        (["claude-harness", "-p", "--output-format", "text"], "claude", "output format already set"),
        (["claude-harness", "--continue"], "claude", "not print mode"),
        (["env", "claude", "-p"], "claude", "wrapper does not pass harness options"),
        (["claude-harness"], "claude", "wrapper does not pass harness options"),
        (["codex-review-harness", "exec", "--json", "-"], "codex", "json already set"),
        (["codex-review-harness", "resume"], "codex", "no exec subcommand"),
        (["cursor-agent", "-p"], "cursor", "not a known harness"),
        ([], "claude", "empty command"),
    ],
)
def test_a_command_left_as_given_says_why(command: list[str], bot: str, reason: str) -> None:
    flags = stream_flags(command, bot)
    assert flags.argv == command and not flags.added and flags.reason == reason
    assert reason in STREAM_REASONS
    assert stream_flags_line(flags) == f"telemetry: stream flags skipped ({reason})"


@pytest.mark.parametrize("worker_env", [False, True], ids=["box", "remote"])
def test_the_session_runs_the_flagged_wrapper_and_logs_one_line(tmp_path: Path, worker_env: bool) -> None:
    messages: list[str] = []
    session = telemetry.start_session(
        connect=lambda: sqlite3.connect(tmp_path / "orch.db"),
        database=tmp_path / "orch.db",
        job_id="job-abc",
        bot="claude-bot",
        repo="acme/widgets",
        command=["claude-harness", *CLAUDE_BOT],
        log=messages.append,
        environ={},
        stdout=io.BytesIO(),
        worker_env=worker_env,
        device="pc" if worker_env else "box",
    )
    assert session is not None
    assert session.argv == ["claude-harness", *CLAUDE_FLAGS, *CLAUDE_BOT]
    assert session.harness == "claude"
    assert messages == ["telemetry: stream flags added (claude wrapper)"]


# --- end to end: a wrapper on the box and on a remote device ---------------------------

FAKE_CLAUDE = """#!{python}
import json, sys
argv = sys.argv[1:]
if argv[:3] != ["--output-format", "stream-json", "--verbose"]:
    print("text mode: no events")
    sys.exit(0)
for line in open({fixture!r}, encoding="utf-8"):
    sys.stdout.write(line.replace("@@GH_TOKEN@@", "x").replace("@@API_KEY@@", ""))
print("ORCH-HANDOFF: done")
"""

WRAPPER = """#!/bin/sh
exec {claude} "$@"
"""


def _fake_bin(directory: Path, fixture: str = "claude_stream.jsonl") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    claude = directory / "claude"
    claude.write_text(
        FAKE_CLAUDE.format(python=sys.executable, fixture=str(Path(__file__).parent / "fixtures" / "telemetry" / fixture)),
        encoding="utf-8",
    )
    claude.chmod(0o755)
    wrapper = directory / "claude-harness"
    wrapper.write_text(WRAPPER.format(claude=shlex.quote(str(claude))), encoding="utf-8")
    wrapper.chmod(0o755)
    return wrapper


def test_a_box_claude_wrapper_job_records_events_turns_and_tokens(tmp_path: Path) -> None:
    _, job = _running_job(tmp_path)
    wrapper = _fake_bin(tmp_path / "bin")
    done = _orch(
        "job", "run", "claude-bot", job.id, "--db", str(tmp_path / "orch.db"), "--interval", "0.2",
        "--", str(wrapper), *CLAUDE_BOT,
        env={"ORCH_JOB_STATE_DIR": str(tmp_path / "state")},
    )
    assert done.returncode == 0, done.stderr
    assert "orch job run: telemetry: stream flags added (claude wrapper)\n" in done.stderr
    assert done.stdout.splitlines()[-1] == "ORCH-HANDOFF: done"  # #87: still run.log's last line
    row = _row(tmp_path / "orch.db", job.id)
    assert row["harness"] == "claude" and row["events"] > 0 and row["turns"] == 6
    assert (row["tokens_in"], row["tokens_out"], row["tokens_unknown"]) == (5460, 300, 0)


def test_a_remote_claude_wrapper_job_records_its_summary_on_the_box(tmp_path: Path) -> None:
    from tests.test_remote_start import remote_setup

    machines, queue, _, job_id, ssh = remote_setup(tmp_path, "claude")
    home = tmp_path / "worker-home"
    home.mkdir()
    worker_bin = tmp_path / "worker-bin"
    _fake_bin(worker_bin)
    (home / ".bash_profile").write_text(f"export PATH={shlex.quote(str(worker_bin))}:$PATH\n", encoding="utf-8")
    run = _orch(
        "job", "run", "claude-bot", job_id, "--db", str(queue.path), "--machines", str(machines),
        "--", "claude-harness", *CLAUDE_BOT,
        env={"HOME": str(tmp_path), **ssh.env(), "FAKE_SSH_HOME": str(home)},
    )
    assert run.returncode == 0, run.stderr
    assert "telemetry: stream flags added (claude wrapper)" in run.stderr
    assert run.stdout.splitlines()[-1] == "ORCH-HANDOFF: done"
    row = _row(queue.path, job_id)
    assert row["harness"] == "claude" and row["events"] > 0 and row["turns"] == 6
    assert (row["tokens_in"], row["tokens_out"], row["tokens_unknown"]) == (5460, 300, 0)
    assert row["exit_reason"] == "success; exit 0"


# --- no usage reported: unknown, not 0 ---------------------------------------------------


def _without_usage(name: str) -> list[dict]:
    events = []
    for line in fixture_lines(name):
        event = json.loads(line)
        event.pop("usage", None)
        if isinstance(event.get("message"), dict):
            event["message"].pop("usage", None)
        events.append(event)
    return events


@pytest.mark.parametrize(
    "harness, fixture", [("claude", "claude_stream.jsonl"), ("codex", "codex_exec.jsonl")]
)
def test_a_stream_without_usage_stores_tokens_unknown(harness: str, fixture: str) -> None:
    summary = Summary(harness, Scrubber({}))
    for index, event in enumerate(_without_usage(fixture)):
        summary.feed(event, 1000.0 + index)
    row = summary.row()
    assert row["events"] > 0 and row["turns"] > 0
    assert row["tokens_unknown"] == 1 and row["tokens_in"] == row["tokens_out"] == 0
    reported = Summary(harness, Scrubber({}))
    for index, line in enumerate(fixture_lines(fixture)):
        reported.feed(json.loads(line), 1000.0 + index)
    assert reported.row()["tokens_unknown"] == 0


def test_the_dashboard_shows_no_data_for_a_job_without_usage(tmp_path: Path) -> None:
    session, db, _, _ = _session(tmp_path)
    for event in _without_usage("claude_stream.jsonl"):
        session.consume(io.BytesIO((json.dumps(event) + "\n").encode()))
    session.finish(0)
    stored = _row(db)
    assert stored["tokens_unknown"] == 1
    with sqlite3.connect(db) as connection:
        connection.row_factory = sqlite3.Row
        read = dashboard_telemetry.read_telemetry(connection, stored["updated_at"] + 1)
    rollup = read["rollup"]
    recent = rollup["recent"][0]
    assert recent["tokens_known"] is False
    assert rollup["totals"]["unmetered_jobs"] == 1
    assert dashboard_telemetry._stats(recent)[5] == ("no data", "tokens in / out", "")
    page = dashboard_telemetry.rollup_panel({"telemetry": rollup})
    assert "1 jobs report no token usage (no data)" in page
    assert '<td class="num muted">no data</td>' in page


def test_an_older_table_gains_tokens_unknown_and_old_zero_rows_read_as_no_data(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    old = telemetry.DDL.replace(",\n    tokens_unknown INTEGER", "")
    assert old != telemetry.DDL
    with sqlite3.connect(db) as connection:
        connection.execute(old)
        values = {name: 0 for name in telemetry.COLUMNS if name != "tokens_unknown"}
        values.update(job_id="old", bot="claude-bot", harness="claude", repo="acme/widgets",
                      started_at=time.time(), updated_at=time.time(), finished_at=time.time(),
                      tool_calls="{}", steps="[]", cost_usd=None, exit_reason="exit 0",
                      tests_passed=None, tests_failed=None, last_event_at=None,
                      plan=None, current_task=None, last_finished=None, subagents=None)
        connection.execute(
            f"INSERT INTO job_telemetry ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
            tuple(values.values()),
        )
        connection.row_factory = sqlite3.Row
        before = dashboard_telemetry.read_telemetry(connection, time.time())
        assert before["rollup"]["recent"][0]["tokens_known"] is False
        telemetry.ensure_table(connection)
        assert "tokens_unknown" in {row[1] for row in connection.execute("PRAGMA table_info(job_telemetry)")}
