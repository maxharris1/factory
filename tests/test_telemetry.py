"""Job telemetry on the runner side (issue #93, lib/telemetry.py).

Parse fixtures for both CLIs' streams, scrubbing, the raw log's size cap,
the session's table row, and `orch job run` end to end with a fake harness.
No real CLI, network or ssh: the harness is a script in tmp_path.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lib import job_run, telemetry, waiter
from lib.queue import JobRequest, SQLiteJobQueue
from lib.telemetry import (
    EventLog,
    Scrubber,
    Session,
    Summary,
    count_tests,
    exit_reason,
    harness_argv,
    last_seen,
    start_session,
)
from tests.pool_helpers import pool_claim

ROOT = Path(__file__).resolve().parents[1]
ORCH = ROOT / "bin" / "orch"
FIXTURES = ROOT / "tests" / "fixtures" / "telemetry"
# Fake secrets, built at runtime so no literal token shape sits in the repo.
GH_TOKEN = "gh" + "p_" + "Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0Uv1Wx2"
API_KEY = "sk" + "-proj-" + "Zq8Yw7Xv6Ut5Sr4Qp3On2Ml1"
ENV_SECRET = "hunter2-" + "very-private-value"


def fixture_lines(name: str) -> list[str]:
    text = (FIXTURES / name).read_text(encoding="utf-8")
    text = text.replace("@@GH_TOKEN@@", GH_TOKEN).replace("@@API_KEY@@", API_KEY)
    return text.splitlines()


def parse(name: str, harness: str) -> Summary:
    summary = Summary(harness, Scrubber({}))
    for index, line in enumerate(fixture_lines(name)):
        summary.feed(json.loads(line), 1000.0 + index)
    return summary


def no_secret(value: object) -> None:
    text = json.dumps(value) if not isinstance(value, str) else value
    for secret in (GH_TOKEN, API_KEY, ENV_SECRET):
        assert secret not in text


# --- the harness argv -------------------------------------------------------------


def test_claude_print_mode_gets_stream_json_and_verbose() -> None:
    argv, harness = harness_argv(["claude", "-p", "--dangerously-skip-permissions"])
    assert harness == "claude"
    assert argv == [
        "claude", "--output-format", "stream-json", "--verbose", "-p",
        "--dangerously-skip-permissions",
    ]
    argv, _ = harness_argv(["/usr/local/bin/claude", "--print", "--verbose"])
    assert argv == ["/usr/local/bin/claude", "--output-format", "stream-json", "--print", "--verbose"]


def test_a_claude_command_that_picks_its_own_output_or_is_interactive_runs_as_given() -> None:
    for command in (
        ["claude", "-p", "--output-format", "json"],
        ["claude", "-p", "--output-format=text"],
        ["claude", "--continue"],
    ):
        assert harness_argv(command) == (command, "claude")


def test_codex_exec_gets_json_right_after_exec() -> None:
    argv, harness = harness_argv(["codex", "-m", "gpt-5", "exec", "--full-auto", "-"])
    assert harness == "codex"
    assert argv == ["codex", "-m", "gpt-5", "exec", "--json", "--full-auto", "-"]
    for command in (["codex", "exec", "--json", "-"], ["codex", "resume"], ["codex", "--", "exec"]):
        assert harness_argv(command) == (command, "codex")


def test_any_other_command_runs_unchanged() -> None:
    for command in ([sys.executable, "-c", "pass"], ["cursor-agent", "-p"], []):
        assert harness_argv(command) == (command, None)


# --- parsing both CLIs' streams -----------------------------------------------------


def test_claude_stream_json_fixture_parses_into_the_summary() -> None:
    summary = parse("claude_stream.jsonl", "claude")
    assert summary.events == 12
    assert summary.turns == 6  # the result event's num_turns
    assert dict(summary.tool_calls) == {"Read": 1, "Edit": 1, "Bash": 2}
    assert summary.files_touched == 1 and summary.files == ["lib/app.py"]
    assert summary.commands == 2
    assert (summary.test_runs, summary.tests_passed, summary.tests_failed) == (1, 41, 1)
    # tokens in counts every input token, cached ones included (Codex's convention).
    assert (summary.tokens_in, summary.tokens_cached, summary.tokens_out) == (5460, 5000, 300)
    assert summary.cost_usd == pytest.approx(0.4213)
    assert summary.cli_reason == "success"
    assert summary.last_event_at == 1011.0
    steps = [(step["kind"], step["status"], step["title"]) for step in summary.steps]
    assert steps[:2] == [("tool", "ok", "Read lib/app.py"), ("tool", "ok", "Edit lib/app.py")]
    assert steps[2][:2] == ("command", "error")
    assert steps[2][2].startswith("$ GH_TOKEN=[redacted") and steps[2][2].endswith("git push origin HEAD")
    assert steps[3] == ("command", "ok", "$ python3 -m pytest -q")
    no_secret(summary.row())


def test_claude_live_counts_before_the_result_dedupe_repeated_message_usage() -> None:
    summary = Summary("claude", Scrubber({}))
    for index, line in enumerate(fixture_lines("claude_stream.jsonl")[:-1]):
        summary.feed(json.loads(line), float(index))
    assert summary.turns == 5  # msg_01 arrives twice and counts once
    assert summary.tokens_in == (1210 + 1220 + 1310 + 1410 + 1710)
    assert summary.tokens_out == 5 + 40 + 30 + 20 + 200
    assert summary.cost_usd is None and summary.cli_reason is None


def test_codex_exec_json_fixture_parses_into_the_summary() -> None:
    summary = parse("codex_exec.jsonl", "codex")
    assert summary.events == 13
    assert summary.turns == 1
    assert dict(summary.tool_calls) == {"shell": 3, "file_change": 1, "mcp:github.get_pr": 1}
    assert summary.files == ["…/lib/app.py", "tests/test_app.py"]  # outside the base: the last two parts
    assert summary.commands == 3
    assert (summary.test_runs, summary.tests_passed, summary.tests_failed) == (1, 2, 0)
    assert (summary.tokens_in, summary.tokens_cached, summary.tokens_out) == (24763, 24448, 122)
    assert summary.cost_usd is None  # Codex reports no cost
    assert summary.cli_reason == "completed"
    statuses = [(step["title"].split(" ", 1)[0], step["status"]) for step in summary.steps]
    assert statuses == [("$", "ok"), ("edit", "ok"), ("$", "ok"), ("mcp:github.get_pr", "ok"), ("$", "error")]
    assert summary.steps[-1]["title"].endswith("Authorization: [redacted]")  # the rest of the line goes
    no_secret(summary.row())


def test_codex_turn_failures_and_errors_become_the_exit_reason() -> None:
    summary = Summary("codex", Scrubber({}))
    summary.feed({"type": "turn.started"}, 1.0)
    summary.feed({"type": "turn.failed", "error": {"message": f"401 with key {API_KEY}"}}, 2.0)
    assert summary.cli_reason.startswith("turn failed: 401 with key")
    assert summary.steps[-1]["kind"] == "error"
    no_secret(summary.row())
    no_secret(summary.cli_reason)


def test_unknown_and_malformed_events_are_counted_and_ignored() -> None:
    summary = Summary("claude", Scrubber({}))
    for event in (
        {"type": "assistant", "message": "not a dict"},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "nope"}]}},
        {"type": "item.completed", "item": ["x"]},
        {"type": "something-new"},
        {"no": "type"},
    ):
        summary.feed(event, 5.0)
    assert summary.events == 5
    assert summary.turns == 0 and not summary.steps and not summary.tool_calls


def test_recent_steps_and_files_are_capped() -> None:
    summary = Summary("claude", Scrubber({}))
    for index in range(telemetry.RECENT_STEPS + 15):
        summary.feed(
            {"type": "assistant", "message": {"id": f"m{index}", "content": [
                {"type": "tool_use", "id": f"t{index}", "name": "Write",
                 "input": {"file_path": f"src/file{index}.py"}}]}},
            float(index),
        )
    assert len(summary.steps) == telemetry.RECENT_STEPS
    assert summary.files_touched == telemetry.RECENT_STEPS + 15
    assert len(json.loads(summary.row()["files"])) <= telemetry.MAX_FILES


@pytest.mark.parametrize(
    ("command", "output", "expected"),
    [
        ("pytest -q", "...\n3 failed, 10 passed, 1 error in 1.0s\n", (10, 4)),
        ("npx jest", "Test Suites: 1 failed, 1 total\nTests:       2 failed, 5 passed, 7 total\nTime: 1s", (5, 2)),
        ("cargo test", "test result: ok. 4 passed; 0 failed; 0 ignored\ntest result: FAILED. 1 passed; 2 failed;", (5, 2)),
        ("make test", "all good, nothing counted", None),
        ("ls -la", "3 passed", None),
    ],
)
def test_count_tests(command: str, output: str, expected) -> None:
    assert count_tests(command, output) == expected


def test_exit_reason_names_the_cli_reason_and_how_the_process_ended() -> None:
    assert exit_reason(0, "success") == "success; exit 0"
    assert exit_reason(1, None) == "exit 1"
    assert exit_reason(143, "completed") == "completed; killed by signal 15"
    assert exit_reason(127, None) == "could not start (exit 127)"
    assert exit_reason(None, None) == "runner error"


# --- scrubbing -----------------------------------------------------------------------


def test_scrubber_removes_env_values_token_shapes_and_auth_headers() -> None:
    scrub = Scrubber({"MY_SERVICE_TOKEN": ENV_SECRET, "HOME": "/home/claude-bot", "SHORT_KEY": "abc"})
    text = scrub(f"echo {ENV_SECRET} && curl -H 'Authorization: token {GH_TOKEN}' x", limit=300)
    assert ENV_SECRET not in text and GH_TOKEN not in text
    assert "[redacted-env]" in text
    assert scrub(f"export OPENAI_API_KEY={API_KEY}").startswith("export OPENAI_API_KEY=[redacted")
    assert scrub("cat /home/claude-bot/notes") == "cat /home/claude-bot/notes"  # HOME is not secret
    assert "abc" not in scrub("echo abc")  # a short secret is scrubbed too
    assert scrub("a\nb\tc") == "a b c"
    long = scrub("x " * 400)
    assert len(long) == telemetry.TITLE_LIMIT and long.endswith("…")


# --- the raw log's size cap ------------------------------------------------------------


def test_raw_log_is_private_and_rotates_at_its_cap(tmp_path: Path) -> None:
    messages: list[str] = []
    path = tmp_path / "state" / "job-1" / "events.jsonl"
    log = EventLog(path, max_bytes=100, log=messages.append)
    line = b'{"type":"turn.started"}\n'  # 24 bytes
    for _ in range(10):
        log.write(line)
    log.close()
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    rotated = path.with_name("events.jsonl.1")
    assert stat.S_IMODE(rotated.stat().st_mode) == 0o600
    assert path.stat().st_size <= 100 and rotated.stat().st_size <= 100
    assert sorted(p.name for p in path.parent.iterdir()) == ["events.jsonl", "events.jsonl.1"]
    # 4 lines fit under 100 bytes: [4][4][2] written, the first 4 rotated out.
    assert (rotated.stat().st_size, path.stat().st_size) == (4 * len(line), 2 * len(line))
    assert messages == []


def test_raw_log_default_cap_is_about_20_mb() -> None:
    assert telemetry.MAX_LOG_BYTES == 20 * 1024 * 1024


def test_an_unwritable_raw_log_is_one_line_and_never_stops_the_run(tmp_path: Path) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    messages: list[str] = []
    log = EventLog(blocked / "job" / "events.jsonl", log=messages.append)
    log.write(b"x\n")
    assert len(messages) == 1 and messages[0].startswith("telemetry: raw event log off")


# --- the session: tee, summary and table row --------------------------------------------


def _session(tmp_path: Path, *, connect=None, max_bytes: int = telemetry.MAX_LOG_BYTES, environ=None):
    db = tmp_path / "orch.db"
    messages: list[str] = []
    out = io.BytesIO()
    clock = iter(range(2000, 100000))
    session = Session(
        connect=connect or (lambda: sqlite3.connect(db)),
        job_id="job-abc",
        bot="claude-bot",
        repo="acme/widgets",
        harness="claude",
        argv=["claude"],
        raw_log=EventLog(tmp_path / "state" / "events.jsonl", max_bytes=max_bytes, log=messages.append),
        scrub=Scrubber(environ or {}),
        log=messages.append,
        stdout=out,
        clock=lambda: float(next(clock)),
    )
    return session, db, out, messages


def _row(db: Path, job_id: str = "job-abc") -> dict:
    with sqlite3.connect(db) as connection:
        connection.row_factory = sqlite3.Row
        return dict(connection.execute("SELECT * FROM job_telemetry WHERE job_id = ?", (job_id,)).fetchone())


def test_session_tees_the_stream_and_writes_the_scrubbed_summary(tmp_path: Path) -> None:
    session, db, out, messages = _session(tmp_path)
    lines = fixture_lines("claude_stream.jsonl")
    stream = "\n".join([lines[0], "plain warning line", *lines[1:]]) + "\n"
    session.consume(io.BytesIO(stream.encode()))
    session.flush()
    live = _row(db)
    assert live["finished_at"] is None and live["exit_reason"] is None and live["events"] == 12
    session.finish(0)
    row = _row(db)
    assert out.getvalue() == b"plain warning line\n"  # JSON events stay out of run.log
    raw = (tmp_path / "state" / "events.jsonl").read_bytes()
    assert raw == stream.encode()  # the raw log keeps everything, on this machine only
    assert row["exit_code"] == 0 and row["exit_reason"] == "success; exit 0"
    assert row["finished_at"] is not None and row["updated_at"] >= live["updated_at"]
    assert (row["turns"], row["commands"], row["files_touched"]) == (6, 2, 1)
    assert (row["tests_passed"], row["tests_failed"], row["test_runs"]) == (41, 1, 1)
    assert (row["tokens_in"], row["tokens_cached"], row["tokens_out"]) == (5460, 5000, 300)
    assert row["cost_usd"] == pytest.approx(0.4213)
    assert json.loads(row["tool_calls"]) == {"Bash": 2, "Read": 1, "Edit": 1}
    assert len(json.loads(row["steps"])) == 4
    no_secret(row)
    assert messages == []
    session.flush()  # a flush after finish never rewrites the final row
    assert _row(db)["finished_at"] == row["finished_at"]


def test_session_skips_an_oversized_line_and_keeps_going(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(telemetry, "MAX_LINE_BYTES", 64)
    session, db, out, _ = _session(tmp_path)
    huge_event = json.dumps({"type": "user", "message": {"content": "y" * 500}})
    huge_text = "z" * 200
    stream = "\n".join([huge_event, huge_text, '{"type":"turn.started"}']) + "\n"
    session.consume(io.BytesIO(stream.encode()))
    session.finish(0)
    assert out.getvalue() == (huge_text + "\n").encode()
    assert _row(db)["turns"] == 1


def test_a_job_with_no_events_still_gets_a_row_with_its_exit(tmp_path: Path) -> None:
    session, db, _, _ = _session(tmp_path)
    session.consume(io.BytesIO(b""))
    session.finish(137)
    row = _row(db)
    assert row["events"] == 0 and row["exit_reason"] == "killed by signal 9"
    assert json.loads(row["steps"]) == []


def test_a_failing_table_write_is_one_log_line_and_never_raises(tmp_path: Path) -> None:
    def broken() -> sqlite3.Connection:
        raise sqlite3.OperationalError("database is locked")

    session, _, _, messages = _session(tmp_path, connect=broken)
    session.consume(io.BytesIO(b'{"type":"turn.started"}\n'))
    session.flush()
    session.flush()
    session.finish(0)
    assert len(messages) == 1 and "database is locked" in messages[0]


def test_a_malformed_event_is_skipped_with_one_line(tmp_path: Path, monkeypatch) -> None:
    session, db, _, messages = _session(tmp_path)

    def boom(self, event, now):
        raise KeyError("x")

    monkeypatch.setattr(Summary, "feed", boom)
    session.consume(io.BytesIO(b'{"type":"a"}\n{"type":"b"}\n'))
    session.finish(0)
    assert messages == ["telemetry: skipped an event (KeyError)"]


def test_env_secrets_are_scrubbed_from_titles_and_reasons(tmp_path: Path) -> None:
    session, db, _, _ = _session(tmp_path, environ={"DEPLOY_SECRET": ENV_SECRET})
    event = {"type": "assistant", "message": {"id": "m", "content": [
        {"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": f"deploy --pass {ENV_SECRET}"}}]}}
    session.consume(io.BytesIO((json.dumps(event) + "\n").encode()))
    session.finish(0)
    row = _row(db)
    no_secret(row)
    assert "[redacted-env]" in row["steps"]


@pytest.mark.parametrize("secret", ["7", "42", "4821", "pw!"])
def test_a_short_env_secret_never_reaches_the_table(tmp_path: Path, secret: str) -> None:
    session, db, _, _ = _session(tmp_path, environ={"DEPLOY_PASSWORD": secret + "\n"})
    command = f"deploy -p{secret} --pin={secret}"
    events = [
        {"type": "assistant", "message": {"id": "m", "content": [
            {"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": command}},
            {"type": "tool_use", "id": "u", "name": "Edit", "input": {"file_path": f"/w/{secret}.txt"}}]}},
        {"type": "turn.failed", "error": {"message": f"bad pin {secret}"}},
    ]
    session.consume(io.BytesIO("".join(json.dumps(e) + "\n" for e in events).encode()))
    session.finish(0)
    row = _row(db)
    titles = [step["title"] for step in json.loads(row["steps"])]
    assert len(titles) == 3 and all("[redacted-env]" in title for title in titles)
    for text in (*titles, row["files"], row["exit_reason"], row["tool_calls"]):
        assert secret not in text, text


QUOTED_SECRET = "walrus'" + 'har"b\\or'
QUOTING_ONLY_SECRETS = ["\\", "'", '"', ("'" + '"' + "\\") * 4]


def _unquoted(text: str) -> str:
    return "".join(char for char in text if char not in "'\"\\")


def readable(secret: str, title: str) -> bool:
    """Whether `secret`, or a line of it, reads back from `title` as written,
    through the shell, or with quoting ignored."""
    try:
        words = shlex.split(title)
    except ValueError:
        words = []
    for line in {secret, *secret.splitlines()}:
        if line in title or any(line in word for word in words):
            return True
        if _unquoted(line) and _unquoted(line) in _unquoted(title):
            return True
    return not _unquoted(secret) and any(char in title for char in "'\"\\")


@pytest.mark.parametrize("secret", [
    QUOTED_SECRET, "plainpass", "multi\nline-secret-tail", "'walrus'", "\\harbor\"", *QUOTING_ONLY_SECRETS,
])
@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_a_shell_quoted_env_secret_cannot_be_recovered_from_a_title(harness: str, secret: str) -> None:
    commands = [
        shlex.join(["deploy", "--pin", secret]),
        "deploy --pin " + json.dumps(secret),
        "deploy --pin " + "".join("\\" + char for char in secret),
        "bash -lc " + shlex.quote(shlex.join(["deploy", "--pin", secret])),
        "deploy --pin " + "''".join(secret),
    ]
    summary = Summary(harness, Scrubber({"DEPLOY_PASSWORD": secret}))
    for index, command in enumerate(commands):
        if harness == "claude":
            summary.feed({"type": "assistant", "message": {"id": f"m{index}", "content": [
                {"type": "tool_use", "id": f"t{index}", "name": "Bash", "input": {"command": command}}]}}, 1.0)
        else:
            summary.feed({"type": "item.completed", "item": {
                "id": f"t{index}", "type": "command_execution", "command": command,
                "exit_code": 0, "status": "completed"}}, 1.0)
    titles = [step["title"] for step in json.loads(summary.row()["steps"])]
    assert len(titles) == len(commands)
    for title in titles:
        assert "[redacted-env]" in title, title
        assert not readable(secret, title), title


def test_a_blank_line_in_a_secret_does_not_strip_quoting_from_every_title() -> None:
    scrub = Scrubber({"DEPLOY_KEY": "first-key-line\n\n  \nsecond-key-line"})
    assert scrub("echo 'hi' \"there\"") == "echo 'hi' \"there\""
    assert scrub("cat 'first-key-line'") == "cat [redacted-env]"


def test_flags_and_system_session_variables_are_not_treated_as_secrets() -> None:
    scrub = Scrubber({
        "XDG_SESSION_ID": "3", "XDG_SESSION_TYPE": "tty", "XDG_SESSION_CLASS": "user",
        "CLAUDE_CODE_CHILD_SESSION": "1", "USE_KEYRING": "true", "AUTH_DISABLED": "0",
    })
    assert scrub("pytest -n 3 tests/user_tty.py; exit 1 0 true") == "pytest -n 3 tests/user_tty.py; exit 1 0 true"


@pytest.mark.parametrize(
    "event",
    [
        {"type": "turn.completed", "usage": {"input_tokens": 2**63, "output_tokens": 10**30}},
        {"type": "turn.completed", "usage": {"input_tokens": 1e309, "cached_input_tokens": float("nan")}},
        {"type": "assistant", "message": {"id": "m", "usage": {"input_tokens": 2**64, "output_tokens": 1e309}}},
        {"type": "result", "subtype": "success", "num_turns": 2**70,
         "usage": {"input_tokens": 2**63, "cache_read_input_tokens": 2**63}, "total_cost_usd": 1e309},
        {"type": "result", "subtype": "success", "total_cost_usd": float("nan")},
    ],
)
def test_out_of_range_numbers_are_bounded_and_the_row_is_written(tmp_path: Path, event: dict) -> None:
    session, db, _, messages = _session(tmp_path)
    stream = json.dumps(event) + "\n" + json.dumps(event) + "\n"
    session.consume(io.BytesIO(stream.encode()))
    session.flush()
    session.finish(0)
    row = _row(db)
    assert messages == []
    for name in ("events", "turns", "tokens_in", "tokens_cached", "tokens_out"):
        assert isinstance(row[name], int) and 0 <= row[name] <= telemetry.MAX_COUNT, name
    assert row["cost_usd"] is None
    assert row["events"] == 2


@pytest.mark.parametrize("error", [OverflowError, RecursionError, RuntimeError, KeyError])
def test_flush_and_finish_never_raise_whatever_the_write_raises(tmp_path: Path, error) -> None:
    def broken() -> sqlite3.Connection:
        raise error("boom")

    session, _, _, messages = _session(tmp_path, connect=broken)
    session.consume(io.BytesIO(b'{"type":"turn.started"}\n'))
    session.flush()
    session.flush()
    session.finish(0)
    assert len(messages) == 1 and messages[0].startswith("telemetry: summary not written")
    assert session.raw_log._fd < 0


@pytest.mark.parametrize("opener", ["[", '{"a":'])
def test_a_deeply_nested_line_never_stops_the_drain(tmp_path: Path, opener: str) -> None:
    session, db, out, messages = _session(tmp_path)
    closer = "]" if opener == "[" else "}"
    nested = opener * 100_000 + "1" + closer * 100_000
    stream = "\n".join([nested, '{"type":"turn.started"}', "after"]) + "\n"
    session.consume(io.BytesIO(stream.encode()))
    session.finish(0)
    assert _row(db)["turns"] == 1
    assert out.getvalue().endswith(b"after\n")


def test_session_entry_points_never_raise_from_a_broken_summary(tmp_path: Path, monkeypatch) -> None:
    session, db, _, messages = _session(tmp_path)

    def boom(self):
        raise OverflowError("int too large")

    monkeypatch.setattr(Summary, "row", boom)
    session.consume(io.BytesIO(b'{"type":"turn.started"}\n'))
    session.flush()
    session.finish(0)
    assert messages == ["telemetry: summary not written (OverflowError: int too large)"]


# --- a remote run's ORCH-HANDOFF line still reaches run.log (#87 merge) ------------------


def _stream(*events: object) -> bytes:
    return b"".join(
        (event if isinstance(event, str) else json.dumps(event)).encode() + b"\n" for event in events
    )


def test_claude_result_handoff_line_reaches_run_log_as_its_last_line(tmp_path: Path) -> None:
    session, db, out, _ = _session(tmp_path)
    lines = fixture_lines("claude_stream.jsonl")
    result = json.loads(lines[-1])
    result["result"] = "Posted the handoff.\n\nORCH-HANDOFF: 4242424242\n"
    session.consume(io.BytesIO(("\n".join([*lines[:-1], json.dumps(result)]) + "\n").encode()))
    session.finish(0)
    assert out.getvalue() == b"ORCH-HANDOFF: 4242424242\n"  # nothing else of the message
    assert waiter.handoff_marker(out.getvalue().decode()) == "4242424242"
    assert _row(db)["exit_reason"] == "success; exit 0"


def test_codex_handoff_line_comes_from_the_last_agent_message_only(tmp_path: Path) -> None:
    uuid = "0f8fad5b-d9cb-469f-a165-70867728950e"
    session, _, out, _ = _session(tmp_path)
    session.consume(io.BytesIO(_stream(
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "ORCH-HANDOFF: 1"}},
        {"type": "item.completed", "item": {"id": "b", "type": "agent_message", "text": "Still working."}},
        {"type": "turn.completed", "usage": {}},
    )))
    session.finish(0)
    assert out.getvalue() == b""  # an earlier message's marker is not the final word

    (tmp_path / "second").mkdir()
    session, _, out, _ = _session(tmp_path / "second")
    session.consume(io.BytesIO(_stream(
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": f"Done.\nORCH-HANDOFF: {uuid}"}},
        {"type": "turn.completed", "usage": {}},
    )))
    session.finish(0)
    assert out.getvalue() == f"ORCH-HANDOFF: {uuid}\n".encode()  # once, at turn.completed
    assert waiter.handoff_marker(out.getvalue().decode()) == uuid


def test_handoff_line_goes_out_at_the_end_event_before_the_pipe_closes(tmp_path: Path) -> None:
    session, _, out, _ = _session(tmp_path)
    seen: list[bytes] = []
    lines = [
        json.dumps({"type": "result", "subtype": "success", "result": "ORCH-HANDOFF: failed"}).encode() + b"\n",
    ]

    class Pipe:
        def readline(self, limit: int = -1) -> bytes:
            if lines:
                return lines.pop(0)
            seen.append(out.getvalue())  # what run.log had while the pipe was still open
            return b""

    session.consume(Pipe())
    session.finish(1)
    assert seen == [b"ORCH-HANDOFF: failed\n"]
    assert out.getvalue() == b"ORCH-HANDOFF: failed\n"  # not written twice


def test_a_final_message_without_a_handoff_line_never_reaches_run_log(tmp_path: Path) -> None:
    session, _, out, _ = _session(tmp_path)
    session.consume(io.BytesIO(_stream(
        {"type": ["not", "hashable"], "result": 1},
        {"type": "item.completed", "item": {"type": "agent_message", "text": ["ORCH-HANDOFF: 1"]}},
        {"type": "result", "subtype": "success", "result": "ORCH-HANDOFF: 12\nthen more text"},
        "a plain line",
        {"type": "result", "subtype": "success", "result": "ORCH-HANDOFF: " + "9" * 200},
    )))
    session.finish(0)
    assert out.getvalue() == b"a plain line\n"


def test_last_seen_reads_the_summary_time_or_none(tmp_path: Path) -> None:
    session, db, _, _ = _session(tmp_path)
    with sqlite3.connect(db) as connection:
        assert last_seen(connection, "job-abc") is None  # no table yet
    session.flush()
    with sqlite3.connect(db) as connection:
        assert last_seen(connection, "job-abc") == _row(db)["updated_at"]
        assert last_seen(connection, "other") is None


def test_start_session_is_off_with_orch_telemetry_off(tmp_path: Path) -> None:
    kwargs = dict(
        connect=lambda: sqlite3.connect(tmp_path / "orch.db"),
        database=tmp_path / "orch.db",
        job_id="j1",
        bot="codex-bot",
        repo="acme/widgets",
        command=["codex", "exec", "-"],
        log=lambda line: None,
    )
    assert start_session(environ={"ORCH_TELEMETRY": "off"}, **kwargs) is None
    session = start_session(environ={}, **kwargs)
    assert session is not None
    assert session.argv == ["codex", "exec", "--json", "-"] and session.harness == "codex"
    assert session.raw_log.path == tmp_path / "telemetry-codex-bot" / "j1" / "events.jsonl"
    session = start_session(environ={"ORCH_JOB_STATE_DIR": str(tmp_path / "w")}, **kwargs)
    assert session.raw_log.path == tmp_path / "w" / "events.jsonl"


# --- job_run.run hands the command's stdout to the session ---------------------------------


def test_run_passes_the_commands_stdout_to_output() -> None:
    seen: list[bytes] = []

    def output(stream) -> None:
        seen.extend(stream)

    code = job_run.run(
        [sys.executable, "-c", "print('a'); print('b'); raise SystemExit(3)"],
        beat=lambda: None,
        interval=5,
        log=lambda line: None,
        output=output,
    )
    assert code == 3
    assert seen == [b"a\n", b"b\n"]


def test_run_does_not_wait_forever_for_a_pipe_a_leftover_process_holds(monkeypatch) -> None:
    monkeypatch.setattr(job_run, "OUTPUT_GRACE_SECONDS", 0.2)
    started = time.monotonic()
    code = job_run.run(
        [sys.executable, "-c",
         "import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)'])"],
        beat=lambda: None,
        interval=5,
        log=lambda line: None,
        output=lambda stream: stream.read(),
    )
    assert code == 0
    assert time.monotonic() - started < 2.5


# --- orch job run, end to end with a fake claude ---------------------------------------------


FAKE_CLAUDE = """#!{python}
import json, sys, time
argv = sys.argv[1:]
assert argv[:3] == ["--output-format", "stream-json", "--verbose"], argv
assert argv[3:] == ["-p", "--dangerously-skip-permissions"], argv
for line in open({fixture!r}, encoding="utf-8"):
    sys.stdout.write(line.replace("@@GH_TOKEN@@", {token!r}).replace("@@API_KEY@@", ""))
    sys.stdout.flush()
    time.sleep(0.02)
print("not json: a plain line")
time.sleep(0.5)
"""


def _running_job(tmp_path: Path):
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=time.time)
    queue.enqueue(
        JobRequest(
            repo="acme/widgets",
            ticket_id="12",
            action="build",
            recipient="claude",
            source_comment_id="1",
            timeout_seconds=600,
        ),
        dedupe_key=f"k-{time.monotonic_ns()}",
    )
    return queue, pool_claim(queue, "claude")


def _fake_claude(tmp_path: Path) -> Path:
    path = tmp_path / "bin" / "claude"
    path.parent.mkdir()
    path.write_text(
        FAKE_CLAUDE.format(
            python=sys.executable, fixture=str(FIXTURES / "claude_stream.jsonl"), token=GH_TOKEN
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _orch(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    environment = {k: v for k, v in os.environ.items() if k not in ("ORCH_DB", "ORCH_TELEMETRY")}
    environment.update(env or {})
    return subprocess.run(
        [sys.executable, str(ORCH), *args],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_cli_job_run_streams_claude_telemetry_into_the_table(tmp_path: Path) -> None:
    queue, job = _running_job(tmp_path)
    claude = _fake_claude(tmp_path)
    state = tmp_path / "waiter-job"
    done = _orch(
        "job", "run", "claude-bot", job.id, "--db", str(tmp_path / "orch.db"), "--interval", "0.2",
        "--", str(claude), "-p", "--dangerously-skip-permissions",
        env={"ORCH_JOB_STATE_DIR": str(state)},
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == "not json: a plain line\n"
    assert "telemetry" not in done.stderr
    row = _row(tmp_path / "orch.db", job.id)
    assert row["bot"] == "claude-bot" and row["harness"] == "claude" and row["repo"] == "acme/widgets"
    assert row["exit_reason"] == "success; exit 0"
    assert (row["turns"], row["tests_passed"], row["tests_failed"]) == (6, 41, 1)
    assert row["cost_usd"] == pytest.approx(0.4213)
    assert row["elapsed_seconds"] > 0.2
    no_secret(row)
    raw = (state / "events.jsonl").read_text()
    assert GH_TOKEN in raw  # the raw log is local and unscrubbed; it never leaves the box
    assert stat.S_IMODE((state / "events.jsonl").stat().st_mode) == 0o600
    with queue.connect() as connection:
        heartbeat = connection.execute(
            "SELECT last_activity_at FROM jobs WHERE id = ?", (job.id,)
        ).fetchone()[0]
        # the summary moved with the heartbeats: the last flush came at or after the last beat
        assert last_seen(connection, job.id) >= heartbeat


def test_cli_job_run_with_telemetry_off_runs_the_command_as_given(tmp_path: Path) -> None:
    _, job = _running_job(tmp_path)
    script = "import sys; print(' '.join(sys.argv[1:]))"
    done = _orch(
        "job", "run", "claude-bot", job.id, "--db", str(tmp_path / "orch.db"),
        "--", sys.executable, "-c", script, "-p",
        env={"ORCH_TELEMETRY": "off"},
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == "-p\n"
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    assert "job_telemetry" not in tables
    assert not (tmp_path / "telemetry-claude-bot").exists()


def test_cli_job_run_with_a_plain_command_records_no_events_and_its_exit(tmp_path: Path) -> None:
    _, job = _running_job(tmp_path)
    done = _orch(
        "job", "run", "claude-bot", job.id, "--db", str(tmp_path / "orch.db"),
        "--", sys.executable, "-c", "print('hello'); raise SystemExit(4)",
    )
    assert done.returncode == 4
    assert done.stdout == "hello\n"
    row = _row(tmp_path / "orch.db", job.id)
    assert row["events"] == 0 and row["harness"] == "claude" and row["exit_reason"] == "exit 4"
    log = tmp_path / "telemetry-claude-bot" / job.id / "events.jsonl"
    assert log.read_text() == "hello\n"
    assert stat.S_IMODE(log.parent.parent.stat().st_mode) == 0o700
