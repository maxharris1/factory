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
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lib import codex_rollout, telemetry, usage_probe, usage_readings
from lib.dashboard import collect_snapshot
from lib.dashboard_usage import read_usage
from lib.job_token import PROOF_ENV
from lib.machines_yml import Device, MachinesConfig
from lib.remote import Exchanged, RemoteTarget, ssh_argv
from lib.telemetry import Scrubber, Session, harness_argv
from lib.usage import reserve_check
from lib.usage_readings import Reading, TelemetryUsage, parse_codex, parse_get_usage
from tests.test_dashboard import make_db

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
        Reading("five_hour", 0.125, FIVE_HOUR_RESET, CAPTURED),
        Reading("seven_day", 0.345, SEVEN_DAY_RESET, CAPTURED),
    ]


@pytest.mark.parametrize(
    "out", [UNAVAILABLE, reply({"five_hour": {"utilization": 1, "resets_at": "2026-11-14T19:25:32+00:00"}},
                               available="true")])
def test_a_login_without_limits_gives_no_reading(out: bytes) -> None:
    assert parse_get_usage(out, CAPTURED) == [
        Reading("five_hour", None, None, CAPTURED), Reading("seven_day", None, None, CAPTURED)]


@pytest.mark.parametrize("five_hour", [None, "missing", {"utilization": 3}])
def test_a_null_or_missing_window_is_no_reading_never_zero(five_hour: object) -> None:
    limits = {"seven_day": {"utilization": 24, "resets_at": "2026-11-20T20:00:00+00:00"}}
    if five_hour != "missing":
        limits["five_hour"] = five_hour
    assert parse_get_usage(reply(limits), CAPTURED) == [
        Reading("five_hour", None, None, CAPTURED), Reading("seven_day", 0.24, SEVEN_DAY_RESET, CAPTURED)]


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
    assert parse_get_usage(out, CAPTURED)[0] == Reading("five_hour", None, None, CAPTURED)


# --- Codex: the session rollout ----------------------------------------------------------


def test_the_rollout_reader_returns_only_the_last_rate_limit_numbers(tmp_path: Path) -> None:
    found = codex_rollout.latest(str(codex_home(tmp_path)), THREAD)
    assert found == {
        "rate_limits": {
            "primary": {"used_percent": 12.5, "window_minutes": 300, "resets_at": FIVE_HOUR_RESET},
            "secondary": {"used_percent": 34.5, "window_minutes": 10080, "resets_in_seconds": 531270},
        },
        "timestamp": "2026-11-14T16:25:30.000Z",
    }
    assert "TEST VALUE" not in json.dumps(found)


def test_codex_windows_are_mapped_by_minutes_and_reset_in_seconds_counts_from_the_event(tmp_path: Path) -> None:
    found = codex_rollout.latest(str(codex_home(tmp_path)), THREAD)
    assert parse_codex(found, CAPTURED) == [
        Reading("five_hour", 0.125, FIVE_HOUR_RESET, EMITTED),
        Reading("seven_day", 0.345, SEVEN_DAY_RESET, EMITTED),
    ]
    swapped = {"rate_limits": {"primary": {"used_percent": 34.5, "window_minutes": 10080, "resets_at": SEVEN_DAY_RESET},
                               "secondary": {"used_percent": 1, "window_minutes": 60, "resets_at": FIVE_HOUR_RESET}},
               "timestamp": "2026-11-14T16:25:30Z"}
    assert parse_codex(swapped, CAPTURED) == [
        Reading("five_hour", None, None, EMITTED), Reading("seven_day", 0.345, SEVEN_DAY_RESET, EMITTED)]


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


@pytest.mark.parametrize("last", [TOKEN_COUNT_NULL, TOKEN_COUNT_MISSING])
def test_a_last_token_count_without_rate_limits_is_no_reading_not_an_older_one(tmp_path: Path, last: bytes) -> None:
    found = codex_rollout.latest(str(codex_home(tmp_path, ROLLOUT + last)), THREAD)
    assert found == {"rate_limits": None, "timestamp": "2026-11-14T16:26:00.000Z"}
    assert parse_codex(found, CAPTURED + 60) == [
        Reading("five_hour", None, None, LATER), Reading("seven_day", None, None, LATER)]


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
    assert session.harness == "codex" and "--json" in session.argv
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
        return _bots(read_usage(connection, now, BOX))[("box", "codex")]


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
        entry = _bots(read_usage(connection, CAPTURED + 660, BOX))[("box", "claude")]
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

    assert usage_probe.claude_local("claude", {}, run=slow) == b""


def test_only_a_claude_command_is_probed() -> None:
    assert usage_probe.claude_binary(["/usr/local/bin/claude", "-p"]) == "/usr/local/bin/claude"
    assert usage_probe.claude_binary(["fake-harness", "--from-config"]) is None
    assert usage_probe.claude_binary([]) is None


def test_the_remote_probe_runs_claude_under_the_bots_login_shell() -> None:
    transport = FakeTransport(Exchanged(0, GET_USAGE, ""))
    assert usage_probe.claude_remote(transport, TARGET) == GET_USAGE
    [(argv, data)] = transport.calls
    assert argv == ssh_argv(TARGET, usage_probe.claude_remote_script())
    assert argv[-1].startswith("exec bash -lc ") and data == usage_probe.REQUEST
    assert usage_probe.claude_remote(FakeTransport(Exchanged(255, GET_USAGE, "down")), TARGET) == b""


def test_a_remote_codex_read_sends_the_reader_and_parses_its_line(tmp_path: Path) -> None:
    found = codex_rollout.latest(str(codex_home(tmp_path)), THREAD)
    transport = FakeTransport(Exchanged(0, json.dumps(found).encode() + b"\n", ""))
    readings = usage_probe.remote_codex(transport, TARGET, "codex", THREAD, CAPTURED)
    assert [r.window for r in readings] == ["five_hour", "seven_day"]
    [(argv, _data)] = transport.calls
    assert THREAD in argv[-1] and "def latest" in argv[-1]
    assert usage_probe.remote_codex(transport, TARGET, "codex", None, CAPTURED) == []
    assert usage_probe.remote_codex(transport, TARGET, "claude", THREAD, CAPTURED) == []
    assert usage_probe.remote_codex(transport, TARGET, "codex", "../x", CAPTURED) == []
    assert usage_probe.remote_codex(FakeTransport(Exchanged(0, b"garbage", "")), TARGET, "codex", THREAD, CAPTURED) == []


def test_the_claude_probe_thread_hands_its_readings_to_the_session(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    session = _session(db)
    usage_probe.start_claude(session.add_usage, lambda: CAPTURED, lambda: GET_USAGE).join(5)
    usage_probe.start_claude(session.add_usage, lambda: CAPTURED, lambda: 1 / 0).join(5)
    session.finish(0)
    assert [row[3:5] for row in _stored(db)] == [("five_hour", 0.125), ("seven_day", 0.345)]


# --- storing on the telemetry flush ------------------------------------------------------


def _session(db: Path, *, job_id: str = "job-1", device: str = "box", at: float = CAPTURED) -> Session:
    return Session(
        connect=lambda: sqlite3.connect(db), job_id=job_id, bot="claude-bot", repo="acme/widgets",
        harness="claude", argv=["claude"], raw_log=None, scrub=Scrubber({}), log=lambda _m: None,
        stdout=io.BytesIO(), clock=lambda: at, device=device,
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


def _source(db: Path, now: float) -> TelemetryUsage:
    bots = {"claude": "claude-bot", "codex": "codex-bot"}
    return TelemetryUsage(lambda: sqlite3.connect(db), bot_for=bots.get, device="box", clock=lambda: now)


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


# --- the snapshot ------------------------------------------------------------------------


def _bots(usage: dict) -> dict[tuple[str, str], dict]:
    return {(entry["device"], entry["harness"]): entry for entry in usage["bots"]}


def test_snapshot_usage_has_one_entry_per_bot_account(tmp_path: Path) -> None:
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
    bots = _bots(usage)
    assert set(bots) == {("box", "claude"), ("box", "codex"), ("box", "cursor")}
    assert bots[("box", "claude")] == {
        "bot": "claude-bot",
        "harness": "claude",
        "device": "box",
        "state": "ok",
        "five_hour": {"used": 0.125, "resets_at": FIVE_HOUR_RESET, "observed_at": CAPTURED, "state": "ok"},
        "seven_day": {"used": 0.345, "resets_at": SEVEN_DAY_RESET, "observed_at": CAPTURED, "state": "ok"},
        "headroom": 0.655,
        "observed_at": CAPTURED,
        "age_seconds": 120,
        "limited_until": None,
        "limit_label": None,
    }
    assert bots[("box", "codex")]["state"] == "no reading"
    assert bots[("box", "codex")]["headroom"] is None
    assert bots[("box", "cursor")]["state"] == "not tracked"
    json.dumps(snapshot, allow_nan=False)


def test_a_database_without_the_table_reads_as_no_telemetry(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "orch.db")
    usage = read_usage(connection, CAPTURED, BOX)
    assert usage["state"] == "no telemetry"
    assert {e["harness"]: e["state"] for e in usage["bots"]} == {
        "claude": "no telemetry", "codex": "no telemetry", "cursor": "not tracked"}


def test_a_reset_or_old_reading_is_stale_with_no_used_value(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    _with_rows(db, ("five_hour", 0.6, FIVE_HOUR_RESET, CAPTURED), ("seven_day", 0.2, SEVEN_DAY_RESET, CAPTURED))
    with sqlite3.connect(db) as connection:
        reset = _bots(read_usage(connection, FIVE_HOUR_RESET + 1, BOX))[("box", "claude")]
        old = _bots(read_usage(connection, CAPTURED + usage_readings.STALE_SECONDS + 1, BOX))[("box", "claude")]
    assert reset["five_hour"]["state"] == "stale" and reset["five_hour"]["used"] is None
    assert reset["seven_day"]["state"] == "ok" and reset["headroom"] == 0.8
    assert reset["state"] == "ok"
    assert old["state"] == "stale" and old["headroom"] is None
    assert old["five_hour"]["used"] is None and old["seven_day"]["used"] is None


def test_headroom_is_the_tighter_fresh_window(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    _with_rows(db, ("five_hour", 0.6, FIVE_HOUR_RESET, CAPTURED), ("seven_day", 0.2, SEVEN_DAY_RESET, CAPTURED))
    with sqlite3.connect(db) as connection:
        entry = _bots(read_usage(connection, CAPTURED + 1, BOX))[("box", "claude")]
    assert entry["headroom"] == 0.4


def test_bad_rows_never_reach_the_snapshot(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    with sqlite3.connect(db) as connection:
        usage_readings.ensure_table(connection)
        connection.executemany(
            "INSERT INTO usage_readings VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("claude", "claude-bot", "box", "five_hour", 7.0, FIVE_HOUR_RESET, CAPTURED, "j"),
                ("claude", "claude-bot", "box", "seven_day", 0.2, "soon", CAPTURED, "j"),
                ("claude", "bot with spaces", "box", "five_hour", 0.2, FIVE_HOUR_RESET, CAPTURED, "j"),
                ("other", "x", "box", "five_hour", 0.2, FIVE_HOUR_RESET, CAPTURED, "j"),
            ],
        )
        usage = read_usage(connection, CAPTURED + 1, BOX)
    assert [(e["device"], e["harness"], e["bot"]) for e in usage["bots"]] == [
        ("box", "claude", "claude-bot"), ("box", "codex", "codex-bot"), ("box", "cursor", "cursor-bot")]
    claude = usage["bots"][0]
    assert claude["five_hour"]["used"] is None and claude["five_hour"]["state"] == "no reading"
    assert claude["seven_day"]["state"] == "no reading" and claude["state"] == "no reading"


# --- no credential file ---------------------------------------------------------------------


def test_capture_and_reads_open_no_file_but_the_database_and_the_jobs_rollout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = codex_home(tmp_path)
    [rollout] = (home / "sessions").rglob("rollout-*.jsonl")
    opened: list[str] = []
    real_open, real_os_open, real_path_open = builtins.open, os.open, Path.open

    def record_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    def record_os_open(path, *args, **kwargs):
        opened.append(str(path))
        return real_os_open(path, *args, **kwargs)

    def record_path_open(self, *args, **kwargs):
        opened.append(str(self))
        return real_path_open(self, *args, **kwargs)

    session = _start(tmp_path, WRAPPER, "codex-bot", {"CODEX_HOME": str(home)})
    session.clock = lambda: CAPTURED
    monkeypatch.setattr(builtins, "open", record_open)
    monkeypatch.setattr(io, "open", record_open)
    monkeypatch.setattr(os, "open", record_os_open)
    monkeypatch.setattr(Path, "open", record_path_open)
    session.consume(io.BytesIO(CODEX_EXEC))
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
    names = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
             for alias in node.names}
    names |= {node.module.split(".")[0] for node in ast.walk(tree)
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
