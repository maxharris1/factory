"""Job telemetry on the runner side (issue #93, lib/telemetry.py).

Parse fixtures for both CLIs' streams, scrubbing, the raw log's size cap,
the session's table row, and `orch job run` end to end with a fake harness.
No real CLI, network or ssh: the harness is a script in tmp_path.
"""

from __future__ import annotations

import ast
import io
import json
import os
import shutil
import warnings
import shlex
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lib import job_run, telemetry, waiter
from lib.pstack_pin import load_pin
from lib.queue import JobRequest, SQLiteJobQueue
from lib.telemetry import (
    EventLog,
    Scrubber,
    Session,
    SkillReads,
    Summary,
    count_tests,
    exit_reason,
    harness_argv,
    last_seen,
    start_session,
    stream_flags,
)
from tests.pool_helpers import LOCAL_BOX_MACHINES, pool_claim
from tests.test_remote_start import past_uncontained

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


@pytest.mark.parametrize(
    "command, expected",
    [
        (
            ["codex", "--profile", "exec", "exec", "--help"],
            ["codex", "--profile", "exec", "exec", "--json", "--help"],
        ),
        (
            ["codex", "-p", "exec", "-m", "exec", "e", "-"],
            ["codex", "-p", "exec", "-m", "exec", "e", "--json", "-"],
        ),
        (
            ["codex", "-c", "exec", "--profile=exec", "exec", "-"],
            ["codex", "-c", "exec", "--profile=exec", "exec", "--json", "-"],
        ),
        (
            ["codex", "exec", "-", "--", "--json"],
            ["codex", "exec", "--json", "-", "--", "--json"],
        ),
        (["codex", "--profile", "exec", "resume"], ["codex", "--profile", "exec", "resume"]),
        (["codex", "-m", "exec"], ["codex", "-m", "exec"]),
        (["codex", "review", "exec"], ["codex", "review", "exec"]),
    ],
)
def test_codex_json_goes_after_the_exec_subcommand_never_an_option_value(
    command: list[str], expected: list[str]
) -> None:
    assert harness_argv(command) == (expected, "codex")


def test_any_other_command_runs_unchanged() -> None:
    for command in ([sys.executable, "-c", "pass"], ["unknown-agent", "-p"], []):
        assert harness_argv(command) == (command, None)


# --- parsing both CLIs' streams -----------------------------------------------------


def test_claude_stream_json_fixture_parses_into_the_summary() -> None:
    summary = parse("claude_stream.jsonl", "claude")
    assert summary.events == 12
    assert summary.turns == 6  # the result event's num_turns
    assert dict(summary.tool_calls) == {"Read": 1, "Edit": 1, "Bash": 2}
    assert summary.files_touched == 1
    assert summary.commands == 2
    assert (summary.test_runs, summary.tests_passed, summary.tests_failed) == (1, 41, 1)
    # tokens in counts every input token, cached ones included (Codex's convention).
    assert (summary.tokens_in, summary.tokens_cached, summary.tokens_out) == (5460, 5000, 300)
    assert summary.cost_usd == pytest.approx(0.4213)
    assert summary.cli_reason == "success"
    assert summary.last_event_at == 1011.0
    steps = [(step["kind"], step["status"], step["label"]) for step in summary.steps]
    assert steps == [
        ("tool", "ok", "Read"), ("tool", "ok", "Edit"), ("command", "error", "command"), ("command", "ok", "command"),
    ]
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
    assert summary.files_touched == 2
    assert summary.commands == 3
    assert (summary.test_runs, summary.tests_passed, summary.tests_failed) == (1, 2, 0)
    assert (summary.tokens_in, summary.tokens_cached, summary.tokens_out) == (24763, 24448, 122)
    assert summary.cost_usd is None  # Codex reports no cost
    assert summary.cli_reason == "completed"
    statuses = [(step["label"], step["status"]) for step in summary.steps]
    assert statuses == [
        ("command", "ok"), ("edit 2 files", "ok"), ("command", "ok"), ("mcp:github.get_pr", "ok"), ("command", "error"),
    ]
    no_secret(summary.row())


def test_claude_tokens_are_the_session_total_over_every_model_helpers_included(tmp_path: Path) -> None:
    session, db, _, _ = _session(tmp_path)
    session.consume(io.BytesIO((FIXTURES / "claude_helpers_stream.jsonl").read_bytes()))
    session.finish(0)
    row = _row(db)
    stored = (row["tokens_in"], row["tokens_cached"], row["tokens_out"])
    assert stored == (7 + 58000 + 12700 + 4 + 20000 + 30000, 58000 + 20000, 350 + 120)
    assert stored != (4 + 700 + 43000, 43000, 6)
    assert row["tokens_unknown"] == 0 and row["cost_usd"] == pytest.approx(0.5)


@pytest.mark.parametrize("model_usage", [{}, {"claude-opus-5-5": None}, None])
def test_claude_without_model_usage_keeps_the_result_usage(model_usage: object) -> None:
    usage = {"input_tokens": 100, "cache_read_input_tokens": 1000, "output_tokens": 200}
    summary = _feed(Summary("claude", Scrubber({})),
                    {"type": "result", "subtype": "success", "usage": usage, "modelUsage": model_usage})
    assert (summary.tokens_in, summary.tokens_cached, summary.tokens_out) == (1100, 1000, 200)


CODEX_154 = FIXTURES / "codex_154_review"


def _codex_session(tmp_path: Path, home: Path) -> tuple[Session, Path]:
    db = tmp_path / "orch.db"
    session = Session(
        connect=lambda: sqlite3.connect(db), job_id="job-154", bot="codex-bot", repo="acme/widgets",
        harness="codex", flags=stream_flags(["codex", "exec", "-"]), raw_log=None, scrub=Scrubber({}),
        log=lambda _m: None, stdout=io.BytesIO(), clock=lambda: 1794673532.0, rollout_home=str(home),
    )
    return session, db


def test_a_codex_review_stores_codex_own_session_total_not_the_turn_sums(tmp_path: Path) -> None:
    session, db = _codex_session(tmp_path, CODEX_154)
    session.consume(io.BytesIO((CODEX_154 / "exec.jsonl").read_bytes()))
    session.finish(0)
    row = _row(db, "job-154")
    assert (row["tokens_in"], row["tokens_cached"], row["tokens_out"]) == (5_200_000, 4_900_000, 40_000)
    assert row["tokens_in"] + row["tokens_out"] != 2100
    assert row["tokens_unknown"] == 0


def test_a_codex_turn_after_the_rollout_total_never_adds_to_it(tmp_path: Path) -> None:
    home = tmp_path / "codex-home"
    shutil.copytree(CODEX_154, home)
    [rollout] = (home / "sessions").rglob("rollout-*.jsonl")
    session, db = _codex_session(tmp_path, home)
    session.consume(io.BytesIO((CODEX_154 / "exec.jsonl").read_bytes()))
    session.flush()
    with rollout.open("ab") as out:
        out.write(b'{"timestamp":"2026-11-14T16:50:00.000Z","type":"event_msg","payload":'
                  b'{"type":"token_count","info":null,"rate_limits":null}}\n')
    session.consume(io.BytesIO(b'{"type":"turn.completed","usage":'
                               b'{"input_tokens":900,"cached_input_tokens":800,"output_tokens":70}}\n'))
    session.finish(0)
    row = _row(db, "job-154")
    assert (row["tokens_in"], row["tokens_cached"], row["tokens_out"]) == (5_200_000, 4_900_000, 40_000)


NULL_TOKEN_COUNT = (b'{"timestamp":"2026-11-14T16:50:00.000Z","type":"event_msg","payload":'
                    b'{"type":"token_count","info":null,"rate_limits":null}}\n')


@pytest.mark.parametrize("prior_flush", [False, True])
def test_a_last_token_count_without_info_keeps_the_last_session_total(tmp_path: Path, prior_flush: bool) -> None:
    home = tmp_path / "codex-home"
    shutil.copytree(CODEX_154, home)
    [rollout] = (home / "sessions").rglob("rollout-*.jsonl")
    session, db = _codex_session(tmp_path, home)
    session.consume(io.BytesIO((CODEX_154 / "exec.jsonl").read_bytes()))
    if prior_flush:
        session.flush()
    with rollout.open("ab") as out:
        out.write(NULL_TOKEN_COUNT)
    session.finish(0)
    row = _row(db, "job-154")
    assert (row["tokens_in"], row["tokens_cached"], row["tokens_out"]) == (5_200_000, 4_900_000, 40_000)


CODEX_154_THREAD = "00000000-0000-4000-8000-000000000154"


def _helper_rollout(parent: Path, thread: str, meta: dict, name: str | None = None) -> Path:
    lines = parent.read_text().splitlines()
    first = json.loads(lines[0])
    first["payload"].update(id=thread, **meta)
    path = parent.with_name(name or parent.name.replace(CODEX_154_THREAD, thread))
    path.write_text("\n".join([json.dumps(first), *lines[1:]]) + "\n")
    return path


def test_a_codex_job_total_includes_every_helper_agent_it_spawned(tmp_path: Path) -> None:
    home = tmp_path / "codex-home"
    shutil.copytree(CODEX_154, home)
    [parent] = (home / "sessions").rglob("rollout-*.jsonl")
    root = CODEX_154_THREAD
    child, grandchild = "00000000-0000-4000-8000-000000000155", "00000000-0000-4000-8000-000000000156"
    _helper_rollout(parent, child, {"parent_thread_id": root})
    later = parent.parent.parent / "15"
    later.mkdir()
    _helper_rollout(parent, grandchild,
                    {"source": {"subagent": {"thread_spawn": {"parent_thread_id": child, "depth": 2}}}}
                    ).rename(later / f"rollout-2026-11-15T00-10-00-{grandchild}.jsonl")
    _helper_rollout(parent, "00000000-0000-4000-8000-000000000157",
                    {"parent_thread_id": "00000000-0000-4000-8000-000000000999"})
    _helper_rollout(parent, "00000000-0000-4000-8000-000000000158", {"parent_thread_id": root},
                    name="rollout-2026-11-13T09-00-00-00000000-0000-4000-8000-000000000158.jsonl")
    session, db = _codex_session(tmp_path, home)
    session.consume(io.BytesIO((CODEX_154 / "exec.jsonl").read_bytes()))
    session.finish(0)
    row = _row(db, "job-154")
    assert (row["tokens_in"], row["tokens_cached"], row["tokens_out"]) == (15_600_000, 14_700_000, 120_000)


HELPER_155 = "00000000-0000-4000-8000-000000000155"
BIG_TOOL_OUTPUT = (json.dumps({"timestamp": "2026-11-14T16:48:00.000Z", "type": "response_item",
                               "payload": {"type": "function_call_output", "call_id": "call-1",
                                           "output": "x" * 200_000}}) + "\n").encode()
SMALLER_TOKEN_COUNT = (b'{"timestamp":"2026-11-14T16:50:00.000Z","type":"event_msg","payload":'
                       b'{"type":"token_count","info":{"total_token_usage":{"input_tokens":2600000,'
                       b'"cached_input_tokens":2400000,"output_tokens":21000,"total_tokens":2621000}},'
                       b'"rate_limits":null}}\n')


def _parent_and_helper(tmp_path: Path) -> tuple[Session, Path, dict[str, Path]]:
    home = tmp_path / "codex-home"
    shutil.copytree(CODEX_154, home)
    [parent] = (home / "sessions").rglob("rollout-*.jsonl")
    helper = _helper_rollout(parent, HELPER_155, {"parent_thread_id": CODEX_154_THREAD})
    session, db = _codex_session(tmp_path, home)
    session.consume(io.BytesIO((CODEX_154 / "exec.jsonl").read_bytes()))
    session.flush()
    assert _stored_tokens(db) == (10_400_000, 9_800_000, 80_000, 0)
    return session, db, {"parent": parent, "helper": helper}


def _stored_tokens(db: Path) -> tuple:
    row = _row(db, "job-154")
    return row["tokens_in"], row["tokens_cached"], row["tokens_out"], row["tokens_unknown"]


@pytest.mark.parametrize("rollout", ["helper", "parent"])
def test_a_rollout_total_pushed_out_of_the_read_window_keeps_its_share_of_the_job_total(
    tmp_path: Path, rollout: str
) -> None:
    session, db, rollouts = _parent_and_helper(tmp_path)
    with rollouts[rollout].open("ab") as out:
        out.write(BIG_TOOL_OUTPUT * 22 + NULL_TOKEN_COUNT)
    session.finish(0)
    assert _stored_tokens(db) == (10_400_000, 9_800_000, 80_000, 0)


def test_a_helper_rollout_that_cannot_be_read_keeps_its_share_of_the_job_total(tmp_path: Path) -> None:
    session, db, rollouts = _parent_and_helper(tmp_path)
    rollouts["helper"].unlink()
    session.finish(0)
    assert _stored_tokens(db) == (10_400_000, 9_800_000, 80_000, 0)


def test_a_newer_smaller_total_for_the_same_rollout_replaces_its_share(tmp_path: Path) -> None:
    session, db, rollouts = _parent_and_helper(tmp_path)
    with rollouts["helper"].open("ab") as out:
        out.write(SMALLER_TOKEN_COUNT)
    session.finish(0)
    assert _stored_tokens(db) == (5_200_000 + 2_600_000, 4_900_000 + 2_400_000, 40_000 + 21_000, 0)


def test_codex_turn_failures_and_errors_become_the_exit_reason() -> None:
    summary = Summary("codex", Scrubber({}))
    summary.feed({"type": "turn.started"}, 1.0)
    summary.feed({"type": "turn.failed", "error": {"message": f"401 with key {API_KEY}"}}, 2.0)
    assert summary.cli_reason == "turn failed"
    assert summary.steps[-1] == {"at": 2.0, "kind": "error", "status": "error", "label": "turn failed"}
    summary.feed({"type": "error", "message": "stream disconnected"}, 3.0)
    assert summary.cli_reason == "error"
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


def test_claude_result_reasons_are_fixed_labels() -> None:
    for subtype, is_error, reason in [
        ("success", False, "success"), ("success", True, "error"), ("error_max_turns", True, "max turns reached"),
        ("error_max_budget_usd", True, "error"), (f"weird {API_KEY}", False, "finished"), (None, False, "finished"),
        (["x"], False, "finished"),
    ]:
        summary = Summary("claude", Scrubber({}))
        summary.feed({"type": "result", "subtype": subtype, "is_error": is_error, "result": API_KEY}, 1.0)
        assert summary.cli_reason == reason


def test_recent_steps_are_capped_and_files_only_counted() -> None:
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
    assert "files" not in summary.row() and "src/file" not in json.dumps(summary.row())


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
    assert len(long) == telemetry.LABEL_LIMIT and long.endswith("…")


@pytest.mark.parametrize("prefix", ["", "deploy --pin ", "x " * 2000])
def test_the_scrubber_matches_a_secret_before_it_cuts_the_text(prefix: str) -> None:
    """#100 r9 finding 2: a 2,400-character secret is matched whole, then the text is cut."""
    secret = "walrusharbor" * 200
    scrub = Scrubber({"DEPLOY_PASSWORD": secret})
    for limit in (telemetry.LABEL_LIMIT, 300):
        text = scrub(prefix + secret + " done", limit=limit)
        assert "walr" not in text and "harbor" not in text, text


def test_a_text_too_long_to_match_whole_is_hidden_whole() -> None:
    scrub = Scrubber({"DEPLOY_PASSWORD": "walrusharbor"})
    fits = "y " * (telemetry.SCRUB_LIMIT // 2)
    assert scrub(fits) == fits[: telemetry.LABEL_LIMIT - 1] + "…"
    assert scrub(fits + "y") == telemetry.HIDDEN


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


def _session(
    tmp_path: Path,
    *,
    connect=None,
    max_bytes: int = telemetry.MAX_LOG_BYTES,
    environ=None,
    worker_env=False,
    skills=None,
):
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
        flags=stream_flags(["claude"]),
        raw_log=EventLog(tmp_path / "state" / "events.jsonl", max_bytes=max_bytes, log=messages.append),
        scrub=Scrubber(environ or {}, blind=worker_env),
        log=messages.append,
        stdout=out,
        clock=lambda: float(next(clock)),
        worker_env=worker_env,
        skills=skills,
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


def test_env_secrets_are_scrubbed_from_tool_names(tmp_path: Path) -> None:
    session, db, _, _ = _session(tmp_path, environ={"DEPLOY_SECRET": ENV_SECRET})
    event = {"type": "assistant", "message": {"id": "m", "content": [
        {"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": f"deploy --pass {ENV_SECRET}"}},
        {"type": "tool_use", "id": "u", "name": f"mcp__{ENV_SECRET}", "input": {}}]}}
    session.consume(io.BytesIO((json.dumps(event) + "\n").encode()))
    session.finish(0)
    row = _row(db)
    no_secret(row)
    assert [step["label"] for step in json.loads(row["steps"])] == ["command", "mcp__[redacted-env]"]


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
    labels = [step["label"] for step in json.loads(row["steps"])]
    assert labels == ["command", "Edit", "turn failed"]
    for text in (*labels, row["exit_reason"], row["tool_calls"]):
        assert secret not in text, text


QUOTED_SECRET = "walrus'" + 'har"b\\or'
QUOTING_ONLY_SECRETS = ["\\", "'", '"', ("'" + '"' + "\\") * 4]


def _unquoted(text: str) -> str:
    return "".join(char for char in text if char not in "'\"\\")


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for pair in value.items() for item in pair for text in _strings(item)]
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _strings(item)]
    return []


def _decoded_once(text: str) -> set[str]:
    """What `text` reads as after one round of shell, JSON or Python string decoding."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        found = {text.encode("latin-1", "backslashreplace").decode("unicode_escape", "replace")}
    try:
        found.update(shlex.split(text))
    except ValueError:
        pass
    for candidate in (text, f'"{text}"'):
        try:
            found.update(_strings(json.loads(candidate)))
        except ValueError:
            pass
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            found.update(_strings(ast.literal_eval(text)))
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        pass
    return found


def _readings(title: str, rounds: int = 3) -> set[str]:
    readings = frontier = {title}
    for _ in range(rounds):
        frontier = {text for item in frontier for text in _decoded_once(item)} - readings
        readings = readings | frontier
    return readings


def readable(secret: str, title: str) -> bool:
    """Whether `secret`, or a line of it, reads back from `title` as written,
    with quoting ignored, or after up to three rounds of shell, JSON or Python
    string decoding."""
    for reading in _readings(title):
        for line in {secret, *secret.splitlines()}:
            if line in reading:
                return True
            if _unquoted(line) and _unquoted(line) in _unquoted(reading):
                return True
    return not _unquoted(secret) and any(char in title for char in "'\"\\")


@pytest.mark.parametrize("secret", [
    QUOTED_SECRET, "plainpass", "multi\nline-secret-tail", "'walrus'", "\\harbor\"", *QUOTING_ONLY_SECRETS,
])
def test_the_scrubber_hides_a_shell_quoted_env_secret(secret: str) -> None:
    commands = [
        shlex.join(["deploy", "--pin", secret]),
        "deploy --pin " + json.dumps(secret),
        "deploy --pin " + "".join("\\" + char for char in secret),
        "bash -lc " + shlex.quote(shlex.join(["deploy", "--pin", secret])),
        "deploy --pin " + "''".join(secret),
    ]
    scrub = Scrubber({"DEPLOY_PASSWORD": secret})
    for command in commands:
        text = scrub(command, limit=300)
        assert "[redacted-env]" in text, text
        assert not readable(secret, text), text


ESCAPED_SECRETS = [
    "walrus\tharbor",
    "walrus\u00e9harbor",
    "walrus\U0001f600harbor",
    "walrus\x01harbor\x7f",
    "wal/rus\u2028harbor",
    "walrus\\u00e9harbor",
    "walrus\\tharbor",
    QUOTED_SECRET + "\t\u00e9",
]
STRING_ENCODINGS = {
    "json": json.dumps,
    "json-utf8": lambda text: json.dumps(text, ensure_ascii=False),
    "json-object": lambda text: json.dumps({"pin": text}),
    "json-twice": lambda text: json.dumps(json.dumps({"pin": text})),
    "python": repr,
    "python-ascii": ascii,
}
SHELL_ENCODINGS = {
    "as-is": lambda text: text,
    "quoted": shlex.quote,
    "backslashed": lambda text: "".join("\\" + char for char in text),
    "per-character": lambda text: "".join(shlex.quote(char) for char in text),
    "nested": lambda text: shlex.quote("deploy --pin " + shlex.quote(text)),
}


@pytest.mark.parametrize("secret", ESCAPED_SECRETS)
def test_the_scrubber_hides_an_escaped_env_secret(secret: str) -> None:
    commands = [
        "deploy --pin " + shell(string(secret))
        for string in STRING_ENCODINGS.values()
        for shell in SHELL_ENCODINGS.values()
    ]
    commands.append(shlex.join(["deploy", "--json", json.dumps({"pin": secret})]))
    scrub = Scrubber({"DEPLOY_PASSWORD": secret})
    for command in commands:
        text = scrub(command, limit=300)
        assert "[redacted-env]" in text, (command, text)
        assert not readable(secret, text), (command, text)


def test_the_scrubber_reads_bash_ansi_c_and_locale_quoting() -> None:
    """A sub-agent description is free text (#147), so a secret written in
    bash's $'...' (with its \\E escape) or $"..." quoting goes too."""
    esc, plain = "walrus\x1bharbor-pin", "otter" + "lantern-key"
    scrub = Scrubber({"DEPLOY_PASSWORD": esc, "DEPLOY_TOKEN": plain})
    for word in (
        "$'walrus\\Eharbor-pin'",
        "$'walrus\\eharbor-pin'",
        "$'walr'$'us\x1bh'$'arbo'$'r-pi'$'n'",
        '$"otte"$"rlan"$"tern"$"-key"',
        "$'otter\\x6cantern-key'",
    ):
        assert scrub(f"check {word} now", 200) == "check [redacted-env] now", word
    assert scrub("echo $'it\\'s' $\"HOME\" $5", 200) == "echo $'it\\'s' $\"HOME\" $5"


def test_escape_decoding_keeps_text_that_holds_no_secret() -> None:
    scrub = Scrubber({"DEPLOY_PASSWORD": "walrus\u00e9harbor"})
    command = 'printf "a\\tb\\u00e9" --json \'{"k": "v\\n"}\''
    assert scrub(command, limit=300) == command


def test_a_blank_line_in_a_secret_does_not_strip_quoting_from_every_text() -> None:
    scrub = Scrubber({"DEPLOY_KEY": "first-key-line\n\n  \nsecond-key-line"})
    assert scrub("echo 'hi' \"there\"") == "echo 'hi' \"there\""
    assert scrub("cat 'first-key-line'") == "cat [redacted-env]"


def test_flags_and_system_session_variables_are_not_treated_as_secrets() -> None:
    scrub = Scrubber({
        "XDG_SESSION_ID": "3", "XDG_SESSION_TYPE": "tty", "XDG_SESSION_CLASS": "user",
        "CLAUDE_CODE_CHILD_SESSION": "1", "USE_KEYRING": "true", "AUTH_DISABLED": "0",
    })
    assert scrub("pytest -n 3 tests/user_tty.py; exit 1 0 true", limit=300) == "pytest -n 3 tests/user_tty.py; exit 1 0 true"


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
        "a plain line",
        {"type": "result", "subtype": "success", "result": "Done.\nSay ORCH-HANDOFF: 12 to finish."},
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
    assert session.flags.argv == ["codex", "exec", "--json", "-"] and session.harness == "codex"
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


def _running_job(tmp_path: Path, machines: Path | None = None):
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
    return queue, pool_claim(queue, "claude", machines)


def _fake_claude(tmp_path: Path, fixture: str = "claude_stream.jsonl") -> Path:
    path = tmp_path / "bin" / "claude"
    path.parent.mkdir()
    path.write_text(
        FAKE_CLAUDE.format(
            python=sys.executable, fixture=str(FIXTURES / fixture), token=GH_TOKEN
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
        "job", "run", "claude-bot", job.id, "--machines", str(LOCAL_BOX_MACHINES), "--db", str(tmp_path / "orch.db"), "--interval", "0.2",
        "--", str(claude), "-p", "--dangerously-skip-permissions",
        env={"ORCH_JOB_STATE_DIR": str(state)},
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == "not json: a plain line\n"
    assert [line for line in done.stderr.splitlines() if "telemetry" in line] == [
        "orch job run: telemetry: stream flags added (claude)"
    ]
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
        "job", "run", "claude-bot", job.id, "--machines", str(LOCAL_BOX_MACHINES), "--db", str(tmp_path / "orch.db"),
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
        "job", "run", "claude-bot", job.id, "--machines", str(LOCAL_BOX_MACHINES), "--db", str(tmp_path / "orch.db"),
        "--", sys.executable, "-c", "print('hello'); raise SystemExit(4)",
    )
    assert done.returncode == 4
    assert done.stdout == "hello\n"
    row = _row(tmp_path / "orch.db", job.id)
    assert row["events"] == 0 and row["harness"] == "claude" and row["exit_reason"] == "exit 4"
    log = tmp_path / "telemetry-claude-bot" / job.id / "events.jsonl"
    assert log.read_text() == "hello\n"
    assert stat.S_IMODE(log.parent.parent.stat().st_mode) == 0o700


# --- a remote run scrubs against the worker's environment (#100 r5) ---------------------

WORKER_SECRET = "walrus" + "harbor-pin"
_REMOTE_EVENTS = {
    "claude": (
        ["-p"],
        "{'type': 'assistant', 'message': {'id': 'm', 'content': [{'type': 'tool_use', "
        "'id': 't', 'name': 'Bash', 'input': {'command': cmd}}]}}",
    ),
    "codex": (
        ["exec", "-"],
        "{'type': 'item.completed', 'item': {'id': 't', 'type': 'command_execution', "
        "'command': cmd, 'status': 'completed', 'exit_code': 0}}",
    ),
}
_PROFILES = {
    "newline": "echo welcome",
    "no-newline": "printf welcome",
    "background": (
        '(if [ -n "$ORCH_WORKER_TAG" ]; then w=0; '
        'until [ -e "$HOME/box-done" ] || [ "$w" -ge 600 ]; do sleep 0.05; w=$((w + 1)); done; '
        "else sleep 0.2; fi; printf late) &"
    ),
    "traced": "set -x",
}


def _remote_run(
    tmp_path: Path, harness: str, env: dict | None = None, *, profile: str = "newline",
    link_delay: float = 0,
):
    from tests.test_remote_start import remote_setup

    machines, queue, _, job_id, ssh = remote_setup(tmp_path, harness)
    if link_delay:
        ssh.fault(f"fakepc-{harness}-bot", "delay", link_delay)
    home = tmp_path / "worker-home"
    home.mkdir()
    worker_bin = tmp_path / "worker-bin"
    worker_bin.mkdir()
    (home / ".bash_profile").write_text(
        f"export PATH={shlex.quote(str(worker_bin))}:$PATH\n"
        f"export DEPLOY_PASSWORD={shlex.quote(WORKER_SECRET)}\n{_PROFILES[profile]}\n",
        encoding="utf-8",
    )
    args, event = _REMOTE_EVENTS[harness]
    cli = worker_bin / harness
    cli.write_text(
        f"#!{sys.executable}\nimport json, os\n"
        "cmd = 'deploy --pin ' + os.environ['DEPLOY_PASSWORD']\n"
        f"print(json.dumps({event}))\n"
        "print('plain worker line')\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    run = _orch(
        "job", "run", f"{harness}-bot", job_id, "--db", str(queue.path),
        "--machines", str(machines), "--", str(cli), *args,
        env={"HOME": str(tmp_path), **ssh.env(), "FAKE_SSH_HOME": str(home), **(env or {})},
    )
    (home / "box-done").touch()
    return run, queue.path, job_id


@pytest.mark.parametrize("profile", sorted(_PROFILES))
@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_a_secret_only_the_workers_login_profile_exports_is_never_published(
    tmp_path: Path, harness: str, profile: str
) -> None:
    from lib.dashboard import collect_snapshot, write_snapshot_pages

    run, db, job_id = _remote_run(tmp_path, harness, profile=profile)
    assert run.returncode == 0, run.stderr
    row = _row(db, job_id)
    stored = [step["label"] for step in json.loads(row["steps"])]
    status = write_snapshot_pages(
        tmp_path / "public", collect_snapshot(db, tmp_path / "empty-log", now=time.time())
    )[0]
    published = json.loads(status.read_text())["telemetry"]["recent"][0]["steps"]
    assert stored == [step["label"] for step in published] == ["command"]
    raw = (tmp_path / f"telemetry-{harness}-bot" / job_id / "events.jsonl").read_text()
    # #128: a detached run's device log holds its stdout and stderr together,
    # so the profile's own output is relayed ahead of the report line; it is
    # read with the scrubber still blind, and the report itself never kept.
    assert run.stdout.endswith("plain worker line\n") and raw.endswith("plain worker line\n")
    assert "late" not in raw
    for text in (run.stdout, run.stderr, raw):
        assert "ORCH-WORKER-ENV" not in text and WORKER_SECRET.encode().hex() not in text
    for text in (run.stdout, run.stderr, json.dumps(row), status.read_text()):
        assert WORKER_SECRET not in text


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_a_profiles_background_output_stays_out_of_the_relay_on_a_slow_link(
    tmp_path: Path, harness: str
) -> None:
    run, _, job_id = _remote_run(tmp_path, harness, profile="background", link_delay=0.3)
    assert run.returncode == 0, run.stderr
    raw = (tmp_path / f"telemetry-{harness}-bot" / job_id / "events.jsonl").read_text()
    assert run.stdout.endswith("plain worker line\n") and raw.endswith("plain worker line\n")
    assert "late" not in raw


_TRACING = {
    "xtrace": "set -x",
    "xtrace-fd": "exec 9>&2; BASH_XTRACEFD=9; set -o xtrace",
    "verbose": "set -xv",
    "debug-trap": "set -T; trap 'eval \"echo $BASH_COMMAND\" >&2' DEBUG",
}


@pytest.mark.parametrize("report_env", [False, True], ids=["plain", "reporting"])
@pytest.mark.parametrize("tracing", sorted(_TRACING))
def test_a_tracing_login_profile_never_echoes_an_environment_value(
    tmp_path: Path, tracing: str, report_env: bool
) -> None:
    from lib.remote import RemoteTarget, new_tag, start_script

    home = tmp_path / "home"
    home.mkdir()
    (home / ".bash_profile").write_text(
        f"export DEPLOY_PASSWORD={shlex.quote(WORKER_SECRET)}\n{_TRACING[tracing]}\n", encoding="utf-8"
    )
    job_secret = "otter" + "lantern-key"
    target = RemoteTarget("pc", "unused", "codex-bot", str(tmp_path / "work"), "job", new_tag())
    script = start_script(target, ["/usr/bin/printf", "finished\n"], {"JOB_TOKEN": job_secret}, report_env=report_env)
    run = subprocess.run(
        ["sh", "-c", script], env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        start_new_session=True, capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.endswith("finished\n")
    for secret in (WORKER_SECRET, job_secret):
        assert secret not in run.stderr and secret.encode().hex() not in run.stderr


def test_with_telemetry_off_a_remote_start_reports_no_environment(tmp_path: Path) -> None:
    run, db, _ = _remote_run(tmp_path, "codex", env={"ORCH_TELEMETRY": "off"})
    assert run.returncode == 0, run.stderr
    assert past_uncontained(run.stdout.encode()).splitlines()[0] == b"welcome"
    assert "ORCH-WORKER-ENV" not in run.stdout


def _report(environ: dict[str, str], *, end: bool = True) -> bytes:
    entries = [f"{name}={value}" for name, value in environ.items()] + (["ORCH-WORKER-ENV-END"] if end else [])
    return b"ORCH-WORKER-ENV " + "".join(entry + "\0" for entry in entries).encode().hex().encode() + b"\n"


def _remote_session(tmp_path: Path, stream: bytes):
    session, db, out, messages = _session(tmp_path, worker_env=True)
    session.consume(io.BytesIO(stream))
    session.finish(3)
    return _row(db), out, messages


def _tool_call(name: str) -> bytes:
    event = {"type": "assistant", "message": {"id": "m", "content": [
        {"type": "tool_use", "id": "t", "name": name, "input": {"command": name}}]}}
    return (json.dumps(event) + "\n").encode()


def test_a_remote_session_scrubs_the_reported_environment_and_never_keeps_the_report(tmp_path: Path) -> None:
    report = _report({"DEPLOY_PASSWORD": WORKER_SECRET, "PATH": "/usr/bin"})
    call = _tool_call(f"mcp__{WORKER_SECRET}")
    row, out, messages = _remote_session(tmp_path, b"hi\n" + report + call)
    assert [step["label"] for step in json.loads(row["steps"])] == ["mcp__[redacted-env]"]
    assert row["exit_reason"] == "exit 3" and WORKER_SECRET not in json.dumps(row)
    assert out.getvalue() == b"hi\n" and messages == []
    assert (tmp_path / "state" / "events.jsonl").read_bytes() == b"hi\n" + call


def test_a_report_that_does_not_start_its_line_is_still_never_kept(tmp_path: Path) -> None:
    report = _report({"DEPLOY_PASSWORD": WORKER_SECRET})
    call = _tool_call(f"mcp__{WORKER_SECRET}")
    row, out, messages = _remote_session(tmp_path, b"welcome" + report + call)
    assert [step["label"] for step in json.loads(row["steps"])] == ["mcp__[redacted-env]"]
    assert out.getvalue() == b"welcome" and messages == []
    assert (tmp_path / "state" / "events.jsonl").read_bytes() == b"welcome" + call


@pytest.mark.parametrize(
    "report",
    [b"", _report({"DEPLOY_PASSWORD": WORKER_SECRET}, end=False), b"ORCH-WORKER-ENV zz\n"],
    ids=["missing", "cut-short", "not-hex"],
)
def test_a_remote_session_without_a_whole_report_hides_every_text(tmp_path: Path, report: bytes) -> None:
    row, _, messages = _remote_session(tmp_path, report + _tool_call(f"mcp__{WORKER_SECRET}") + _tool_call("Bash"))
    assert [step["label"] for step in json.loads(row["steps"])] == ["[redacted-env]", "command"]
    assert json.loads(row["tool_calls"]) == {"[redacted-env]": 2}
    assert WORKER_SECRET not in json.dumps(row)
    assert row["exit_reason"] == "exit 3"
    assert messages == ([] if not report else ["telemetry: unreadable worker environment report; tool names hidden"])


def test_an_oversized_report_is_dropped_whole_and_the_session_stays_blind(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(telemetry, "MAX_LINE_BYTES", 64)
    report = _report({"DEPLOY_PASSWORD": WORKER_SECRET * 8})
    row, out, _ = _remote_session(tmp_path, report + b"after\n")
    assert out.getvalue() == b"after\n"
    assert (tmp_path / "state" / "events.jsonl").read_bytes() == b"after\n"
    assert row["exit_reason"] == "exit 3"


# --- the live run: plan, current and last task, sub-agents (#147) ------------------------

LIVE_RUN = ("plan", "current_task", "last_finished", "subagents")
LIVE_FIXTURES = [
    ("claude_subagents_stream.jsonl", "claude"),
    ("claude_tasks_v2_stream.jsonl", "claude"),
    ("codex_todo_exec.jsonl", "codex"),
]
# Text the fixtures hold in fields telemetry must never read: prompts, results,
# summaries, output files, a Bash description, a TaskCreate description, assistant text.
SENTINELS = ("SENTINEL",)
# The #93 table, as an older runner created it.
OLD_DDL = """
CREATE TABLE job_telemetry (
    job_id TEXT PRIMARY KEY, bot TEXT NOT NULL, harness TEXT NOT NULL, repo TEXT NOT NULL,
    started_at REAL NOT NULL, updated_at REAL NOT NULL, last_event_at REAL, finished_at REAL,
    elapsed_seconds REAL NOT NULL, events INTEGER NOT NULL, turns INTEGER NOT NULL,
    tool_calls TEXT NOT NULL, files_touched INTEGER NOT NULL, commands INTEGER NOT NULL,
    test_runs INTEGER NOT NULL, tests_passed INTEGER, tests_failed INTEGER,
    tokens_in INTEGER NOT NULL, tokens_cached INTEGER NOT NULL, tokens_out INTEGER NOT NULL,
    cost_usd REAL, exit_code INTEGER, exit_reason TEXT, steps TEXT NOT NULL
)
"""


def _feed(summary: Summary, *events: dict) -> Summary:
    for index, event in enumerate(events):
        summary.feed(event, 1000.0 + index)
    return summary


def _use(call_id: str, name: str, data: object) -> dict:
    return {"type": "assistant", "message": {"id": f"m-{call_id}", "content": [
        {"type": "tool_use", "id": call_id, "name": name, "input": data}]}}


def _result(call_id: str, text: str = "ok", *, error: bool = False) -> dict:
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": call_id, "content": text, "is_error": error}]}}


def _todos(*items: tuple[str, str]) -> dict:
    return {"todos": [{"content": text, "status": status, "activeForm": f"Doing {text}"} for text, status in items]}


def _live(summary: Summary) -> dict:
    row = summary.row()
    return {
        "plan": json.loads(row["plan"]),
        "current_task": row["current_task"],
        "last_finished": row["last_finished"],
        "subagents": json.loads(row["subagents"]),
    }


def test_claude_subagents_fixture_fills_the_live_run() -> None:
    summary = parse("claude_subagents_stream.jsonl", "claude")
    live = _live(summary)
    assert live["plan"] == [
        {"text": "Read the telemetry module", "status": "done"},
        {"text": "Write the fixture tests in [path]", "status": "done"},
        {"text": "Push with GH_TOKEN=[redacted-token]", "status": "in_progress"},
    ]
    assert live["current_task"] == "Pushing with GH_TOKEN=[redacted-token]"  # the item's active form
    assert live["last_finished"] == "Write the fixture tests in [path]"
    assert live["subagents"] == [
        # background: the launch acknowledgement leaves it running; task_notification finishes it
        {"kind": "Explore", "description": "Find the stream parser", "status": "finished",
         "started_at": 1003.0, "finished_at": 1015.0},
        # run_in_background false: its tool_result is the finish, an error result is a failure
        {"kind": "general-purpose", "description": "Check the CI config", "status": "failed",
         "started_at": 1012.0, "finished_at": 1013.0},
        # finished, resumed by SendMessage (matched by task id), then stopped
        {"kind": "Plan", "description": "Review the diff", "status": "failed",
         "started_at": 1016.0, "finished_at": 1023.0},
        # no subagent_type: Claude Code's default; still running when the stream ends
        {"kind": "general-purpose", "description": "Summarize the run", "status": "running",
         "started_at": 1024.0, "finished_at": None},
    ]
    assert json.loads(summary.row()["tool_calls"]) == {"Agent": 4, "TodoWrite": 3, "Bash": 1, "SendMessage": 1}
    no_secret(summary.row())


def test_a_resumed_subagent_runs_again_until_its_next_notification() -> None:
    lines = [json.loads(line) for line in fixture_lines("claude_subagents_stream.jsonl")]
    resumed = next(index for index, event in enumerate(lines) if event.get("tool_use_id") == "toolu_send")
    summary = _feed(Summary("claude", Scrubber({})), *lines[: resumed + 1])
    review = [agent for agent in _live(summary)["subagents"] if agent["description"] == "Review the diff"]
    assert review == [{"kind": "Plan", "description": "Review the diff", "status": "running",
                       "started_at": 1016.0, "finished_at": None}]


def test_claude_tasks_v2_fixture_fills_the_plan() -> None:
    live = _live(parse("claude_tasks_v2_stream.jsonl", "claude"))
    assert live == {
        # task 3 was deleted; the create that failed never became an item
        "plan": [{"text": "Add the columns", "status": "done"}, {"text": "Parse the stream", "status": "in_progress"}],
        "current_task": "Parsing the stream",
        "last_finished": "Add the columns",
        "subagents": [],
    }


def test_codex_todo_list_fixture_fills_the_plan_and_derives_in_progress() -> None:
    summary = parse("codex_todo_exec.jsonl", "codex")
    live = _live(summary)
    assert live["plan"] == [
        {"text": "Inspect the repo layout", "status": "done"},
        {"text": "Write tests for [redacted-token] rotation", "status": "done"},
        {"text": "Run pytest", "status": "in_progress"},  # the first item not completed
    ]
    assert (live["current_task"], live["last_finished"]) == ("Run pytest", "Write tests for [redacted-token] rotation")
    assert summary.commands == 1 and summary.turns == 1
    no_secret(summary.row())


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_a_newly_finished_item_counts_even_when_its_text_repeats(harness: str) -> None:
    def snapshot(last_done: bool) -> dict:
        items = [("repeat", True), ("another", True), ("repeat", last_done)]
        if harness == "claude":
            return _use("t", "TodoWrite", _todos(*((text, "completed" if done else "pending") for text, done in items)))
        return {"type": "item.updated", "item": {"id": "t", "type": "todo_list",
                "items": [{"text": text, "completed": done} for text, done in items]}}

    summary = _feed(Summary(harness, Scrubber({})), snapshot(False))
    assert _live(summary)["last_finished"] == "another"
    assert _live(_feed(summary, snapshot(True)))["last_finished"] == "repeat"


def test_codex_reports_no_subagents() -> None:
    """Codex's collab_tool_call (spawn_agent) carries only a prompt and agent
    messages, no description or type: sub-agents are not available for codex."""
    summary = parse("codex_todo_exec.jsonl", "codex")
    assert _live(summary)["subagents"] == []
    assert "PROMPT-SENTINEL" not in json.dumps(summary.row())


def test_codex_first_list_puts_its_first_item_in_progress_and_finishes_nothing() -> None:
    item = {"id": "item_1", "type": "todo_list", "items": [
        {"text": "one", "completed": False}, {"text": "two", "completed": False}]}
    live = _live(_feed(Summary("codex", Scrubber({})), {"type": "item.started", "item": item}))
    assert live["plan"] == [{"text": "one", "status": "in_progress"}, {"text": "two", "status": "pending"}]
    assert (live["current_task"], live["last_finished"]) == ("one", "")


@pytest.mark.parametrize("name, harness", LIVE_FIXTURES)
def test_prompts_results_and_summaries_never_reach_the_row(name: str, harness: str) -> None:
    lines = fixture_lines(name)
    assert any(sentinel in line for line in lines for sentinel in SENTINELS)
    row = parse(name, harness).row()
    for sentinel in SENTINELS:
        assert sentinel not in json.dumps(row)
    no_secret(row)


def test_live_run_text_is_scrubbed_capped_and_one_line() -> None:
    scrub = Scrubber({"DEPLOY_SECRET": ENV_SECRET})
    long = "word " * 100
    summary = _feed(
        Summary("claude", scrub),
        _use("t1", "TodoWrite", _todos(
            (f"Rotate {GH_TOKEN} and\nthe {ENV_SECRET} key", "completed"),
            ("Call https://api.example.com/v1 as ops@example.com from ~/bin/run and /etc/hosts", "in_progress"),
            (long, "pending"),
            ("x" * (telemetry.SCRUB_LIMIT + 5), "pending"),
        )),
        _use("a1", "Agent", {"description": f"Check {API_KEY}\nfor leaks", "subagent_type": "k" * 80}),
    )
    row = summary.row()
    no_secret(row)
    live = _live(summary)
    texts = [item["text"] for item in live["plan"]]
    assert texts[0] == "Rotate [redacted-token] and the [redacted-env] key"
    assert texts[1] == "Call [redacted-url] as [redacted-email] from [path]"
    assert len(texts[2]) == telemetry.TASK_TEXT_LIMIT == 120 and texts[2].endswith("…")
    assert texts[3] == telemetry.HIDDEN  # longer than the scrubber reads: hidden whole
    assert live["current_task"] == "Doing Call [redacted-url] as [redacted-email] from [path]"
    agent = live["subagents"][0]
    assert agent["description"] == "Check [redacted-token] for leaks"
    assert len(agent["kind"]) == telemetry.LABEL_LIMIT
    for text in [*texts, live["current_task"], live["last_finished"], agent["description"], agent["kind"]]:
        assert "\n" not in text and len(text) <= telemetry.TASK_TEXT_LIMIT


ABSOLUTE_PATHS = [
    "/home/bot/private/deployment.txt",
    "//home/bot/private/deployment.txt",
    "~/private/deployment.txt",
    "~bot/private/deployment.txt",
    r"C:\Users\bot\private\deployment.txt",
    "C:/Users/bot/private/deployment.txt",
    r"c:\private",
    r"\\fileserver\bot\private\deployment.txt",
    r"\Users\bot\private\deployment.txt",
]


PATH_CUTS = [
    *((f"Inspect {path}", "Inspect [path]") for path in ABSOLUTE_PATHS),
    *((f"Inspect {path} then report", "Inspect [path]") for path in ABSOLUTE_PATHS),
    ("Inspect build-host:/srv/factory/private/deployment.txt", "Inspect [path]"),
    ("Inspect bot@build-host:/srv/factory/private/deployment.txt", "Inspect [path]"),
    ("Inspect bot@[2001:db8::1]:/srv/factory/private/deployment.txt", "Inspect [path]"),
    ("Inspect [2001:db8::1]:/srv/factory/private/deployment.txt", "Inspect [path]"),
    ("Inspect file:/srv/factory/private/deployment.txt", "Inspect [path]"),
    (r"Inspect build-host:C:\Users\bot\private\deployment.txt", "Inspect [path]"),
    ("Inspect path=/srv/factory/private/deployment.txt now", "Inspect [path]"),
    ("Inspect (/srv/factory/private/deployment.txt)", "Inspect [path]"),
    (r"Inspect %USERPROFILE%\private\deployment.txt", "Inspect [path]"),
    ('Read "/Users/John Doe/private/deployment.txt"', "Read [path]"),
    ("Read '/Users/John Doe/private/deployment.txt' first", "Read [path]"),
    ("Read `~/My Files/private/deployment.txt` first", "Read [path]"),
    (r'Read "C:\Users\John Doe\private\deployment.txt"', "Read [path]"),
    ('Read "build-host:/srv/John Doe/private/deployment.txt"', "Read [path]"),
    ('Read "/Users/John Doe/private/deployment.txt', "Read [path]"),
    (r'Read "/Users/John \"The Builder\" Doe/private/deployment.txt"', "Read [path]"),
    ("Read '/Users/John O'Brien/private/deployment.txt'", "Read [path]"),
    ("Read \u201c/Users/John Doe/private/deployment.txt\u201d", "Read [path]"),
    ("Read /Users/John\tDoe/private/deployment.txt", "Read [path]"),
    (r"Read /Users/John\ Doe/private/deployment.txt", "Read [path]"),
    ("/Users/John Doe/private/deployment.txt", "[path]"),
    ("Compile with -I/home/bot/private/include", "Compile with [path]"),
    ("Compile with --sysroot=/home/bot/private", "Compile with [path]"),
    (r"Compile with /I\home\bot\private", "Compile with [path]"),
    ('Read "/ company secrets/private.txt"', "Read [path]"),
    (r"Read '\ company secrets\private.txt'", "Read [path]"),
    ("Inspect lib/x,/home/bot/private/deployment.txt", "Inspect [path]"),
    ("Inspect x:/home/bot/private/deployment.txt", "Inspect [path]"),
    ("Inspect <~/private/deployment.txt>", "Inspect [path]"),
    (r"Read /\ company\ secrets/private.txt", "Read [path]"),
    (r"Read /\ company\ secrets/private.txt then report", "Read [path]"),
    ("Read /\\\tcompany/private.txt", "Read [path]"),
    (r"Read \\\ company\ secrets\private.txt", "Read [path]"),
    (r"Read //\ company/private.txt", "Read [path]"),
]


def _persisted_live(tmp_path: Path, harness: str, text: str, environ: dict | None = None) -> dict:
    if harness == "claude":
        events = [
            _use("t1", "TodoWrite", {"todos": [{"content": text, "status": "in_progress", "activeForm": text}]}),
            _use("a1", "Agent", {"description": text, "subagent_type": "general-purpose"}),
        ]
    else:
        events = [{"type": "item.started", "item": {
            "id": "t", "type": "todo_list", "items": [{"text": text, "completed": False}]}}]
    db = tmp_path / f"{harness}.db"
    session = Session(
        connect=lambda: sqlite3.connect(db), job_id=harness, bot=f"{harness}-bot", repo="acme/widgets",
        harness=harness, flags=stream_flags([]), raw_log=None, scrub=Scrubber(environ or {}), log=lambda _: None, stdout=io.BytesIO(),
    )
    session.consume(io.BytesIO("".join(json.dumps(event) + "\n" for event in events).encode()))
    session.finish(0)
    row = _row(db, harness)
    return {**row, "plan": json.loads(row["plan"]), "subagents": json.loads(row["subagents"])}


@pytest.mark.parametrize(("text", "cut"), PATH_CUTS)
def test_every_absolute_path_form_is_cut_whole_before_it_is_stored(tmp_path: Path, text: str, cut: str) -> None:
    claude = _persisted_live(tmp_path, "claude", text)
    codex = _persisted_live(tmp_path, "codex", text)
    for live in (claude, codex):
        assert [item["text"] for item in live["plan"]] == [cut]
        assert live["current_task"] == cut
        assert "private" not in json.dumps(live)
    assert claude["subagents"][0]["description"] == cut


@pytest.mark.parametrize("text", ["and/or", "TCP/IP", "1/2 done", "a / b", "Read a \\ b", r"Read a \\ b", "see https://example.com/x", "see file:///etc/x", "Note: a/b", "it's a/b", "edit lib/telemetry.py", "run ./setup.sh", "24/7 on-call", "edit lib/telemetry.py.", "see (lib/x.py)", "use ../x/y", "a // b"])
def test_relative_slashes_are_not_paths(text: str) -> None:
    live = _live(_feed(Summary("claude", Scrubber({})), _use("t1", "TodoWrite", _todos((text, "pending")))))
    assert "[path]" not in live["plan"][0]["text"]


@pytest.mark.parametrize("secret", ["correct horse /battery staple", "/srv/deploy key", "hunter2 -I/x"])
def test_an_environment_secret_with_a_slash_is_hidden_whole(tmp_path: Path, secret: str) -> None:
    for harness in ("claude", "codex"):
        live = _persisted_live(tmp_path, harness, f"Rotate {secret} now", {"DEPLOY_SECRET": secret})
        assert [item["text"] for item in live["plan"]] == ["Rotate [redacted-env] now"]
        assert live["current_task"] == "Rotate [redacted-env] now"


def test_a_blind_remote_session_hides_task_text(tmp_path: Path) -> None:
    session, db, _, _ = _session(tmp_path, worker_env=True)
    session.consume(io.BytesIO("".join(line + "\n" for line in fixture_lines("claude_subagents_stream.jsonl")).encode()))
    session.finish(0)
    row = _row(db)
    plan, agents = json.loads(row["plan"]), json.loads(row["subagents"])
    assert {item["text"] for item in plan} == {telemetry.HIDDEN}
    assert {agent["description"] for agent in agents} == {agent["kind"] for agent in agents} == {telemetry.HIDDEN}
    assert row["current_task"] == row["last_finished"] == telemetry.HIDDEN
    assert [item["status"] for item in plan] == ["done", "done", "in_progress"]


def test_current_task_falls_back_to_the_running_subagent_then_none() -> None:
    summary = Summary("claude", Scrubber({}))
    assert _live(summary) == {"plan": [], "current_task": "", "last_finished": "", "subagents": []}
    _feed(
        summary,
        _use("a1", "Agent", {"description": "Older helper"}),
        _use("a2", "Agent", {"description": "Newer helper"}),
        _use("b1", "Bash", {"command": "make", "description": "Bash text is never a task"}),
    )
    assert _live(summary)["current_task"] == "Newer helper"
    summary.feed({"type": "system", "subtype": "task_notification", "tool_use_id": "a2", "status": "completed"}, 2000.0)
    assert _live(summary)["current_task"] == "Older helper"
    summary.feed({"type": "system", "subtype": "task_notification", "tool_use_id": "a1", "status": "failed"}, 2001.0)
    assert _live(summary)["current_task"] == ""
    summary.feed(_use("t1", "TodoWrite", _todos(("Plan it", "pending"))), 2002.0)
    assert _live(summary)["current_task"] == ""  # a list with nothing in progress, and no running sub-agent


def test_a_long_plan_keeps_twelve_items_around_the_one_in_progress() -> None:
    items = [(f"step {n}", "completed" if n < 15 else "in_progress" if n == 15 else "pending") for n in range(30)]
    plan = _live(_feed(Summary("claude", Scrubber({})), _use("t", "TodoWrite", _todos(*items))))["plan"]
    assert [item["text"] for item in plan] == [f"step {n}" for n in range(13, 25)]
    near_end = [(f"step {n}", "in_progress" if n == 28 else "pending") for n in range(30)]
    plan = _live(_feed(Summary("claude", Scrubber({})), _use("t", "TodoWrite", _todos(*near_end))))["plan"]
    assert [item["text"] for item in plan] == [f"step {n}" for n in range(18, 30)]
    none_started = [(f"step {n}", "pending") for n in range(30)]
    plan = _live(_feed(Summary("claude", Scrubber({})), _use("t", "TodoWrite", _todos(*none_started))))["plan"]
    assert [item["text"] for item in plan] == [f"step {n}" for n in range(12)]


def test_subagents_keep_the_running_ones_then_the_latest_finished() -> None:
    summary = Summary("claude", Scrubber({}))
    for n in range(12):
        summary.feed(_use(f"a{n}", "Agent", {"description": f"agent {n}"}), 1000.0 + n)
    for n in (0, 1, 2, 3, 4, 5):
        summary.feed({"type": "system", "subtype": "task_notification", "tool_use_id": f"a{n}", "status": "completed"},
                     2000.0 - n)  # a0 finished last
    agents = _live(summary)["subagents"]
    assert [agent["description"] for agent in agents] == ["agent 0", "agent 1"] + [f"agent {n}" for n in range(6, 12)]
    for n in range(6, 12):
        summary.feed({"type": "system", "subtype": "task_notification", "tool_use_id": f"a{n}", "status": "completed"}, 3000.0)
    for n in range(12, 22):
        summary.feed(_use(f"a{n}", "Agent", {"description": f"agent {n}"}), 4000.0 + n)
    agents = _live(summary)["subagents"]
    assert [agent["description"] for agent in agents] == [f"agent {n}" for n in range(14, 22)]
    assert {agent["status"] for agent in agents} == {"running"}


def test_a_task_that_is_not_a_subagent_is_not_counted() -> None:
    summary = _feed(
        Summary("claude", Scrubber({})),
        _use("b1", "Bash", {"command": "sleep 9", "run_in_background": True}),
        {"type": "system", "subtype": "task_started", "task_id": "b1", "tool_use_id": "b1",
         "description": "sleep", "task_type": "local_bash"},
        {"type": "system", "subtype": "task_notification", "task_id": "b1", "tool_use_id": "b1", "status": "completed"},
    )
    assert _live(summary)["subagents"] == []


def test_the_old_task_tool_finishes_on_its_result() -> None:
    summary = _feed(
        Summary("claude", Scrubber({})),
        _use("t1", "Task", {"description": "Legacy helper", "subagent_type": "general-purpose", "prompt": "PROMPT-SENTINEL"}),
        _result("t1", "RESULT-SENTINEL"),
    )
    assert _live(summary)["subagents"] == [{"kind": "general-purpose", "description": "Legacy helper",
                                            "status": "finished", "started_at": 1000.0, "finished_at": 1001.0}]


@pytest.mark.parametrize(
    "data",
    [
        {"todos": "not a list"},
        {"todos": [None, 3, {"content": ["x"], "status": "pending"}, {"content": "x", "status": {"a": 1}},
                   {"content": "y", "status": "weird"}]},
        {"todos": {"content": "x"}},
    ],
)
def test_a_malformed_todo_input_is_dropped_and_counters_still_update(data: dict) -> None:
    summary = Summary("claude", Scrubber({}))
    summary.feed(_use("t0", "TodoWrite", _todos(("Kept", "in_progress"))), 999.0)
    _feed(
        summary,
        _use("t1", "TodoWrite", data),
        _use("u1", "TaskUpdate", {"taskId": ["1"], "status": "completed"}),
        _use("a1", "Agent", {"description": 7, "subagent_type": None, "run_in_background": "yes"}),
        {"type": "system", "subtype": "task_notification", "tool_use_id": ["a1"], "task_id": {}, "status": ["completed"]},
        {"type": "item.updated", "item": {"id": "i", "type": "todo_list", "items": "nope"}},
        {"type": "item.updated", "item": {"id": "j", "type": "todo_list", "items": [None, {"text": 4}]}},
    )
    assert summary.events == 7
    assert json.loads(summary.row()["tool_calls"]) == {"TodoWrite": 2, "TaskUpdate": 1, "Agent": 1}
    live = _live(summary)
    assert live["subagents"] == [{"kind": "general-purpose", "description": "", "status": "running",
                                  "started_at": 1002.0, "finished_at": None}]


def test_counters_only_setting_reads_counters_and_stores_no_text() -> None:
    assert telemetry.text_enabled({}) and telemetry.text_enabled({"ORCH_TELEMETRY": "on"})
    assert not telemetry.text_enabled({"ORCH_TELEMETRY": " Counters "})
    assert telemetry.enabled({"ORCH_TELEMETRY": "counters"})


@pytest.mark.parametrize("name, harness", LIVE_FIXTURES)
def test_counters_only_stores_no_live_run_text(tmp_path: Path, name: str, harness: str) -> None:
    rows = {}
    for mode in ("full", "counters"):
        session = start_session(
            connect=lambda: sqlite3.connect(tmp_path / f"{mode}.db"),
            database=tmp_path / f"{mode}.db",
            job_id="job-abc",
            bot=f"{harness}-bot",
            repo="acme/widgets",
            command=[harness, "-p"] if harness == "claude" else [harness, "exec", "-"],
            log=lambda line: None,
            environ={"ORCH_TELEMETRY": mode, "ORCH_JOB_STATE_DIR": str(tmp_path / mode)},
            stdout=io.BytesIO(),
        )
        assert session.summary.live is None if mode == "counters" else session.summary.live is not None
        stream = "".join(line + "\n" for line in fixture_lines(name)).encode()
        session.consume(io.BytesIO(stream))
        session.finish(0)
        rows[mode] = _row(tmp_path / f"{mode}.db")
    full, counters = rows["full"], rows["counters"]
    assert all(counters[name] is None for name in LIVE_RUN)
    assert all(full[name] is not None for name in LIVE_RUN)
    same = [name for name in telemetry.COLUMNS if name not in LIVE_RUN + ("started_at", "updated_at",
                                                                          "finished_at", "elapsed_seconds", "last_event_at", "steps")]
    assert {name: counters[name] for name in same} == {name: full[name] for name in same}
    texts = {item["text"] for item in json.loads(full["plan"])}
    texts |= {agent["description"] for agent in json.loads(full["subagents"])}
    texts |= {full["current_task"], full["last_finished"]}
    texts.discard("")
    stored = json.dumps(counters)
    assert texts and not [text for text in texts if text in stored]


def test_counters_only_still_adds_the_stream_flags(tmp_path: Path) -> None:
    session = start_session(
        connect=lambda: sqlite3.connect(tmp_path / "orch.db"),
        database=tmp_path / "orch.db",
        job_id="j1",
        bot="claude-bot",
        repo="acme/widgets",
        command=["claude", "-p"],
        log=lambda line: None,
        environ={"ORCH_TELEMETRY": "counters"},
    )
    assert session.flags.argv == ["claude", "--output-format", "stream-json", "--verbose", "-p"]


def test_an_old_table_gains_the_live_run_columns(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    with sqlite3.connect(db) as connection:
        connection.execute(OLD_DDL)
        connection.execute(
            "INSERT INTO job_telemetry VALUES ('old', 'b', 'claude', 'r', 1, 2, NULL, NULL, 1, 0, 0, '{}',"
            " 0, 0, 0, NULL, NULL, 0, 0, 0, NULL, NULL, NULL, '[]')"
        )
    session, _, _, messages = _session(tmp_path)
    session.consume(io.BytesIO("".join(line + "\n" for line in fixture_lines("claude_tasks_v2_stream.jsonl")).encode()))
    session.flush()
    assert messages == []
    row = _row(db)
    assert json.loads(row["plan"])[1] == {"text": "Parse the stream", "status": "in_progress"}
    assert row["current_task"] == "Parsing the stream" and json.loads(row["subagents"]) == []
    old = _row(db, "old")
    assert all(old[name] is None for name in LIVE_RUN)  # NULL: an older runner recorded nothing
    assert old["suite_wait_seconds"] is None


def test_two_runners_migrating_at_once_both_write(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    with sqlite3.connect(db) as connection:
        connection.execute(OLD_DDL)
    first, second = sqlite3.connect(db), sqlite3.connect(db)
    try:
        stale = {row[1] for row in second.execute("PRAGMA table_info(job_telemetry)")}
        telemetry.ensure_table(first)
        first.commit()

        class Stale:  # the loser read the old columns before the winner added them
            def execute(self, sql, *args):
                if sql.startswith("PRAGMA"):
                    return [(0, name) for name in stale]
                return second.execute(sql, *args)

        telemetry.ensure_table(Stale())
        second.commit()
    finally:
        first.close()
        second.close()
    for job in ("one", "two"):
        session, _, _, messages = _session(tmp_path)
        session.job_id = job
        session.consume(io.BytesIO(b'{"type":"turn.started"}\n'))
        session.finish(0)
        assert messages == []
        assert json.loads(_row(db, job)["plan"]) == []
    with sqlite3.connect(db) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(job_telemetry)")]
    assert columns == list(telemetry.COLUMNS)


def test_cli_job_run_writes_the_live_run_and_counters_only_writes_none(tmp_path: Path) -> None:
    rows = {}
    for mode in ("on", "counters"):
        root = tmp_path / mode
        root.mkdir()
        _, job = _running_job(root)
        with sqlite3.connect(root / "orch.db") as connection:
            connection.execute(OLD_DDL)  # an orch.db an older runner made
        claude = _fake_claude(root, "claude_subagents_stream.jsonl")
        done = _orch(
            "job", "run", "claude-bot", job.id, "--machines", str(LOCAL_BOX_MACHINES), "--db", str(root / "orch.db"), "--interval", "0.2",
            "--", str(claude), "-p", "--dangerously-skip-permissions",
            env={"ORCH_JOB_STATE_DIR": str(root / "state"), "ORCH_TELEMETRY": mode},
        )
        assert done.returncode == 0, done.stderr
        assert [line for line in done.stderr.splitlines() if "telemetry" in line] == [
            "orch job run: telemetry: stream flags added (claude)"
        ]
        rows[mode] = _row(root / "orch.db", job.id)
    assert rows["on"]["current_task"] == "Pushing with GH_TOKEN=[redacted-token]"
    assert len(json.loads(rows["on"]["subagents"])) == 4
    no_secret(rows["on"])
    assert all(rows["counters"][name] is None for name in LIVE_RUN)
    assert rows["counters"]["events"] == rows["on"]["events"] == 31
    assert rows["on"]["skill_reads"] == rows["counters"]["skill_reads"] == "[]"



@pytest.mark.parametrize(
    "name, root, expected",
    [
        (
            "claude_skill_reads_stream.jsonl",
            "/home/claude-bot/factory/skills",
            [
                "cursor-team-kit/deslop/SKILL.md",
                "factory/factory-builder/SKILL.md",
                "pstack/agents/comment-sicko.md",
                "pstack/agents/poteto-agent.md",
                "pstack/docs/harness/claude-code.md",
                "pstack/skills/figure-it-out/SKILL.md",
                "pstack/skills/no-comments/SKILL.md",
                "pstack/skills/poteto-mode/SKILL.md",
                "pstack/skills/poteto-mode/playbooks/feature.md",
                "pstack/skills/poteto-mode/playbooks/opening-a-pr.md",
                "pstack/skills/principle-model-the-domain/SKILL.md",
                "pstack/skills/principle-prove-it-works/SKILL.md",
            ],
        ),
        (
            "codex_skill_reads_exec.jsonl",
            "/Users/codex-bot/factory/skills",
            [
                "factory/factory-reviewer/SKILL.md",
                "factory/harness/codex.md",
                "pstack/skills/arena/SKILL.md",
                "pstack/skills/blast-radius/SKILL.md",
                "pstack/skills/in-your-own-words/SKILL.md",
                "pstack/skills/interrogate/SKILL.md",
                "pstack/skills/poteto-mode/SKILL.md",
                "pstack/skills/principle-prove-it-works/SKILL.md",
                "pstack/skills/unslop/SKILL.md",
            ],
        ),
    ],
)
def test_a_recorded_session_stores_the_pinned_skill_files_it_read(
    tmp_path: Path, name: str, root: str, expected: list[str]
) -> None:
    session, db, _, messages = _session(tmp_path, skills=SkillReads.pinned(root, load_pin(ROOT)))
    session.consume(io.BytesIO("".join(line + "\n" for line in fixture_lines(name)).encode()))
    session.finish(0)
    assert json.loads(_row(db)["skill_reads"]) == expected
    assert messages == []


SKILLS = "/r/factory/skills"
LOCKED = frozenset({"pstack/skills/arena/SKILL.md", "pstack/skills/unslop/SKILL.md", "factory/factory-builder/SKILL.md"})


def _exec(command: str) -> dict:
    return {"type": "item.started", "item": {"id": "item_1", "type": "command_execution", "command": command}}


def _exec_done(exit_code: int, output: str = "") -> dict:
    status = "completed" if exit_code == 0 else "failed"
    item = {"id": "item_1", "type": "command_execution", "exit_code": exit_code, "status": status}
    item["aggregated_output"] = output
    return {"type": "item.completed", "item": item}


def _succeeded(event: dict) -> dict:
    if event["type"] == "item.started":
        return _exec_done(0)
    return _result(event["message"]["content"][0]["id"])


@pytest.mark.parametrize(
    "event, expected",
    [
        (_use("t1", "Read", {"file_path": f"{SKILLS}/pstack/skills/arena/SKILL.md"}), ["pstack/skills/arena/SKILL.md"]),
        (_use("t1", "Read", {"file_path": "/elsewhere/skills/pstack/skills/arena/SKILL.md"}), []),
        (_use("t1", "Read", {"file_path": f"{SKILLS}/pstack/skills/arena/notes.md"}), []),
        (_use("t1", "Skill", {"skill": "pstack:unslop"}), ["pstack/skills/unslop/SKILL.md"]),
        (_use("t1", "Skill", {"skill": "claude-api"}), []),
        (_use("t1", "Bash", {"command": f"test -f {SKILLS}/pstack/skills/arena/SKILL.md && ls {SKILLS}/pstack"}), []),
        (_use("t1", "Bash", {"command": f"grep -n x {SKILLS}/pstack/skills/arena/SKILL.md | wc -l"}), []),
        (_use("t1", "Bash", {"command": f"sed -n 1,9p {SKILLS}/pstack/skills/arena/SKILL.md"}), ["pstack/skills/arena/SKILL.md"]),
        (_use("t1", "Bash", {"command": f"cd {SKILLS}/pstack/skills/unslop; head -n 3 ../arena/SKILL.md"}), ["pstack/skills/arena/SKILL.md"]),
        (_use("t1", "Bash", {"command": "cat pstack/skills/arena/SKILL.md"}), []),
        (_use("t1", "Bash", {"command": f"cat '{SKILLS}/pstack/skills/arena/SKILL.md"}), []),
        (_exec(f"/bin/zsh -lc 'cd {SKILLS}\ncat factory/factory-builder/SKILL.md > /tmp/out'"), ["factory/factory-builder/SKILL.md"]),
        (_exec(f"bash -lc 'cd $HOME && cat {SKILLS}/pstack/skills/*/SKILL.md'"), []),
    ],
    ids=[
        "read", "read-outside-root", "read-not-locked", "skill-tool", "skill-not-locked", "test-f-probe",
        "grep-is-no-read", "sed", "cd-base", "relative-without-cd", "unbalanced-quote",
        "codex-multi-line", "codex-glob",
    ],
)
def test_only_reads_of_locked_files_under_the_skills_tree_count(event: dict, expected: list[str]) -> None:
    summary = _feed(Summary("claude", Scrubber({}), skills=SkillReads(SKILLS, LOCKED)), event, _succeeded(event))
    assert json.loads(summary.row()["skill_reads"]) == expected


ARENA = f"{SKILLS}/pstack/skills/arena/SKILL.md"


@pytest.mark.parametrize(
    "harness, events, expected",
    [
        ("claude", [_use("t1", "Skill", {"skill": "pstack:unslop"})], []),
        ("claude", [_use("t1", "Skill", {"skill": "pstack:unslop"}), _result("t1", "disable-model-invocation", error=True)], []),
        ("claude", [_use("t1", "Read", {"file_path": ARENA}), _result("t1", "File does not exist.", error=True)], []),
        ("claude", [_use("t1", "Read", {"file_path": ARENA}), _result("t1", "EACCES", error=True)], []),
        (
            "claude",
            [_use("t1", "Bash", {"command": f"cat {ARENA}"}), _result("t1", f"Exit code 1\ncat: {ARENA}: No such file or directory", error=True)],
            [],
        ),
        (
            "claude",
            [_use("t1", "Bash", {"command": f"cat {ARENA}"}), _result("t1", f"Permission to use Bash with command cat {ARENA} has been denied.", error=True)],
            [],
        ),
        (
            "claude",
            [_use("t1", "Bash", {"command": f"cat {ARENA} /tmp/gone.md"}), _result("t1", "# Arena\ncat: /tmp/gone.md: No such file or directory", error=True)],
            ["pstack/skills/arena/SKILL.md"],
        ),
        (
            "claude",
            [
                _use("t1", "Bash", {"command": f"cd {SKILLS} && cat pstack/skills/arena/SKILL.md pstack/skills/unslop/SKILL.md"}),
                _result("t1", "# Arena\ncat: pstack/skills/unslop/SKILL.md: No such file or directory", error=True),
            ],
            ["pstack/skills/arena/SKILL.md"],
        ),
        ("claude", [_use("t1", "Read", {"file_path": ARENA}), _result("t2")], []),
        ("codex", [_exec(f"cat {ARENA}")], []),
        ("codex", [_exec(f"cat {ARENA}"), _exec_done(1)], []),
        ("codex", [_exec(f"cat {ARENA} /tmp/gone.md"), _exec_done(1, "# Arena\ncat: /tmp/gone.md: No such file or directory\n")], ["pstack/skills/arena/SKILL.md"]),
        (
            "claude",
            [
                _use("t1", "Read", {"file_path": ARENA}), _result("t1"),
                _use("t2", "Bash", {"command": f"cat {ARENA}"}), _result("t2", "Exit code 1", error=True),
            ],
            ["pstack/skills/arena/SKILL.md"],
        ),
    ],
    ids=[
        "claude-no-result", "claude-skill-refused", "claude-read-failed", "claude-read-error-unnamed",
        "claude-bash-failed", "claude-bash-denied", "claude-bash-other-operand-failed", "claude-bash-relative-operand-failed",
        "claude-other-call-result", "codex-not-completed", "codex-failed", "codex-other-operand-failed", "earlier-success-kept",
    ],
)
def test_a_skill_file_counts_as_read_unless_its_call_failed_on_it(
    harness: str, events: list[dict], expected: list[str]
) -> None:
    summary = _feed(Summary(harness, Scrubber({}), skills=SkillReads(SKILLS, LOCKED)), *events)
    assert json.loads(summary.row()["skill_reads"]) == expected


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_a_shell_read_that_fails_on_a_later_operand_keeps_the_file_it_printed(tmp_path: Path, harness: str) -> None:
    root = tmp_path / "skills"
    router = root / "factory" / "factory-builder" / "SKILL.md"
    router.parent.mkdir(parents=True)
    router.write_text("# Factory builder\n", encoding="utf-8")
    command = f"cat {shlex.quote(str(router))} {shlex.quote(str(tmp_path / 'missing.md'))}"
    run = subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=False)
    assert run.returncode == 1 and run.stdout == router.read_text(encoding="utf-8")
    if harness == "claude":
        events = [_use("t1", "Bash", {"command": command}), _result("t1", run.stdout + run.stderr, error=True)]
    else:
        events = [_exec(command), _exec_done(run.returncode, run.stdout + run.stderr)]
    summary = _feed(Summary(harness, Scrubber({}), skills=SkillReads(str(root), LOCKED)), *events)
    assert json.loads(summary.row()["skill_reads"]) == ["factory/factory-builder/SKILL.md"]


BSD_SED = """sed() {
    local args=() arg
    for arg; do
        case $arg in -l) ;; -nl) args+=(-n) ;; *) args+=("$arg") ;; esac
    done
    command sed "${args[@]}"
}
"""


@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.parametrize(
    "script, returncode, expected",
    [
        ("cat {missing} && cat {skill}", 1, []),
        ("cat {skill} && cat {missing}", 1, ["factory/factory-builder/SKILL.md"]),
        ("cat {skill}; cat {missing}", 1, ["factory/factory-builder/SKILL.md"]),
        ("cat {missing} || cat {skill}", 0, ["factory/factory-builder/SKILL.md"]),
        ("head -n 3 {guide} && cat {skill}; cat {missing}", 1, ["factory/factory-builder/SKILL.md"]),
        ("sed -n 3p {guide} && cat {skill}; cat {missing}", 1, ["factory/factory-builder/SKILL.md"]),
        ("head -n 3 {missing} && cat {skill}", 1, []),
        ("head -n 3 {missing} || cat {skill}", 0, ["factory/factory-builder/SKILL.md"]),
        ("sed -nl '1,3p' {skill}; cat {missing}", 1, ["factory/factory-builder/SKILL.md"]),
        ("sed -n -l '1,3p' {skill}; cat {missing}", 1, ["factory/factory-builder/SKILL.md"]),
        ("sed -n -l 80 '1,3p' {skill}; cat {missing}", 1, ["factory/factory-builder/SKILL.md"]),
        ("sed -nl80 '1,3p' {skill}; cat {missing}", 1, ["factory/factory-builder/SKILL.md"]),
        ("sed -n --line-length 80 '1,3p' {skill}; cat {missing}", 1, ["factory/factory-builder/SKILL.md"]),
        ("test -f {missing} || cat {skill}; cat {missing}", 1, ["factory/factory-builder/SKILL.md"]),
        ('bash -c "cat {missing} || cat {guide}" && cat {skill}; cat {missing}', 1, ["factory/factory-builder/SKILL.md"]),
        ('bash -c "cat {missing}; cat {guide}" && cat {skill}; cat {missing}', 1, ["factory/factory-builder/SKILL.md"]),
        ('bash -c "cat {guide} && cat {missing}" && cat {skill}', 1, []),
        ('bash -c "cat {missing};" && cat {skill}; cat {missing}', 1, []),
        ('bash -c "cat {missing}\n" && cat {skill}; cat {missing}', 1, []),
        ('bash -c "cat {missing}; # note" && cat {skill}; cat {missing}', 1, []),
        ("{{ cat {missing}; }} && cat {skill}; cat {missing}", 1, []),
    ],
    ids=[
        "and-after-failure", "and-before-failure", "semicolon", "or-after-failure", "option-value-in-output",
        "sed-program-in-output", "head-failure", "or-after-head-failure", "bsd-sed-joined-l", "bsd-sed-spaced-l",
        "gnu-sed-spaced-l", "gnu-sed-joined-l", "gnu-sed-line-length", "or-after-failed-probe",
        "and-after-recovered-shell", "and-after-shell-recovered-by-semicolon", "and-after-failed-shell",
        "and-after-failed-shell-ending-in-semicolon", "and-after-failed-shell-ending-in-newline",
        "and-after-failed-shell-ending-in-comment", "and-after-failed-brace-group",
    ],
)
def test_a_shell_read_counts_only_if_its_place_in_the_chain_ran(
    tmp_path: Path, harness: str, script: str, returncode: int, expected: list[str]
) -> None:
    root = tmp_path / "skills"
    router = root / "factory" / "factory-builder" / "SKILL.md"
    router.parent.mkdir(parents=True)
    router.write_text("# Factory builder\n", encoding="utf-8")
    guide = tmp_path / "guide.md"
    guide.write_text("# Guide\n1. Read\n3. Run 3p\n", encoding="utf-8")
    command = script.format(
        skill=shlex.quote(str(router)), missing=shlex.quote(str(tmp_path / "missing.md")), guide=shlex.quote(str(guide))
    )
    run = subprocess.run(["bash", "-c", BSD_SED + command], capture_output=True, text=True, check=False)
    assert run.returncode == returncode
    if harness == "claude":
        events = [_use("t1", "Bash", {"command": command}), _result("t1", run.stdout + run.stderr, error=returncode != 0)]
    else:
        events = [_exec(command), _exec_done(run.returncode, run.stdout + run.stderr)]
    summary = _feed(Summary(harness, Scrubber({}), skills=SkillReads(str(root), LOCKED)), *events)
    assert json.loads(summary.row()["skill_reads"]) == expected


def test_a_refused_skill_tool_call_records_no_read(tmp_path: Path) -> None:
    skills = SkillReads("/tmp/skill-probe/skills", frozenset({"pstack/skills/poteto-mode/SKILL.md"}))
    session, db, _, _ = _session(tmp_path, skills=skills)
    lines = (ROOT / "tests" / "fixtures" / "skill_probe" / "claude_skill_refused.jsonl").read_text(encoding="utf-8")
    session.consume(io.BytesIO(lines.encode()))
    session.finish(0)
    assert json.loads(_row(db)["skill_reads"]) == []


def test_skill_reads_are_null_without_a_tree_and_empty_when_nothing_was_read(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "orch.db"
    with sqlite3.connect(db) as connection:
        connection.execute(OLD_DDL)
    messages: list[str] = []
    kwargs = dict(
        connect=lambda: sqlite3.connect(db),
        database=db,
        bot="claude-bot",
        repo="acme/widgets",
        command=["claude", "-p"],
        log=messages.append,
        environ={"ORCH_TELEMETRY": "counters", "ORCH_JOB_STATE_DIR": str(tmp_path / "state")},
        stdout=io.BytesIO(),
    )
    read = [_use("t1", "Read", {"file_path": f"{SKILLS}/pstack/skills/poteto-mode/SKILL.md"}), _result("t1")]
    for job, root, events in (("none", None, read), ("tree", SKILLS, read), ("nothing", SKILLS, [{"type": "turn.started"}])):
        session = start_session(job_id=job, skills_root=root, **kwargs)
        session.consume(io.BytesIO("".join(json.dumps(event) + "\n" for event in events).encode()))
        session.finish(0)
    monkeypatch.setattr(telemetry, "FACTORY_CHECKOUT", tmp_path)
    session = start_session(job_id="no-lock", skills_root=SKILLS, **kwargs)
    session.finish(0)
    assert _row(db, "none")["skill_reads"] is None
    assert _row(db, "tree")["skill_reads"] == '["pstack/skills/poteto-mode/SKILL.md"]'
    assert _row(db, "nothing")["skill_reads"] == "[]"
    assert _row(db, "no-lock")["skill_reads"] is None
    assert [line for line in messages if "skill" in line] == [
        "telemetry: skill reads not recorded (skills.lock: FileNotFoundError)"
    ]


SLOT_RUN = "bin/suite-slot python -m pytest -q"


def _slot_output(seconds: int) -> str:
    return f"ORCH-SUITE-SLOT: waited {seconds}s, running\n...\n12 passed in 3.0s\n"


@pytest.mark.parametrize(
    "harness, events, expected",
    [
        ("claude", [_use("t1", "Bash", {"command": SLOT_RUN}), _result("t1", _slot_output(12))], 12),
        ("codex", [_exec(SLOT_RUN), _exec_done(0, _slot_output(12))], 12),
        (
            "claude",
            [
                _use("t1", "Bash", {"command": SLOT_RUN}), _result("t1", _slot_output(12)),
                _use("t2", "Bash", {"command": SLOT_RUN}), _result("t2", _slot_output(30)),
            ],
            42,
        ),
        ("claude", [_use("t1", "Bash", {"command": "python -m pytest -q"}), _result("t1", _slot_output(12))], None),
        ("claude", [_use("t1", "Bash", {"command": SLOT_RUN})], None),
        ("claude", [_use("t1", "Bash", {"command": "ls"}), _result("t1", "a\nb\n")], None),
    ],
    ids=["claude", "codex", "two-calls-sum", "no-helper", "unfinished", "no-shell-output"],
)
def test_suite_wait_seconds_sums_the_helper_s_reported_waits(harness: str, events: list[dict], expected) -> None:
    assert _feed(Summary(harness, Scrubber({})), *events).row()["suite_wait_seconds"] == expected


def test_suite_wait_seconds_reaches_the_table_and_stays_null_without_the_helper(tmp_path: Path) -> None:
    for job, events in (
        ("waited", [_use("t1", "Bash", {"command": SLOT_RUN}), _result("t1", _slot_output(37))]),
        ("plain", [_use("t1", "Bash", {"command": "ls"}), _result("t1", "a\n")]),
    ):
        session, db, _, messages = _session(tmp_path)
        session.job_id = job
        session.consume(io.BytesIO("".join(json.dumps(event) + "\n" for event in events).encode()))
        session.finish(0)
        assert messages == []
    assert _row(db, "waited")["suite_wait_seconds"] == 37
    assert _row(db, "plain")["suite_wait_seconds"] is None
