"""Subscription usage readings (issue #151): parse, capture, store, route, snapshot.

Sources (Max, PR #234, 2026-11-14): Claude Code's `get_usage` control reply,
probed once at each job's start; Codex's own session rollout `token_count`
rate limits, found by the job's `thread.started` thread id. The fixtures
(tests/fixtures/usage/README.md) follow the real shapes with test values.
"""

from __future__ import annotations

import ast
import builtins
import io
import itertools
import json
import math
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lib import codex_rollout, telemetry, usage_probe, usage_readings
from lib.dashboard import collect_snapshot
from lib.dashboard_usage import read_usage
from lib.job_token import PROOF_ENV
from lib.machines_yml import Device, MachinesConfig
from lib.queue import JobRequest, SQLiteJobQueue
from lib.remote import TAG_ENV, Exchanged, RemoteTarget, SshTransport, ssh_argv
from lib.telemetry import Scrubber, Session, harness_argv, stream_flags
from lib.usage import reserve_check
from lib.usage_readings import (
    Reading,
    TelemetryUsage,
    parse_codex,
    parse_get_usage,
    parse_rate_limit_event,
)
from tests import child_reaper, test_telemetry_argv
from tests.pool_helpers import CLAIM_PROOF, LOCAL_BOX_MACHINES
from tests.test_dashboard import make_db
from tests.test_telemetry import _orch, _running_job
from tests.test_telemetry_argv import CLAUDE_BOT

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "usage"
GET_USAGE = (FIXTURES / "claude-2.1.322-get_usage.jsonl").read_bytes()
UNAVAILABLE = (FIXTURES / "claude-2.1.322-get_usage-unavailable.jsonl").read_bytes()
CODEX_EXEC = (FIXTURES / "codex-exec-json.jsonl").read_bytes()
ROLLOUT = (FIXTURES / "codex-rollout.jsonl").read_bytes()
THREAD = "00000000-0000-4000-8000-000000000151"
CAPTURED = 1794673532.0  # 2026-11-14T16:25:32Z
EMITTED = CAPTURED - 2  # the rollout fixture's last token_count, 2026-11-14T16:25:30Z
FIVE_HOUR_RESET = 1794684332  # 2026-11-14T19:25:32Z
SEVEN_DAY_RESET = 1795204800  # 2026-11-20T20:00:00Z
WRAPPER = [
    "/home/codex-bot/.local/bin/codex-review-harness", "exec", "--skip-git-repo-check",
    "--sandbox", "workspace-write", "-c", "sandbox_workspace_write.network_access=true",
    "-m", "gpt-6.1-sol", "-",
]
BOX = MachinesConfig(
    (Device("box", 3, {"claude": "claude-bot", "codex": "codex-bot", "cursor": "cursor-bot"}, local=True),)
)


def reply(rate_limits: object, *, available: object = True, subtype: str = "success",
          request_id: str = "orch-usage") -> bytes:
    body = {"type": "control_response", "response": {
        "subtype": subtype, "request_id": request_id,
        "response": {"rate_limits_available": available, "rate_limits": rate_limits}}}
    return json.dumps(body).encode() + b"\n"


def codex_home(tmp_path: Path, rollout: bytes = ROLLOUT, thread: str = THREAD) -> Path:
    home = tmp_path / "codex-home"
    day = home / "sessions" / "2026" / "11" / "14"
    day.mkdir(parents=True)
    (day / f"rollout-2026-11-14T16-20-00-{thread}.jsonl").write_bytes(rollout)
    (home / "auth.json").write_text('{"TEST": "never opened"}', encoding="utf-8")
    return home


# --- Claude: the get_usage reply -------------------------------------------------------


def test_the_get_usage_reply_gives_both_windows_as_fractions() -> None:
    assert parse_get_usage(GET_USAGE, CAPTURED) == [
        Reading("five_hour", 0.125, FIVE_HOUR_RESET, CAPTURED, 0, CAPTURED),
        Reading("seven_day", 0.345, SEVEN_DAY_RESET, CAPTURED, 0, CAPTURED),
    ]


OBSERVED_UNAVAILABLE = (
    b'{"type":"control_response","response":{"subtype":"success","request_id":"orch-usage",'
    b'"response":{"rate_limits_available":false,"rate_limits":{}}}}\n'
)


@pytest.mark.parametrize(
    "out", [UNAVAILABLE, OBSERVED_UNAVAILABLE,
            reply({"five_hour": {"utilization": 1, "resets_at": "2026-11-14T19:25:32+00:00"}}, available="true")])
def test_a_login_without_limits_gives_no_reading(out: bytes) -> None:
    assert parse_get_usage(out, CAPTURED) == [
        Reading("five_hour", None, None, CAPTURED, 0, CAPTURED),
        Reading("seven_day", None, None, CAPTURED, 0, CAPTURED)]


@pytest.mark.parametrize("five_hour", [None, "missing", {"utilization": 3}])
def test_a_null_or_missing_window_is_no_reading_never_zero_nor_absent(five_hour: object) -> None:
    limits = {"seven_day": {"utilization": 24, "resets_at": "2026-11-20T20:00:00+00:00"}}
    if five_hour != "missing":
        limits["five_hour"] = five_hour
    assert parse_get_usage(reply(limits), CAPTURED) == [
        Reading("five_hour", None, None, CAPTURED, 0, CAPTURED),
        Reading("seven_day", 0.24, SEVEN_DAY_RESET, CAPTURED, 0, CAPTURED)]


@pytest.mark.parametrize(
    "out",
    [
        reply(None, subtype="error"),
        reply({"five_hour": {"utilization": 1, "resets_at": "2026-11-14T19:25:32+00:00"}}, request_id="other"),
        b"not json\n",
        b"",
    ],
)
def test_an_error_another_reply_or_no_reply_gives_no_reading(out: bytes) -> None:
    assert parse_get_usage(out, CAPTURED) == []


@pytest.mark.parametrize("utilization", [True, math.nan, 150, -1, "12"])
def test_an_invalid_utilization_is_kept_as_unknown(utilization: object) -> None:
    out = reply({"five_hour": {"utilization": utilization, "resets_at": "2026-11-14T19:25:32+00:00"}})
    reading = parse_get_usage(out, CAPTURED)[0]
    assert reading.window == "five_hour" and reading.used is None


@pytest.mark.parametrize("resets_at", ["2026-11-14T19:25:32", "soon", "2027-06-01T00:00:00+00:00", 1794684332, None])
def test_a_bad_reset_is_no_reading(resets_at: object) -> None:
    out = reply({"five_hour": {"utilization": 10, "resets_at": resets_at}})
    assert parse_get_usage(out, CAPTURED)[0] == Reading("five_hour", None, None, CAPTURED, 0, CAPTURED)


def rate_limit_event(windows: object) -> dict:
    return {"type": "rate_limit_event", "uuid": "u", "session_id": "s", "rate_limit_info": {
        "status": "allowed", "resetsAt": SEVEN_DAY_RESET, "rateLimitType": "seven_day", "utilization": 0.82,
        "unifiedWindows": windows}}


def test_a_rate_limit_event_gives_its_unified_windows_as_fractions() -> None:
    seven_day = {"utilization": 0.82, "resetsAt": SEVEN_DAY_RESET, "status": "allowed"}
    assert parse_rate_limit_event(rate_limit_event({"seven_day": seven_day}), CAPTURED) == [
        Reading("seven_day", 0.82, SEVEN_DAY_RESET, CAPTURED)]
    both = {"seven_day": seven_day, "five_hour": {"utilization": 0, "resetsAt": FIVE_HOUR_RESET}}
    assert parse_rate_limit_event(rate_limit_event(both), CAPTURED) == [
        Reading("five_hour", 0.0, FIVE_HOUR_RESET, CAPTURED), Reading("seven_day", 0.82, SEVEN_DAY_RESET, CAPTURED)]


@pytest.mark.parametrize(
    "five_hour",
    [
        {"utilization": 1.5, "resetsAt": FIVE_HOUR_RESET},
        {"utilization": -0.1, "resetsAt": FIVE_HOUR_RESET},
        {"utilization": True, "resetsAt": FIVE_HOUR_RESET},
        {"utilization": math.nan, "resetsAt": FIVE_HOUR_RESET},
        {"utilization": "0.5", "resetsAt": FIVE_HOUR_RESET},
        {"utilization": None, "resetsAt": FIVE_HOUR_RESET},
        {"utilization": 0.5, "resetsAt": CAPTURED + usage_readings.RESET_HORIZON_SECONDS + 1},
        {"utilization": 0.5, "resetsAt": "2026-11-14T19:25:32+00:00"},
        {"utilization": 0.5, "resetsAt": FIVE_HOUR_RESET + 0.5},
        {"utilization": 0.5},
        {"utilization": 10**400, "resetsAt": FIVE_HOUR_RESET},
        {"utilization": 0.5, "resetsAt": 10**400},
        None,
    ],
)
def test_an_invalid_event_window_gives_nothing_so_it_never_clears_a_reading(five_hour: object) -> None:
    seven_day = {"utilization": 0.82, "resetsAt": SEVEN_DAY_RESET}
    assert parse_rate_limit_event(rate_limit_event({"five_hour": five_hour, "seven_day": seven_day}), CAPTURED) == [
        Reading("seven_day", 0.82, SEVEN_DAY_RESET, CAPTURED)]


@pytest.mark.parametrize(
    "event",
    [
        {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "resetsAt": SEVEN_DAY_RESET,
                                                         "rateLimitType": "seven_day", "utilization": 0.82}},
        rate_limit_event(None),
        {"type": "rate_limit_event", "rate_limit_info": "x"},
        {"type": "rate_limit_event"},
        {**rate_limit_event({"seven_day": {"utilization": 0.82, "resetsAt": SEVEN_DAY_RESET}}), "type": "result"},
        [],
    ],
)
def test_an_event_without_unified_windows_gives_no_reading(event: object) -> None:
    assert parse_rate_limit_event(event, CAPTURED) == []


def test_a_streamed_rate_limit_event_is_stored_on_flush(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    session = _session(db)
    event = rate_limit_event({"seven_day": {"utilization": 0.82, "resetsAt": SEVEN_DAY_RESET}})
    session.consume(io.BytesIO(b'{"type": "rate_limit_event", "rate_limit_info": 7}\n'
                               + json.dumps(event).encode() + b"\n"))
    session.flush()
    assert _stored(db) == [("claude", "claude-bot", "box", "seven_day", 0.82, SEVEN_DAY_RESET, CAPTURED, "job-1")]


# --- Codex: the session rollout ----------------------------------------------------------


def test_the_rollout_reader_returns_only_the_last_rate_limit_and_token_numbers(tmp_path: Path) -> None:
    found = codex_rollout.latest(str(codex_home(tmp_path)), THREAD)
    assert found == {
        "rate_limits": {
            "primary": {"used_percent": 12.5, "window_minutes": 300, "resets_at": FIVE_HOUR_RESET},
            "secondary": {"used_percent": 34.5, "window_minutes": 10080, "resets_in_seconds": 531270},
        },
        "totals": {THREAD: LAST_TOTAL},
        "timestamp": "2026-11-14T16:25:30.000Z",
    }
    assert "TEST VALUE" not in json.dumps(found)


def test_codex_windows_are_mapped_by_minutes_and_reset_in_seconds_counts_from_the_event(tmp_path: Path) -> None:
    found = codex_rollout.latest(str(codex_home(tmp_path)), THREAD)
    assert parse_codex(found, CAPTURED) == [
        Reading("five_hour", 0.125, FIVE_HOUR_RESET, EMITTED, 0, EMITTED),
        Reading("seven_day", 0.345, SEVEN_DAY_RESET, EMITTED, 0, EMITTED),
    ]
    swapped = {"rate_limits": {"primary": {"used_percent": 34.5, "window_minutes": 10080, "resets_at": SEVEN_DAY_RESET},
                               "secondary": {"used_percent": 1, "window_minutes": 60, "resets_at": FIVE_HOUR_RESET}},
               "timestamp": "2026-11-14T16:25:30Z"}
    assert parse_codex(swapped, CAPTURED) == [
        Reading("five_hour", None, None, EMITTED, 1, EMITTED),
        Reading("seven_day", 0.345, SEVEN_DAY_RESET, EMITTED, 0, EMITTED)]


@pytest.mark.parametrize("stamp", [None, "soon", "2026-11-14T16:25:30", "2026-11-14T16:40:00Z"])
def test_a_codex_reading_without_a_usable_event_time_is_no_observation(stamp: object) -> None:
    found = {"rate_limits": {"primary": {"used_percent": 1, "window_minutes": 300, "resets_at": FIVE_HOUR_RESET}},
             "timestamp": stamp}
    assert parse_codex(found, CAPTURED) == []


TOKEN_COUNT_NULL = (b'{"timestamp":"2026-11-14T16:26:00.000Z","type":"event_msg","payload":'
                    b'{"type":"token_count","info":null,"rate_limits":null}}\n')
TOKEN_COUNT_MISSING = (b'{"timestamp":"2026-11-14T16:26:00.000Z","type":"event_msg","payload":'
                       b'{"type":"token_count","info":null}}\n')
TOKEN_COUNT_ONE_WINDOW = (b'{"timestamp":"2026-11-14T16:26:00.000Z","type":"event_msg","payload":'
                          b'{"type":"token_count","rate_limits":{"primary":{"used_percent":40.0,'
                          b'"window_minutes":300,"resets_at":1794684332}}}}\n')
TOKEN_COUNT_BAD_RESET = (b'{"timestamp":"2026-11-14T16:26:00.000Z","type":"event_msg","payload":'
                         b'{"type":"token_count","rate_limits":{"primary":{"used_percent":40.0,'
                         b'"window_minutes":300,"resets_at":1794684332},"secondary":{"used_percent":50.0,'
                         b'"window_minutes":10080,"resets_at":1}}}}\n')
LATER = CAPTURED + 28  # 2026-11-14T16:26:00Z
LAST_TOTAL = {"input_tokens": 2200, "cached_input_tokens": 1300, "output_tokens": 60, "total_tokens": 2260}


@pytest.mark.parametrize("last", [TOKEN_COUNT_NULL, TOKEN_COUNT_MISSING])
def test_a_last_token_count_without_rate_limits_is_no_reading_but_keeps_the_last_total(
    tmp_path: Path, last: bytes
) -> None:
    found = codex_rollout.latest(str(codex_home(tmp_path, ROLLOUT + last)), THREAD)
    assert found == {
        "rate_limits": None,
        "totals": {THREAD: LAST_TOTAL},
        "timestamp": "2026-11-14T16:26:00.000Z",
    }
    assert parse_codex(found, CAPTURED + 60) == [
        Reading("five_hour", None, None, LATER, 0, LATER), Reading("seven_day", None, None, LATER, 0, LATER)]


@pytest.mark.parametrize(
    ("info", "tokens"),
    [
        ({"total_token_usage": {"input_tokens": "5", "cached_input_tokens": True, "output_tokens": 7,
                                "total_tokens": None, "reasoning_output_tokens": 3, "note": 1}},
         {"output_tokens": 7}),
        ({"total_token_usage": None}, LAST_TOTAL),
        ({"total_token_usage": {"input_tokens": "9"}}, LAST_TOTAL),
        ({"last_token_usage": {"input_tokens": 9}}, LAST_TOTAL),
        ([], LAST_TOTAL),
    ],
)
def test_the_reader_keeps_only_numeric_session_totals(tmp_path: Path, info: object, tokens: object) -> None:
    line = {"timestamp": "2026-11-14T16:26:00.000Z", "type": "event_msg",
            "payload": {"type": "token_count", "info": info, "rate_limits": None}}
    found = codex_rollout.latest(str(codex_home(tmp_path, ROLLOUT + json.dumps(line).encode() + b"\n")), THREAD)
    assert found is not None and found["totals"] == {THREAD: tokens}


def test_the_reader_opens_only_the_jobs_own_regular_rollout(tmp_path: Path) -> None:
    home = codex_home(tmp_path)
    assert codex_rollout.latest(str(home), "00000000-0000-4000-8000-000000000999") is None
    assert codex_rollout.latest(str(home), "../../auth") is None
    assert codex_rollout.latest(str(home), "*") is None
    other = tmp_path / "elsewhere"
    other.mkdir()
    linked = home / "sessions" / "2026" / "11" / "15"
    linked.mkdir()
    secret = other / "secret.jsonl"
    secret.write_bytes(ROLLOUT)
    thread = "00000000-0000-4000-8000-000000000152"
    (linked / f"rollout-x-{thread}.jsonl").symlink_to(secret)
    assert codex_rollout.latest(str(home), thread) is None


def test_the_reader_runs_alone_under_isolated_python_as_on_a_remote_device(tmp_path: Path) -> None:
    home = codex_home(tmp_path)
    source = Path(codex_rollout.__file__).read_text(encoding="utf-8")
    env = {"CODEX_HOME": str(home), "PATH": os.environ.get("PATH", "")}
    out = subprocess.run([sys.executable, "-I", "-S", "-c", source, THREAD], capture_output=True,
                         env=env, cwd=tmp_path, check=True, timeout=30).stdout
    assert json.loads(out) == codex_rollout.latest(str(home), THREAD)
    assert subprocess.run([sys.executable, "-I", "-S", "-c", source, "nope"], capture_output=True, env=env,
                          check=True, timeout=30).stdout == b""


# --- capture: the stream flags, the thread id and the probes ------------------------------


def test_codex_bots_wrapper_gets_json_right_after_its_exec() -> None:
    argv, harness = harness_argv(WRAPPER, "codex")
    assert argv == [WRAPPER[0], "exec", "--json", *WRAPPER[2:]] and harness == "codex"
    assert harness_argv(WRAPPER, None) == (WRAPPER, None)
    assert harness_argv(WRAPPER, "claude") == (WRAPPER, None)
    already = [WRAPPER[0], "exec", "--json", "-"]
    assert harness_argv(already, "codex") == (already, None)
    assert harness_argv(["env", "codex", "exec", "-"], "codex") == (["env", "codex", "exec", "-"], None)


def _start(tmp_path: Path, command: list[str], bot: str, environ: dict, worker_env: bool = False) -> Session:
    session = telemetry.start_session(
        connect=lambda: sqlite3.connect(tmp_path / "orch.db"), database=tmp_path / "orch.db",
        job_id="job-1", bot=bot, repo="acme/widgets", command=command, log=lambda _m: None,
        environ={"ORCH_JOB_STATE_DIR": str(tmp_path / "state"), **environ}, stdout=io.BytesIO(),
        worker_env=worker_env, device="box",
    )
    assert session is not None
    return session


def test_a_box_codex_job_stores_its_rollout_readings_on_flush(tmp_path: Path) -> None:
    home = codex_home(tmp_path)
    session = _start(tmp_path, WRAPPER, "codex-bot", {"CODEX_HOME": str(home)})
    assert session.harness == "codex" and "--json" in session.flags.argv
    session.clock = lambda: CAPTURED
    session.consume(io.BytesIO(CODEX_EXEC))
    assert session.summary.thread_id == THREAD and session.summary.tokens_in == 1200
    session.flush()
    assert _stored(tmp_path / "orch.db") == [
        ("codex", "codex-bot", "box", "five_hour", 0.125, FIVE_HOUR_RESET, EMITTED, "job-1"),
        ("codex", "codex-bot", "box", "seven_day", 0.345, SEVEN_DAY_RESET, EMITTED, "job-1"),
    ]


def _codex_session(tmp_path: Path, now: list[float]) -> tuple[Session, Path]:
    home = codex_home(tmp_path)
    session = _start(tmp_path, WRAPPER, "codex-bot", {"CODEX_HOME": str(home)})
    session.clock = lambda: now[0]
    session.consume(io.BytesIO(CODEX_EXEC))
    session.flush()
    [rollout] = (home / "sessions").rglob("rollout-*.jsonl")
    return session, rollout


def _box_codex(tmp_path: Path, now: float) -> dict:
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        return _accounts(read_usage(connection, now, BOX))["codex"]


def test_rereading_the_same_rollout_event_never_makes_it_fresher(tmp_path: Path) -> None:
    now = [CAPTURED]
    session, _rollout = _codex_session(tmp_path, now)
    now[0] = EMITTED + usage_readings.STALE_SECONDS + 1
    session.flush()
    entry = _box_codex(tmp_path, now[0])
    assert entry["observed_at"] == EMITTED and entry["age_seconds"] == usage_readings.STALE_SECONDS + 1
    assert entry["state"] == "stale" and entry["headroom"] is None
    assert entry["seven_day"] == {"used": None, "resets_at": SEVEN_DAY_RESET, "observed_at": EMITTED,
                                  "state": "stale"}
    assert session.usage["seven_day"].observed_at == EMITTED


@pytest.mark.parametrize(
    ("last", "five_hour", "seven_day"),
    [
        (TOKEN_COUNT_NULL, None, None),
        (TOKEN_COUNT_MISSING, None, None),
        (TOKEN_COUNT_ONE_WINDOW, 0.4, None),
        (TOKEN_COUNT_BAD_RESET, 0.4, None),
    ],
)
def test_a_newer_rollout_event_without_a_window_clears_its_old_numbers(
    tmp_path: Path, last: bytes, five_hour: float | None, seven_day: float | None
) -> None:
    now = [CAPTURED]
    session, rollout = _codex_session(tmp_path, now)
    with rollout.open("ab") as out:
        out.write(last)
    now[0] = CAPTURED + 60
    session.flush()
    entry = _box_codex(tmp_path, now[0])
    assert (entry["five_hour"]["used"], entry["seven_day"]["used"]) == (five_hour, seven_day)
    assert entry["seven_day"]["state"] == "no reading" and entry["observed_at"] == LATER
    assert entry["state"] == ("ok" if five_hour is not None else "no reading")


def test_a_later_claude_login_without_limits_clears_the_stored_numbers(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    first = _session(db, job_id="job-1")
    first.add_usage(parse_get_usage(GET_USAGE, CAPTURED))
    first.finish(0)
    later = _session(db, job_id="job-2", at=CAPTURED + 600)
    later.add_usage(parse_get_usage(UNAVAILABLE, CAPTURED + 600))
    later.finish(0)
    assert [row[3:] for row in _stored(db)] == [
        ("five_hour", None, None, CAPTURED + 600, "job-2"), ("seven_day", None, None, CAPTURED + 600, "job-2")]
    with sqlite3.connect(db) as connection:
        entry = _accounts(read_usage(connection, CAPTURED + 660, BOX))["claude"]
    assert entry["state"] == "no reading" and entry["headroom"] is None
    assert entry["five_hour"]["used"] is None and entry["seven_day"]["used"] is None
    assert _source(db, CAPTURED + 660).read("claude") is None


def test_a_remote_codex_job_is_not_read_on_the_box(tmp_path: Path) -> None:
    session = _start(tmp_path, WRAPPER, "codex-bot", {"CODEX_HOME": str(codex_home(tmp_path))}, worker_env=True)
    assert session.rollout_home is None


def test_a_bad_thread_id_is_never_kept() -> None:
    summary = telemetry.Summary("codex", Scrubber({}))
    summary.feed({"type": "thread.started", "thread_id": "../../auth.json"}, CAPTURED)
    assert summary.thread_id is None


class FakeTransport:
    def __init__(self, result: Exchanged):
        self.result = result
        self.calls: list[tuple[list[str], bytes]] = []

    def run(self, argv, *, timeout):  # pragma: no cover - the Transport protocol
        return 0

    def exchange(self, argv, *, timeout, input=b""):
        self.calls.append((list(argv), input))
        return self.result


TARGET = RemoteTarget("pc", "pc-wsl-{bot}", "claude-bot", "/home/{bot}/factory-work", "job-1")


def test_the_local_probe_sends_only_get_usage_and_never_the_proof() -> None:
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, GET_USAGE, b"")

    out = usage_probe.claude_local("/opt/claude/bin/claude", {PROOF_ENV: "TEST", "HOME": "/h"}, run=run)
    assert out == GET_USAGE
    [(argv, kwargs)] = calls
    assert argv == ["/opt/claude/bin/claude", "-p", "--input-format", "stream-json",
                    "--output-format", "stream-json", "--verbose"]
    assert json.loads(kwargs["input"]) == {"type": "control_request", "request_id": "orch-usage",
                                           "request": {"subtype": "get_usage", "skip_behaviors": True}}
    assert PROOF_ENV not in kwargs["env"] and kwargs["env"]["HOME"] == "/h"

    def slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    with pytest.raises(usage_probe.ProbeFailed, match=r"^timed out after 30s$"):
        usage_probe.claude_local("claude", {}, run=slow)


@pytest.mark.parametrize(
    ("command", "bot", "probed"),
    [
        (["/usr/local/bin/claude", "-p"], "claude-bot", True),
        (["claude-harness", *CLAUDE_BOT], "claude-bot", True),
        (["/home/claude-bot/.local/bin/claude-harness", *CLAUDE_BOT], "claude-bot", True),
        (["fake-harness", "--from-config"], "claude-bot", False),
        (["claude-harness", "run", *CLAUDE_BOT], "claude-bot", False),
        (["codex", "exec", "-"], "codex-bot", False),
        ([], "claude-bot", False),
    ],
    ids=["claude", "wrapper", "wrapper-path", "not-print-mode", "wrapper-not-passing-options", "codex", "empty"],
)
def test_the_probe_gate_is_the_jobs_own_stream_flags(
    tmp_path: Path, command: list[str], bot: str, probed: bool
) -> None:
    session = _start(tmp_path, command, bot, {})
    assert session.flags == stream_flags(command, telemetry.HARNESS_BY_BOT[bot])
    assert (session.flags.harness == "claude") is probed


def test_the_remote_probe_runs_claude_under_the_bots_login_shell() -> None:
    transport = FakeTransport(Exchanged(0, GET_USAGE, ""))
    assert usage_probe.claude_remote(transport, TARGET) == GET_USAGE
    [(argv, data)] = transport.calls
    assert argv == ssh_argv(TARGET, usage_probe.claude_remote_script())
    assert argv[-1].startswith("exec bash -lc ") and data == usage_probe.REQUEST
    with pytest.raises(usage_probe.ProbeFailed, match=r"^ssh exit 255$"):
        usage_probe.claude_remote(FakeTransport(Exchanged(255, GET_USAGE, "down")), TARGET)
    with pytest.raises(usage_probe.ProbeFailed, match=r"^no byte transport$"):
        usage_probe.claude_remote(object(), TARGET)


def test_a_remote_codex_read_sends_the_reader_and_returns_its_line(tmp_path: Path) -> None:
    found = codex_rollout.latest(str(codex_home(tmp_path)), THREAD)
    transport = FakeTransport(Exchanged(0, json.dumps(found).encode() + b"\n", ""))
    assert usage_probe.remote_codex(transport, TARGET, "codex", THREAD) == found
    [(argv, _data)] = transport.calls
    assert THREAD in argv[-1] and "def latest" in argv[-1]
    assert usage_probe.remote_codex(transport, TARGET, "codex", None) is None
    assert usage_probe.remote_codex(transport, TARGET, "claude", THREAD) is None
    assert usage_probe.remote_codex(transport, TARGET, "codex", "../x") is None
    assert usage_probe.remote_codex(FakeTransport(Exchanged(0, b"garbage", "")), TARGET, "codex", THREAD) is None
    assert usage_probe.remote_codex(FakeTransport(Exchanged(255, b"", "down")), TARGET, "codex", THREAD) is None


def test_a_remote_codex_job_stores_its_rollout_readings_and_session_total(tmp_path: Path) -> None:
    found = codex_rollout.latest(str(codex_home(tmp_path)), THREAD)
    transport = FakeTransport(Exchanged(0, json.dumps(found).encode() + b"\n", ""))
    session = _start(tmp_path, WRAPPER, "codex-bot", {}, worker_env=True)
    session.clock = lambda: CAPTURED
    session.consume(io.BytesIO(CODEX_EXEC))
    session.add_rollout(usage_probe.remote_codex(transport, TARGET, session.harness, session.summary.thread_id))
    session.finish(0)
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        tokens = connection.execute(
            "SELECT tokens_in, tokens_cached, tokens_out, tokens_unknown FROM job_telemetry").fetchone()
    assert tokens == (2200, 1300, 60, 0)
    assert [row[3] for row in _stored(tmp_path / "orch.db")] == ["five_hour", "seven_day"]


def test_the_claude_probe_thread_hands_its_readings_to_the_session(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    session = _session(db)
    lines: list[str] = []
    usage_probe.start_claude(session.add_usage, lambda: CAPTURED, lambda: GET_USAGE, lines.append).join(5)
    assert lines == []
    usage_probe.start_claude(session.add_usage, lambda: CAPTURED, lambda: 1 / 0, lines.append).join(5)
    assert lines == ["usage probe: failed (ZeroDivisionError)"]
    session.finish(0)
    assert [row[3:5] for row in _stored(db)] == [("five_hour", 0.125), ("seven_day", 0.345)]


def test_the_probe_reading_is_observed_when_it_was_asked_so_the_stream_stays_newer(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    session = _session(db)
    now = [CAPTURED]

    def probe() -> bytes:
        event = rate_limit_event({"seven_day": {"utilization": 0.82, "resetsAt": SEVEN_DAY_RESET}})
        now[0] = CAPTURED + 5
        session.add_usage(parse_rate_limit_event(event, now[0]))
        now[0] = CAPTURED + 10
        return UNAVAILABLE

    usage_probe.start_claude(session.add_usage, lambda: now[0], probe, lambda _line: None).join(5)
    session.finish(0)
    assert [row[3:6] for row in _stored(db)] == [
        ("five_hour", None, None), ("seven_day", 0.82, SEVEN_DAY_RESET)]


PROBE_MARKER = "PROBE-OUTPUT-MARKER"
TAG_VALUE = "worker-tag-value-7c1"
ENV_VALUE = "plain-env-value-3f9"

HANGING_CLAUDE = f"""#!{sys.executable}
import os, sys, time
print({PROBE_MARKER!r}, *os.environ.values(), flush=True)
print({PROBE_MARKER!r}, *os.environ.values(), file=sys.stderr, flush=True)
time.sleep(60)
"""


def _hanging_claude(tmp_path: Path, name: str = "claude") -> str:
    path = tmp_path / name
    path.write_text(HANGING_CLAUDE, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


@pytest.mark.parametrize(
    ("binary", "timeout", "line"),
    [
        ("hang", 0.5, "usage probe: failed (timed out after 0.5s)"),
        ("missing", 30.0, "usage probe: failed (could not start (FileNotFoundError))"),
    ],
)
def test_a_local_probe_failure_logs_one_fixed_line(tmp_path: Path, binary: str, timeout: float, line: str) -> None:
    path = _hanging_claude(tmp_path) if binary == "hang" else str(tmp_path / "nowhere" / "claude")
    environ = {"PATH": os.environ["PATH"], PROOF_ENV: CLAIM_PROOF, TAG_ENV: TAG_VALUE, "PROBE_TEST": ENV_VALUE}
    lines: list[str] = []
    added: list[list[Reading]] = []
    usage_probe.start_claude(
        added.append, lambda: CAPTURED,
        lambda: usage_probe.claude_local(path, environ, timeout=timeout), lines.append,
    ).join(10)
    assert lines == [line] and added == []


def test_a_remote_probe_timeout_logs_a_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _hanging_claude(tmp_path, "ssh")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("ORCH_SSH_MUX", "off")
    monkeypatch.setenv(PROOF_ENV, CLAIM_PROOF)
    monkeypatch.setenv(TAG_ENV, TAG_VALUE)
    monkeypatch.setenv("PROBE_TEST", ENV_VALUE)
    lines: list[str] = []
    added: list[list[Reading]] = []
    usage_probe.start_claude(
        added.append, lambda: CAPTURED,
        lambda: usage_probe.claude_remote(SshTransport(), TARGET, timeout=0.5), lines.append,
    ).join(10)
    assert lines == ["usage probe: failed (timed out after 0.5s)"] and added == []
    assert not any(secret in lines[0] for secret in (PROBE_MARKER, CLAIM_PROOF, TAG_VALUE, ENV_VALUE))


@pytest.mark.parametrize(
    ("out", "line"),
    [
        (b"", "usage probe: no reply"),
        (reply(None, subtype="error"), "usage probe: no reply"),
        (OBSERVED_UNAVAILABLE, "usage probe: no rate limits for this login"),
        (UNAVAILABLE, "usage probe: no rate limits for this login"),
    ],
)
def test_a_reply_without_numbers_logs_why(out: bytes, line: str) -> None:
    lines: list[str] = []
    usage_probe.start_claude(lambda _r: None, lambda: CAPTURED, lambda: out, lines.append).join(5)
    assert lines == [line]


PROBE_CLAUDE = """#!{python}
import datetime, json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
argv = sys.argv[1:]
if "--input-format" in argv:
    request = sys.stdin.read()
    with open(os.path.join(here, "probe-stdin"), "a", encoding="utf-8") as seen:
        seen.write(request)
    print({marker!r}, *os.environ.values(), flush=True)
    print({marker!r}, *os.environ.values(), file=sys.stderr, flush=True)
    mode = open(os.path.join(here, "probe-mode"), encoding="utf-8").read().strip()
    asked = [json.loads(line) for line in request.splitlines() if line.strip()]
    asked = [r for r in asked if r.get("request", {{}}).get("subtype") == "get_usage"]

    def at(seconds):
        return datetime.datetime.fromtimestamp(time.time() + seconds, datetime.timezone.utc).isoformat()

    if mode == "reply" and asked:
        limits = {{"five_hour": {{"utilization": 12.5, "resets_at": at(3600)}},
                   "seven_day": {{"utilization": 34.5, "resets_at": at(86400)}}}}
        print(json.dumps({{"type": "control_response", "response": {{
            "subtype": "success", "request_id": asked[0]["request_id"],
            "response": {{"rate_limits_available": True, "rate_limits": limits}}}}}}))
    elif mode == "unavailable" and asked:
        print(json.dumps({{"type": "control_response", "response": {{
            "subtype": "success", "request_id": asked[0]["request_id"],
            "response": {{"rate_limits_available": False, "rate_limits": {{}}}}}}}}))
    sys.exit({{"exit": 1, "ssh": 255}}.get(mode, 0))
if argv[:3] != ["--output-format", "stream-json", "--verbose"]:
    print("text mode: no events")
    sys.exit(0)
for line in open({fixture!r}, encoding="utf-8"):
    sys.stdout.write(line.replace("@@GH_TOKEN@@", "x").replace("@@API_KEY@@", ""))
print("ORCH-HANDOFF: done")
"""


def _probe_bin(directory: Path, mode: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    claude = directory / "claude"
    fixture = ROOT / "tests" / "fixtures" / "telemetry" / "claude_stream.jsonl"
    claude.write_text(
        PROBE_CLAUDE.format(python=sys.executable, marker=PROBE_MARKER, fixture=str(fixture)), encoding="utf-8")
    claude.chmod(0o755)
    (directory / "probe-mode").write_text(mode, encoding="utf-8")
    wrapper = directory / "claude-harness"
    wrapper.write_text(test_telemetry_argv.WRAPPER.format(claude=shlex.quote(str(claude))), encoding="utf-8")
    wrapper.chmod(0o755)
    return wrapper


def _box_run(tmp_path: Path, mode: str, command: list[str] | None = None, env: dict | None = None,
             machines: Path = LOCAL_BOX_MACHINES):
    _, job = _running_job(tmp_path)
    wrapper = _probe_bin(tmp_path / "bin", mode)
    run = _orch(
        "job", "run", "claude-bot", job.id, "--machines", str(machines), "--db", str(tmp_path / "orch.db"), "--interval", "0.2",
        "--", *(command or [str(wrapper), *CLAUDE_BOT]),
        env={"ORCH_JOB_STATE_DIR": str(tmp_path / "state"), TAG_ENV: TAG_VALUE, "PROBE_TEST": ENV_VALUE,
             **(env or {})},
    )
    assert run.returncode == 0, run.stderr
    return run, tmp_path / "bin", tmp_path / "orch.db"


def _remote_run(tmp_path: Path, mode: str):
    from tests.test_remote_start import remote_setup

    machines, queue, _, job_id, ssh = remote_setup(tmp_path, "claude")
    home = tmp_path / "worker-home"
    home.mkdir()
    worker_bin = tmp_path / "worker-bin"
    _probe_bin(worker_bin, mode)
    (home / ".bash_profile").write_text(f"export PATH={shlex.quote(str(worker_bin))}:$PATH\n", encoding="utf-8")
    run = _orch(
        "job", "run", "claude-bot", job_id, "--db", str(queue.path), "--machines", str(machines),
        "--", "claude-harness", *CLAUDE_BOT,
        env={"HOME": str(tmp_path), **ssh.env(), "FAKE_SSH_HOME": str(home), TAG_ENV: TAG_VALUE,
             "PROBE_TEST": ENV_VALUE},
    )
    assert run.returncode == 0, run.stderr
    return run, worker_bin, queue.path


def _probe_lines(run: subprocess.CompletedProcess) -> list[str]:
    for secret in (PROBE_MARKER, CLAIM_PROOF, TAG_VALUE, ENV_VALUE):
        assert secret not in run.stderr and secret not in run.stdout
    return [line for line in run.stderr.splitlines() if "usage probe:" in line]


def _usage_rows(db: Path) -> list[tuple]:
    with sqlite3.connect(db) as connection:
        return connection.execute(
            "SELECT harness, bot, device, window, used FROM usage_readings ORDER BY window").fetchall()


def test_a_box_claude_bot_wrapper_job_is_probed_through_its_wrapper(tmp_path: Path) -> None:
    run, bin_dir, db = _box_run(tmp_path, "reply")
    assert (bin_dir / "probe-stdin").read_bytes() == usage_probe.REQUEST
    assert _probe_lines(run) == []
    assert _usage_rows(db) == [
        ("claude", "claude-bot", "box", "five_hour", 0.125), ("claude", "claude-bot", "box", "seven_day", 0.345)]


def test_a_job_stores_its_readings_under_the_placed_devices_account(tmp_path: Path) -> None:
    machines = tmp_path / "machines.yml"
    machines.write_text(
        "accounts:\n  - {id: claude-max-1, harness: claude}\n"
        + LOCAL_BOX_MACHINES.read_text(encoding="utf-8").replace(
            "claude: claude-bot", "claude: {bot: claude-bot, account: claude-max-1}", 1),
        encoding="utf-8",
    )
    _, _, db = _box_run(tmp_path, "reply", machines=machines)
    with sqlite3.connect(db) as connection:
        rows = connection.execute("SELECT device, window, account FROM usage_readings ORDER BY window").fetchall()
    assert rows == [("box", "five_hour", "claude-max-1"), ("box", "seven_day", "claude-max-1")]


def test_a_remote_claude_bot_job_is_probed_on_its_device(tmp_path: Path) -> None:
    run, worker_bin, db = _remote_run(tmp_path, "reply")
    assert (worker_bin / "probe-stdin").read_bytes() == usage_probe.REQUEST
    assert _probe_lines(run) == []
    assert _usage_rows(db) == [
        ("claude", "claude-bot", "pc", "five_hour", 0.125), ("claude", "claude-bot", "pc", "seven_day", 0.345)]


@pytest.mark.parametrize(
    ("mode", "line", "rows"),
    [
        ("exit", "usage probe: failed (exit 1)", []),
        ("silent", "usage probe: no reply", []),
        ("unavailable", "usage probe: no rate limits for this login",
         [("claude", "claude-bot", "box", "five_hour", None), ("claude", "claude-bot", "box", "seven_day", None)]),
    ],
)
def test_a_box_probe_without_numbers_logs_one_line_and_nothing_it_printed(
    tmp_path: Path, mode: str, line: str, rows: list[tuple]
) -> None:
    run, bin_dir, db = _box_run(tmp_path, mode)
    assert (bin_dir / "probe-stdin").read_bytes() == usage_probe.REQUEST
    assert _probe_lines(run) == [f"orch job run: {line}"]
    assert (_usage_rows(db) if rows else []) == rows


def test_a_remote_probe_whose_ssh_fails_logs_its_exit(tmp_path: Path) -> None:
    run, worker_bin, _ = _remote_run(tmp_path, "ssh")
    assert (worker_bin / "probe-stdin").read_bytes() == usage_probe.REQUEST
    assert _probe_lines(run) == ["orch job run: usage probe: failed (ssh exit 255)"]


@pytest.mark.parametrize(
    ("not_claude", "env", "reason"),
    [
        (True, {}, "wrapper does not pass harness options"),
        (False, {"ORCH_TELEMETRY": "off"}, "telemetry off"),
    ],
    ids=["not-claude", "telemetry-off"],
)
def test_a_job_that_is_not_probed_says_why_once(tmp_path: Path, not_claude: bool, env: dict, reason: str) -> None:
    command = [sys.executable, str(tmp_path / "bin" / "claude"), "-p"] if not_claude else None
    run, bin_dir, _ = _box_run(tmp_path, "reply", command, env)
    assert not (bin_dir / "probe-stdin").exists()
    assert _probe_lines(run) == [f"orch job run: usage probe: skipped ({reason})"]


# --- storing on the telemetry flush ------------------------------------------------------


def _session(db: Path, *, job_id: str = "job-1", device: str = "box", at: float = CAPTURED) -> Session:
    return Session(
        connect=lambda: sqlite3.connect(db), job_id=job_id, bot="claude-bot", repo="acme/widgets",
        harness="claude", flags=stream_flags(["claude", "-p"]), raw_log=None, scrub=Scrubber({}),
        log=lambda _m: None, stdout=io.BytesIO(), clock=lambda: at, device=device,
    )


def _stored(db: Path) -> list[tuple]:
    with sqlite3.connect(db) as connection:
        return connection.execute(
            "SELECT harness, bot, device, window, used, resets_at, observed_at, job_id"
            " FROM usage_readings ORDER BY device, window"
        ).fetchall()


def test_readings_are_stored_with_the_telemetry_row_on_flush(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    session = _session(db, device="pc")
    session.add_usage(parse_get_usage(GET_USAGE, CAPTURED))
    session.flush()
    assert _stored(db) == [
        ("claude", "claude-bot", "pc", "five_hour", 0.125, FIVE_HOUR_RESET, CAPTURED, "job-1"),
        ("claude", "claude-bot", "pc", "seven_day", 0.345, SEVEN_DAY_RESET, CAPTURED, "job-1"),
    ]
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM job_telemetry").fetchone() == (1,)


def test_an_older_reading_never_replaces_a_newer_one(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    newer = _session(db, job_id="job-new")
    newer.add_usage([Reading("seven_day", 0.345, SEVEN_DAY_RESET, CAPTURED)])
    newer.finish(0)
    older = _session(db, job_id="job-old")
    older.add_usage([Reading("seven_day", 0.05, SEVEN_DAY_RESET, CAPTURED - 600)])
    older.finish(0)
    [row] = _stored(db)
    assert row[4] == 0.345 and row[-1] == "job-new"


def test_a_run_without_readings_stores_none(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    session = _session(db)
    session.add_usage(parse_get_usage(reply(None, subtype="error"), CAPTURED))
    session.finish(0)
    with sqlite3.connect(db) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    assert "usage_readings" not in tables


def test_telemetry_off_means_no_session_and_so_no_readings(tmp_path: Path) -> None:
    assert telemetry.start_session(
        connect=lambda: sqlite3.connect(tmp_path / "orch.db"), database=tmp_path / "orch.db",
        job_id="job-1", bot="claude-bot", repo="acme/widgets", command=["claude", "-p", "x"],
        log=lambda _m: None, environ={"ORCH_TELEMETRY": "off"}, device="box",
    ) is None


# --- the usage source for routing ---------------------------------------------------------


def _with_rows(db: Path, *rows: tuple[str, float | None, int, float], harness: str = "claude") -> None:
    with sqlite3.connect(db) as connection:
        usage_readings.store(
            connection, harness=harness, bot=f"{harness}-bot", device="box", job_id="job-1",
            readings=[Reading(window, used, resets, observed) for window, used, resets, observed in rows],
        )


def _source(db: Path, now: float, accounts: dict[str, str] | None = None) -> TelemetryUsage:
    used = accounts if accounts is not None else {"claude": "claude-default", "codex": "codex-default"}
    return TelemetryUsage(lambda: sqlite3.connect(db), account_for=used.get, clock=lambda: now)


def test_one_fresh_window_is_an_unverified_report_and_never_blocks(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    _with_rows(db, ("seven_day", 0.9, SEVEN_DAY_RESET, CAPTURED))
    report = _source(db, CAPTURED + 60).read("claude")
    assert report is not None and report.verified is False
    assert report.remaining == pytest.approx(0.1)
    assert reserve_check("claude", _source(db, CAPTURED + 60)).reason == "no-verified-source"


def test_both_fresh_windows_are_verified_and_the_reserve_applies(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    _with_rows(db, ("seven_day", 0.3, SEVEN_DAY_RESET, CAPTURED), ("five_hour", 0.85, FIVE_HOUR_RESET, CAPTURED),
               harness="codex")
    report = _source(db, CAPTURED + 60).read("codex")
    assert report is not None and report.verified is True
    assert report.remaining == pytest.approx(0.15)
    check = reserve_check("codex", _source(db, CAPTURED + 60))
    assert (check.start, check.reason) == (False, "reroute")


def test_stale_or_missing_readings_give_no_report(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    assert _source(db, CAPTURED).read("claude") is None  # no table
    _with_rows(db, ("five_hour", 0.5, FIVE_HOUR_RESET, CAPTURED))
    assert _source(db, FIVE_HOUR_RESET + 1).read("claude") is None  # the window reset
    assert _source(db, CAPTURED + usage_readings.STALE_SECONDS + 1).read("claude") is None
    assert _source(db, CAPTURED).read("codex") is None
    assert _source(db, CAPTURED).read("cursor") is None


def _store_account(db: Path, account: str, device: str, *rows: tuple[str, float, int, float]) -> None:
    with sqlite3.connect(db) as connection:
        usage_readings.store(
            connection, harness="codex", bot="codex-bot", device=device, job_id=f"job-{account}",
            readings=[Reading(window, used, resets, observed) for window, used, resets, observed in rows],
            account=account,
        )


def test_two_accounts_of_one_bot_keep_their_own_windows_and_headroom(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    _store_account(db, "codex-a", "box", ("five_hour", 0.9, FIVE_HOUR_RESET, CAPTURED),
                   ("seven_day", 0.5, SEVEN_DAY_RESET, CAPTURED))
    _store_account(db, "codex-b", "pc", ("five_hour", 0.2, FIVE_HOUR_RESET + 3600, CAPTURED),
                   ("seven_day", 0.3, SEVEN_DAY_RESET - 86400, CAPTURED))
    a = _source(db, CAPTURED + 60, {"codex": "codex-a"}).read("codex")
    b = _source(db, CAPTURED + 60, {"codex": "codex-b"}).read("codex")
    assert a is not None and a.verified and a.remaining == pytest.approx(0.1)
    assert b is not None and b.verified and b.remaining == pytest.approx(0.7)
    with sqlite3.connect(db) as connection:
        rows = connection.execute(
            "SELECT account, device, window, used, resets_at FROM usage_readings ORDER BY account, window"
        ).fetchall()
    assert rows == [
        ("codex-a", "box", "five_hour", 0.9, FIVE_HOUR_RESET),
        ("codex-a", "box", "seven_day", 0.5, SEVEN_DAY_RESET),
        ("codex-b", "pc", "five_hour", 0.2, FIVE_HOUR_RESET + 3600),
        ("codex-b", "pc", "seven_day", 0.3, SEVEN_DAY_RESET - 86400),
    ]


def test_an_account_on_two_devices_reads_the_newest_reading_of_each_window(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    _store_account(db, "codex-a", "box", ("five_hour", 0.9, FIVE_HOUR_RESET, CAPTURED + 60),
                   ("seven_day", 0.5, SEVEN_DAY_RESET, CAPTURED))
    _store_account(db, "codex-a", "pc", ("five_hour", 0.2, FIVE_HOUR_RESET, CAPTURED),
                   ("seven_day", 0.6, SEVEN_DAY_RESET, CAPTURED + 60))
    report = _source(db, CAPTURED + 120, {"codex": "codex-a"}).read("codex")
    assert report is not None and report.remaining == pytest.approx(0.1)
    assert _source(db, CAPTURED + 120, {"codex": "codex-b"}).read("codex") is None


# --- the snapshot ------------------------------------------------------------------------


def _accounts(usage: dict) -> dict[str, dict]:
    return {entry["harness"]: entry for entry in usage["accounts"]}


def test_snapshot_usage_has_one_entry_per_account(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    make_db(db)
    with sqlite3.connect(db) as connection:
        usage_readings.store(connection, harness="claude", bot="claude-bot", device="box", job_id="job-1",
                             readings=parse_get_usage(GET_USAGE, CAPTURED))
    machines = tmp_path / "machines.yml"
    machines.write_text(
        "devices:\n  - name: box\n    local: true\n    slot_max: 3\n    harnesses:\n"
        "      claude: claude-bot\n      codex: codex-bot\n      cursor: cursor-bot\n",
        encoding="utf-8",
    )
    snapshot = collect_snapshot(db, tmp_path / "poll.log", now=CAPTURED + 120, machines_path=machines)
    usage = snapshot["usage"]
    assert usage["state"] == "ok"
    accounts = _accounts(usage)
    assert set(accounts) == {"claude", "codex", "cursor"}
    assert accounts["claude"] == {
        "account": "claude-default",
        "plan": None,
        "label": None,
        "source": "get_usage",
        "bot": "claude-bot",
        "bots": ["claude-bot"],
        "harness": "claude",
        "devices": ["box"],
        "state": "ok",
        "five_hour": {"used": 0.125, "resets_at": FIVE_HOUR_RESET, "observed_at": CAPTURED, "state": "ok"},
        "seven_day": {"used": 0.345, "resets_at": SEVEN_DAY_RESET, "observed_at": CAPTURED, "state": "ok"},
        "headroom": 0.655,
        "observed_at": CAPTURED,
        "age_seconds": 120,
        "limited_until": None,
        "limit_label": None,
    }
    assert accounts["codex"]["state"] == "no reading"
    assert accounts["codex"]["headroom"] is None
    assert accounts["cursor"]["state"] == "not tracked"
    json.dumps(snapshot, allow_nan=False)


SHARED = MachinesConfig((
    Device("box", 3, {"claude": "claude-bot", "codex": "codex-bot"}, local=True),
    Device("pc", 2, {"claude": "claude-bot"}),
))


def test_an_account_shared_by_devices_is_one_entry_with_the_newest_reading_per_window(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "orch.db")
    for device, harness, bot, window, used, observed in (
        ("box", "claude", "claude-bot", "five_hour", 0.6, CAPTURED - 60),
        ("box", "claude", "claude-bot", "seven_day", 0.2, CAPTURED - 60),
        ("pc", "claude", "claude-bot", "five_hour", 0.3, CAPTURED - 30),
        ("mac", "claude", "claude-old", "seven_day", 0.5, CAPTURED - 30),
    ):
        usage_readings.store(connection, harness=harness, bot=bot, device=device, job_id="j",
                             readings=[Reading(window, used, SEVEN_DAY_RESET, observed)])
    connection.execute("UPDATE usage_readings SET account = 'claude-old' WHERE device = 'mac'")
    usage = read_usage(connection, CAPTURED, SHARED)
    assert [(e["harness"], e["bot"], e["devices"]) for e in usage["accounts"]] == [
        ("claude", "claude-bot", ["box", "pc"]), ("codex", "codex-bot", ["box"]),
        ("claude", "claude-old", ["mac"])]
    claude = usage["accounts"][0]
    assert (claude["five_hour"]["used"], claude["five_hour"]["observed_at"]) == (0.3, CAPTURED - 30)
    assert (claude["seven_day"]["used"], claude["seven_day"]["observed_at"]) == (0.2, CAPTURED - 60)
    assert claude["headroom"] == 0.7 and claude["observed_at"] == CAPTURED - 30


def test_a_database_without_the_table_reads_as_no_telemetry(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "orch.db")
    usage = read_usage(connection, CAPTURED, BOX)
    assert usage["state"] == "no telemetry"
    assert {e["harness"]: e["state"] for e in usage["accounts"]} == {
        "claude": "no telemetry", "codex": "no telemetry", "cursor": "not tracked"}


def test_a_reset_or_old_reading_is_stale_with_no_used_value(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    _with_rows(db, ("five_hour", 0.6, FIVE_HOUR_RESET, CAPTURED), ("seven_day", 0.2, SEVEN_DAY_RESET, CAPTURED))
    with sqlite3.connect(db) as connection:
        reset = _accounts(read_usage(connection, FIVE_HOUR_RESET + 1, BOX))["claude"]
        old = _accounts(read_usage(connection, CAPTURED + usage_readings.STALE_SECONDS + 1, BOX))["claude"]
    assert reset["five_hour"]["state"] == "stale" and reset["five_hour"]["used"] is None
    assert reset["seven_day"]["state"] == "ok" and reset["headroom"] == 0.8
    assert reset["state"] == "ok"
    assert old["state"] == "stale" and old["headroom"] is None
    assert old["five_hour"]["used"] is None and old["seven_day"]["used"] is None


def test_headroom_is_the_tighter_fresh_window(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    _with_rows(db, ("five_hour", 0.6, FIVE_HOUR_RESET, CAPTURED), ("seven_day", 0.2, SEVEN_DAY_RESET, CAPTURED))
    with sqlite3.connect(db) as connection:
        entry = _accounts(read_usage(connection, CAPTURED + 1, BOX))["claude"]
    assert entry["headroom"] == 0.4


def test_bad_rows_never_reach_the_snapshot(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    with sqlite3.connect(db) as connection:
        usage_readings.ensure_table(connection)
        connection.executemany(
            "INSERT INTO usage_readings (harness, bot, device, window, used, resets_at, observed_at, job_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("claude", "claude-bot", "box", "five_hour", 7.0, FIVE_HOUR_RESET, CAPTURED, "j"),
                ("claude", "claude-bot", "box", "seven_day", 0.2, "soon", CAPTURED, "j"),
                ("claude", "bot with spaces", "box", "five_hour", 0.2, FIVE_HOUR_RESET, CAPTURED, "j"),
                ("other", "x", "box", "five_hour", 0.2, FIVE_HOUR_RESET, CAPTURED, "j"),
            ],
        )
        usage = read_usage(connection, CAPTURED + 1, BOX)
    assert [(e["devices"], e["harness"], e["bot"]) for e in usage["accounts"]] == [
        (["box"], "claude", "claude-bot"), (["box"], "codex", "codex-bot"), (["box"], "cursor", "cursor-bot")]
    claude = usage["accounts"][0]
    assert claude["five_hour"]["used"] is None and claude["five_hour"]["state"] == "no reading"
    assert claude["seven_day"]["state"] == "no reading" and claude["state"] == "no reading"




def _iso(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


def _seven_day_only(at: float) -> dict:
    return {"rate_limits": {"primary": {"used_percent": 68, "window_minutes": 10080, "resets_at": SEVEN_DAY_RESET},
                            "secondary": None}, "timestamp": _iso(at)}


def _codex_polls(db: Path, polls: list[tuple[str, dict, float]], now: float) -> dict:
    with sqlite3.connect(db) as connection:
        for device, event, parsed_at in polls:
            usage_readings.store(connection, harness="codex", bot="codex-bot", device=device, job_id="j",
                                 readings=parse_codex(event, parsed_at))
        return _accounts(read_usage(connection, now, BOX))["codex"]


def _codex_reads(db: Path, events: list[dict], now: float) -> dict:
    return _codex_polls(db, [("box", event, now) for event in events], now)


THREE_READINGS = [_seven_day_only(CAPTURED + offset) for offset in (0, 600, 1200)]


def test_a_window_left_out_of_three_fresh_readings_is_not_on_the_plan(tmp_path: Path) -> None:
    codex = _codex_reads(tmp_path / "orch.db", THREE_READINGS, CAPTURED + 1260)
    assert codex["five_hour"]["state"] == "not on plan" and codex["five_hour"]["used"] is None
    assert codex["seven_day"] == {"used": 0.68, "resets_at": SEVEN_DAY_RESET, "observed_at": CAPTURED + 1200,
                                  "state": "ok"}
    assert codex["headroom"] == 0.32 and codex["state"] == "ok"
    assert set(codex) == {"account", "plan", "label", "source", "bot", "bots", "harness", "devices", "state",
                          "five_hour", "seven_day", "headroom", "observed_at", "age_seconds", "limited_until",
                          "limit_label"}
    assert set(codex["five_hour"]) == set(codex["seven_day"]) == {"used", "resets_at", "observed_at", "state"}


GAP = usage_readings.STALE_SECONDS + 1


@pytest.mark.parametrize(("offsets", "later", "state"), [
    ([0], 60, "no reading"),
    ([0, 600], 60, "no reading"),
    ([0, 0, 0], 60, "no reading"),
    ([0, 600, 1200], GAP, "stale"),
    ([0, 600, 600 + GAP], 60, "no reading"),
])
def test_fewer_than_three_fresh_readings_without_a_window_stay_no_reading(
    tmp_path: Path, offsets: list[int], later: int, state: str
) -> None:
    events = [_seven_day_only(CAPTURED + offset) for offset in offsets]
    codex = _codex_reads(tmp_path / "orch.db", events, CAPTURED + offsets[-1] + later)
    assert codex["five_hour"]["state"] == state and codex["five_hour"]["used"] is None


def test_readings_without_rate_limits_never_say_not_on_the_plan(tmp_path: Path) -> None:
    events = [{"rate_limits": None, "timestamp": _iso(CAPTURED + offset)} for offset in (0, 600, 1200)]
    codex = _codex_reads(tmp_path / "orch.db", events, CAPTURED + 1260)
    assert codex["five_hour"]["state"] == codex["seven_day"]["state"] == "no reading"


def _both_windows(at: float) -> dict:
    return {"rate_limits": {"primary": {"used_percent": 10, "window_minutes": 300, "resets_at": FIVE_HOUR_RESET},
                            "secondary": {"used_percent": 68, "window_minutes": 10080, "resets_at": SEVEN_DAY_RESET}},
            "timestamp": _iso(at)}


def _polled_at_its_time(polls: list[tuple[str, dict, float]]) -> list[tuple[str, dict, float]]:
    return [(device, event(CAPTURED + offset), CAPTURED + offset) for device, event, offset in polls]


def test_three_account_omissions_on_two_devices_are_not_on_the_plan(tmp_path: Path) -> None:
    polls = _polled_at_its_time([("box", _seven_day_only, 0), ("laptop", _seven_day_only, 60),
                                 ("box", _seven_day_only, 120)])
    codex = _codex_polls(tmp_path / "orch.db", polls, CAPTURED + 180)
    assert codex["five_hour"]["state"] == "not on plan"


def test_a_window_reported_on_another_device_interrupts_the_account_streak(tmp_path: Path) -> None:
    polls = _polled_at_its_time([("box", _seven_day_only, 0), ("box", _seven_day_only, 60),
                                 ("laptop", _both_windows, 120), ("box", _seven_day_only, 180)])
    codex = _codex_polls(tmp_path / "orch.db", polls, CAPTURED + 240)
    assert codex["five_hour"]["state"] == "no reading"


def test_a_late_reading_that_reports_the_window_interrupts_newer_omissions(tmp_path: Path) -> None:
    polls = _polled_at_its_time([("box", _seven_day_only, 0), ("box", _seven_day_only, 60),
                                 ("box", _seven_day_only, 120)])
    db = tmp_path / "orch.db"
    assert _codex_polls(db, polls, CAPTURED + 130)["five_hour"]["state"] == "not on plan"
    codex = _codex_polls(db, [("laptop", _both_windows(CAPTURED + 90), CAPTURED + 130)], CAPTURED + 130)
    assert codex["five_hour"]["state"] == "no reading"


def test_an_omission_older_than_the_accounts_latest_reading_does_not_extend_the_streak(tmp_path: Path) -> None:
    polls = [*_polled_at_its_time([("box", _seven_day_only, 0), ("box", _seven_day_only, 60),
                                   ("laptop", _both_windows, 100)]),
             ("laptop", _seven_day_only(CAPTURED + 200), CAPTURED + 110),
             ("box", _seven_day_only(CAPTURED + 150), CAPTURED + 150)]
    codex = _codex_polls(tmp_path / "orch.db", polls, CAPTURED + 160)
    assert codex["five_hour"]["state"] == "no reading"


def test_one_future_stamped_event_polled_three_times_is_one_omission(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    event = _seven_day_only(CAPTURED + 120)
    for parsed_at in (CAPTURED, CAPTURED + 15, CAPTURED + 30):
        codex = _codex_polls(db, [("box", event, parsed_at)], parsed_at)
        assert codex["five_hour"]["state"] == "no reading", parsed_at


@pytest.mark.parametrize("replies", [3, 5])
def test_a_claude_window_null_in_every_reply_stays_no_reading(tmp_path: Path, replies: int) -> None:
    db = tmp_path / "orch.db"
    limits = {"five_hour": None, "seven_day": {"utilization": 24, "resets_at": "2026-11-20T20:00:00+00:00"}}
    with sqlite3.connect(db) as connection:
        for offset in range(0, 600 * replies, 600):
            usage_readings.store(connection, harness="claude", bot="claude-bot", device="box", job_id="j",
                                 readings=parse_get_usage(reply(limits), CAPTURED + offset))
        claude = _accounts(read_usage(connection, CAPTURED + 600 * replies, BOX))["claude"]
    assert claude["five_hour"] == {
        "used": None, "resets_at": None, "observed_at": CAPTURED + 600 * (replies - 1), "state": "no reading"}
    assert claude["seven_day"]["used"] == 0.24 and claude["headroom"] == 0.76


def test_a_claude_streak_stored_before_the_fix_reads_as_no_reading(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    with sqlite3.connect(db) as connection:
        usage_readings.ensure_table(connection)
        connection.executemany(
            "INSERT INTO usage_readings (harness, bot, device, window, used, resets_at, observed_at, job_id, absent)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(harness, f"{harness}-bot", "box", "five_hour", None, None, CAPTURED, "j", 3)
             for harness in ("claude", "codex")])
        accounts = _accounts(read_usage(connection, CAPTURED + 60, BOX))
    assert accounts["claude"]["five_hour"]["state"] == "no reading"
    assert accounts["codex"]["five_hour"]["state"] == "not on plan"


def test_a_late_reading_that_reported_the_window_before_three_newer_omissions_keeps_not_on_the_plan(
    tmp_path: Path,
) -> None:
    polls = [*_polled_at_its_time([("box", _seven_day_only, offset) for offset in (0, 60, 120, 180)]),
             ("laptop", _both_windows(CAPTURED + 30), CAPTURED + 190)]
    codex = _codex_polls(tmp_path / "orch.db", polls, CAPTURED + 190)
    assert codex["five_hour"]["state"] == "not on plan"


def test_an_earlier_polled_omission_never_hides_a_newer_reading_of_the_window(tmp_path: Path) -> None:
    polls = [*_polled_at_its_time([("box", _seven_day_only, 0), ("box", _seven_day_only, 60)]),
             ("laptop", _both_windows(CAPTURED + 120), CAPTURED + 100),
             ("laptop", _seven_day_only(CAPTURED + 110), CAPTURED + 110),
             *_polled_at_its_time([("box", _seven_day_only, 130), ("box", _seven_day_only, 140)])]
    codex = _codex_polls(tmp_path / "orch.db", polls, CAPTURED + 150)
    assert codex["five_hour"]["state"] == "no reading"


def _in_source_order(events: list[tuple[str, bool, int]]) -> str:
    newest = sorted(events, key=lambda event: event[2], reverse=True)
    if newest[0][1]:
        return "ok"
    run = next((i for i, (_, reported, _) in enumerate(newest) if reported), len(newest))
    return "not on plan" if run >= usage_readings.NOT_ON_PLAN_READINGS else "no reading"


@pytest.mark.parametrize("events", [
    [("box", False, 0), ("laptop", True, 30), ("box", False, 60), ("box", False, 120), ("box", False, 180)],
    [("box", False, 0), ("box", False, 60), ("laptop", True, 120), ("laptop", False, 110), ("box", False, 130),
     ("box", False, 140)],
    [("box", False, 0), ("laptop", True, 50), ("box", False, 100), ("laptop", False, 150), ("box", False, 200)],
])
def test_the_account_streak_does_not_depend_on_delivery_order(events: list[tuple[str, bool, int]]) -> None:
    expected = _in_source_order(events)
    now = CAPTURED + max(offset for *_, offset in events) + 10
    for order in itertools.permutations(events):
        with closing(sqlite3.connect(":memory:")) as connection:
            for device, reported, offset in order:
                event = (_both_windows if reported else _seven_day_only)(CAPTURED + offset)
                usage_readings.store(connection, harness="codex", bot="codex-bot", device=device, job_id="j",
                                     readings=parse_codex(event, now))
            codex = _accounts(read_usage(connection, now, BOX))["codex"]
        assert codex["five_hour"]["state"] == expected, order


OLD_DDL = (
    "CREATE TABLE usage_readings (harness TEXT NOT NULL, bot TEXT NOT NULL, device TEXT NOT NULL,"
    " window TEXT NOT NULL, used REAL, resets_at INTEGER, observed_at REAL NOT NULL, job_id TEXT NOT NULL,"
    " PRIMARY KEY (harness, bot, device, window))"
)


def test_an_older_table_gets_the_absent_count_on_store(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    with sqlite3.connect(db) as connection:
        connection.execute(OLD_DDL)
    codex = _codex_reads(db, THREE_READINGS, CAPTURED + 1260)
    assert codex["five_hour"]["state"] == "not on plan"


@pytest.mark.parametrize("omissions_first", [0, 1])
def test_a_report_kept_in_an_older_table_interrupts_the_omissions_stored_after_migration(
    tmp_path: Path, omissions_first: int
) -> None:
    db = tmp_path / "orch.db"
    omissions = [("box", _seven_day_only(CAPTURED + offset), CAPTURED + 610) for offset in (600, 100, 200)]
    with sqlite3.connect(db) as connection:
        connection.execute(OLD_DDL)
    _codex_polls(db, omissions[:omissions_first], CAPTURED + 610)
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO usage_readings"
            " (harness, bot, device, window, used, resets_at, observed_at, job_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("codex", "codex-bot", "laptop", "five_hour", 0.1, FIVE_HOUR_RESET, CAPTURED + 500, "j"))
    codex = _codex_polls(db, omissions[omissions_first:], CAPTURED + 610)
    assert codex["five_hour"]["state"] == "no reading"


HISTORICAL_UPSERT = (
    "INSERT INTO usage_readings (harness, bot, device, window, used, resets_at, observed_at, job_id)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    " ON CONFLICT (harness, bot, device, window) DO UPDATE SET used = excluded.used,"
    " resets_at = excluded.resets_at, observed_at = excluded.observed_at, job_id = excluded.job_id"
    " WHERE excluded.observed_at > usage_readings.observed_at"
)


def _historical_report(db: Path, device: str, at: float) -> None:
    with sqlite3.connect(db) as connection:
        connection.executemany(HISTORICAL_UPSERT, [
            ("codex", "codex-bot", device, "five_hour", 0.1, FIVE_HOUR_RESET, at, "j"),
            ("codex", "codex-bot", device, "seven_day", 0.68, SEVEN_DAY_RESET, at, "j"),
        ])


def test_a_historical_writers_report_over_a_migrated_omission_interrupts_the_streak(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    with sqlite3.connect(db) as connection:
        connection.execute(OLD_DDL)
    _historical_report(db, "laptop", CAPTURED)
    _codex_polls(db, [("laptop", _seven_day_only(CAPTURED + 100), CAPTURED + 100),
                      ("box", _seven_day_only(CAPTURED + 200), CAPTURED + 200)], CAPTURED + 200)
    _historical_report(db, "laptop", CAPTURED + 500)
    polls = _polled_at_its_time([("box", _seven_day_only, 600), ("box", _seven_day_only, 700)])
    assert _codex_polls(db, polls, CAPTURED + 710)["five_hour"]["state"] == "no reading"
    third = _polled_at_its_time([("box", _seven_day_only, 800)])
    assert _codex_polls(db, third, CAPTURED + 810)["five_hour"]["state"] == "not on plan"


def test_an_older_table_read_only_is_no_reading_not_no_telemetry(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    with sqlite3.connect(db) as connection:
        connection.execute(OLD_DDL)
        connection.execute(
            "INSERT INTO usage_readings VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("codex", "codex-bot", "box", "five_hour", None, None, CAPTURED, "j"))
        usage = read_usage(connection, CAPTURED + 60, BOX)
    assert usage["state"] == "ok"
    codex = _accounts(usage)["codex"]
    assert codex["state"] == "no reading" and codex["five_hour"]["state"] == "no reading"


PRE_ACCOUNT_DDL = (
    "CREATE TABLE usage_readings (harness TEXT NOT NULL, bot TEXT NOT NULL, device TEXT NOT NULL,"
    " window TEXT NOT NULL, used REAL, resets_at INTEGER, observed_at REAL NOT NULL, job_id TEXT NOT NULL,"
    " absent INTEGER NOT NULL DEFAULT 0, source_at REAL, PRIMARY KEY (harness, bot, device, window))"
)
PRE_ACCOUNT_OBSERVATIONS_DDL = (
    "CREATE TABLE usage_observations (harness TEXT NOT NULL, bot TEXT NOT NULL, window TEXT NOT NULL,"
    " source_at REAL NOT NULL, absent INTEGER NOT NULL, PRIMARY KEY (harness, bot, window, source_at))"
)
PRE_ACCOUNT_EVIDENCE_DDL = (
    "CREATE TABLE lane_limit_evidence (harness TEXT NOT NULL, bot TEXT NOT NULL, window TEXT NOT NULL,"
    " observed_at REAL NOT NULL, free_at REAL NOT NULL, PRIMARY KEY (harness, bot, window))"
)
PRE_ACCOUNT_UPSERT = """
INSERT INTO usage_readings (harness, bot, device, window, used, resets_at, observed_at, job_id, absent, source_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (harness, bot, device, window) DO UPDATE SET
    used = excluded.used,
    resets_at = excluded.resets_at,
    observed_at = excluded.observed_at,
    job_id = excluded.job_id,
    absent = excluded.absent,
    source_at = excluded.source_at
WHERE COALESCE(excluded.source_at, excluded.observed_at) > COALESCE(usage_readings.source_at, usage_readings.observed_at)
"""


def _pre_account_db(db: Path) -> None:
    db.parent.mkdir(exist_ok=True)
    with sqlite3.connect(db) as connection:
        for ddl in (PRE_ACCOUNT_DDL, PRE_ACCOUNT_OBSERVATIONS_DDL, PRE_ACCOUNT_EVIDENCE_DDL):
            connection.execute(ddl)
        connection.executemany(PRE_ACCOUNT_UPSERT, [
            ("claude", "claude-bot", "box", "five_hour", 0.4, FIVE_HOUR_RESET, CAPTURED, "j1", 0, CAPTURED),
            ("codex", "codex-bot", "pc", "seven_day", None, None, CAPTURED, "j2", 2, CAPTURED),
        ])
        connection.executemany(
            "INSERT INTO usage_observations VALUES (?, ?, ?, ?, ?)",
            [("codex", "codex-bot", "seven_day", CAPTURED - 60, 1), ("codex", "codex-bot", "seven_day", CAPTURED, 1)],
        )
        connection.execute(
            "INSERT INTO lane_limit_evidence VALUES (?, ?, ?, ?, ?)",
            ("claude", "claude-bot", "five_hour", CAPTURED, CAPTURED),
        )


def _columns(db: Path) -> set[str]:
    with closing(sqlite3.connect(db)) as connection:
        return {row[1] for row in connection.execute("PRAGMA table_info(usage_readings)")}


def _dump(db: Path) -> list[str]:
    with closing(sqlite3.connect(db)) as connection:
        return list(connection.iterdump())


def _migrate(db: Path, backups: Path) -> bool:
    return usage_readings.migrate(db, lambda: sqlite3.connect(db), directory=backups)


def test_the_migration_backs_up_then_names_every_row_and_a_second_run_changes_nothing(tmp_path: Path) -> None:
    db, backups = tmp_path / "state" / "orch.db", tmp_path / "manual"
    _pre_account_db(db)

    assert _migrate(db, backups) is True
    [backup] = list(backups.glob("orch-*-usage-accounts.db"))
    assert "account" not in _columns(backup), "the backup holds the old schema"
    with closing(sqlite3.connect(db)) as connection:
        accounts = connection.execute(
            "SELECT harness, device, account FROM usage_readings ORDER BY harness"
        ).fetchall()
        observations = connection.execute(
            "SELECT account, window, source_at, absent FROM usage_account_observations ORDER BY source_at"
        ).fetchall()
        evidence = connection.execute("SELECT * FROM lane_account_evidence").fetchall()
        limits = connection.execute("SELECT COUNT(*) FROM lane_account_limits").fetchone()
    assert accounts == [("claude", "box", "claude-default"), ("codex", "pc", "codex-default")]
    assert observations == [("codex-default", "seven_day", CAPTURED - 60, 1),
                            ("codex-default", "seven_day", CAPTURED, 1)]
    assert evidence == [("claude-default", "five_hour", CAPTURED, CAPTURED)]
    assert limits == (0,)

    dump = _dump(db)
    assert _migrate(db, backups) is False
    assert list(backups.iterdir()) == [backup], "no second backup"
    assert _dump(db) == dump

    with sqlite3.connect(db) as connection:
        connection.execute(PRE_ACCOUNT_UPSERT, (
            "claude", "claude-bot", "box", "five_hour", 0.7, FIVE_HOUR_RESET, CAPTURED + 60, "j3", 0, CAPTURED + 60))
        connection.execute(PRE_ACCOUNT_UPSERT, (
            "claude", "claude-bot", "pc", "five_hour", 0.1, FIVE_HOUR_RESET, CAPTURED + 60, "j4", 0, CAPTURED + 60))
        rows = connection.execute(
            "SELECT device, used, account FROM usage_readings WHERE harness = 'claude' ORDER BY device"
        ).fetchall()
    assert rows == [("box", 0.7, "claude-default"), ("pc", 0.1, None)], "an older binary's upsert still runs"


def test_a_failed_backup_leaves_the_database_unmigrated(tmp_path: Path) -> None:
    db, backups = tmp_path / "state" / "orch.db", tmp_path / "not-a-directory"
    _pre_account_db(db)
    backups.write_text("")
    dump = _dump(db)
    with pytest.raises(OSError):
        _migrate(db, backups)
    assert _dump(db) == dump


def test_a_database_without_readings_is_backed_up_before_it_gets_the_tables(tmp_path: Path) -> None:
    db, backups = tmp_path / "state" / "orch.db", tmp_path / "manual"
    db.parent.mkdir()
    queue = SQLiteJobQueue(db, clock=lambda: 160.0)
    job = queue.enqueue(JobRequest("owner/repo", "1", "build", "claude", "go", 3600), dedupe_key="j")
    assert _migrate(db, backups) is True
    copies = list(backups.iterdir())
    assert [copy.name.endswith("-usage-accounts.db") for copy in copies] == [True]
    with closing(sqlite3.connect(copies[0])) as connection:
        assert connection.execute("SELECT id FROM jobs").fetchall() == [(job.id,)]
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'usage_readings'"
        ).fetchone() is None
    assert "account" in _columns(db)
    assert _migrate(db, backups) is False
    assert list(backups.iterdir()) == copies


def test_before_the_migration_a_job_writes_the_old_row_shape(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    _pre_account_db(db)
    _store_account(db, "codex-a", "box", ("five_hour", 0.3, FIVE_HOUR_RESET, CAPTURED + 60))
    assert "account" not in _columns(db), "orch job run never adds the column"
    report = _source(db, CAPTURED + 120, {"codex": "codex-default"}).read("codex")
    assert report is not None and report.remaining == pytest.approx(0.7)


def test_orch_usage_migrate_says_whether_it_migrated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from lib import cli

    db = tmp_path / "state" / "orch.db"
    _pre_account_db(db)
    monkeypatch.setenv("ORCH_DB_BACKUP", str(tmp_path / "backup" / "orch.db"))
    assert cli.main(["usage", "migrate", "--db", str(db)]) == 0
    assert cli.main(["usage", "migrate", "--db", str(db)]) == 0
    first, second = capsys.readouterr().out.splitlines()
    assert (first, second) == (f"migrated {db}", f"{db} already migrated")
    assert [path.name.endswith("-usage-accounts.db") for path in (tmp_path / "backup" / "manual").iterdir()] == [True]


# --- no credential file ---------------------------------------------------------------------


def test_capture_and_reads_open_no_file_but_the_database_and_the_jobs_rollout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = codex_home(tmp_path)
    [rollout] = (home / "sessions").rglob("rollout-*.jsonl")
    opened: list[str] = []
    real_open, real_os_open, real_path_open = builtins.open, os.open, Path.open
    test_thread = threading.get_ident()

    def record(path) -> None:
        if threading.get_ident() == test_thread:
            opened.append(str(path))

    def record_open(file, *args, **kwargs):
        record(file)
        return real_open(file, *args, **kwargs)

    def record_os_open(path, *args, **kwargs):
        record(path)
        return real_os_open(path, *args, **kwargs)

    def record_path_open(self, *args, **kwargs):
        record(self)
        return real_path_open(self, *args, **kwargs)

    session = _start(tmp_path, WRAPPER, "codex-bot", {"CODEX_HOME": str(home)})
    session.clock = lambda: CAPTURED
    monkeypatch.setattr(builtins, "open", record_open)
    monkeypatch.setattr(io, "open", record_open)
    monkeypatch.setattr(os, "open", record_os_open)
    monkeypatch.setattr(Path, "open", record_path_open)
    session.consume(io.BytesIO(CODEX_EXEC))
    reaper = threading.Thread(target=child_reaper.reap_orphans)
    reaper.start()
    reaper.join()
    session.finish(0)
    claude = _session(tmp_path / "orch.db")
    claude.add_usage(parse_get_usage(GET_USAGE, CAPTURED))
    claude.finish(0)
    assert _source(tmp_path / "orch.db", CAPTURED + 1).read("codex") is not None
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        read_usage(connection, CAPTURED + 1, BOX)
    assert all(path.startswith(str(tmp_path)) for path in opened), opened
    assert [path for path in opened if path.startswith(str(home))] == [str(rollout)]


@pytest.mark.parametrize("module", ["lib/usage_readings.py", "lib/dashboard_usage.py", "lib/codex_rollout.py"])
def test_usage_modules_import_no_process_or_network_library(module: str) -> None:
    tree = ast.parse((ROOT / module).read_text(encoding="utf-8"))
    typing_only = {id(node) for block in ast.walk(tree)
                   if isinstance(block, ast.If) and ast.unparse(block.test) == "TYPE_CHECKING"
                   for statement in block.body for node in ast.walk(statement)}
    imports = [node for node in ast.walk(tree) if id(node) not in typing_only]
    names = {alias.name.split(".")[0] for node in imports if isinstance(node, ast.Import)
             for alias in node.names}
    names |= {node.module.split(".")[0] for node in imports
              if isinstance(node, ast.ImportFrom) and node.module}
    assert not names & {"subprocess", "urllib", "http", "requests", "socket"}
    if module != "lib/codex_rollout.py":
        assert not names & {"os", "pathlib"}


@pytest.mark.skipif(shutil.which("claude") is None, reason="needs Claude Code on PATH")
@pytest.mark.skipif(os.environ.get("ORCH_LIVE_USAGE") != "1", reason="live probe: ORCH_LIVE_USAGE=1")
def test_live_get_usage_probe_parses() -> None:
    out = usage_probe.claude_local(shutil.which("claude") or "claude", dict(os.environ))
    assert b'"control_response"' in out
    parse_get_usage(out, __import__("time").time())
