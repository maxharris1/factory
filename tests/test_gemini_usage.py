"""Gemini subscription usage: `agy -p /usage --output-format json` (issue #151).

The probe's reply is tests/fixtures/usage/agy-1.5.18-usage.json. No test
runs a real agy: the job-run tests put a fake one on PATH.
"""

from __future__ import annotations

import io
import json
import shlex
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lib import dashboard_usage, harnesses, telemetry, usage_probe, usage_readings
from lib.harnesses.gemini import AUTH_OVERRIDES
from lib.job_token import PROOF_ENV
from lib.machines_yml import Account, Device, MachinesConfig
from lib.queue import JobRequest, SQLiteJobQueue
from lib.remote import TAG_ENV, Exchanged, RemoteTarget, ssh_argv
from lib.usage_readings import Reading
from tests.pool_helpers import pool_claim
from tests.test_telemetry import _orch
from tests.test_usage_readings import FakeTransport

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "usage" / "agy-1.5.18-usage.json"
PAYLOAD = FIXTURE.read_bytes()
NOW = datetime(2026, 11, 26, 16, 0, tzinfo=UTC).timestamp()
FIVE_HOUR_RESET = int(datetime(2026, 11, 26, 17, 1, 47, tzinfo=UTC).timestamp())
SEVEN_DAY_RESET = int(datetime(2026, 12, 3, 7, 1, 47, tzinfo=UTC).timestamp())
DROPPED = (*AUTH_OVERRIDES, "GEMINI_CLI_HOME")
TARGET = RemoteTarget("pc", "pc-wsl-{bot}", "gemini-bot", "/home/{bot}/factory-work", "job-1")


def _probe(payload: object) -> list[Reading]:
    return harnesses.get("gemini").probe_usage(payload, NOW)


def _by_window(readings: list[Reading]) -> dict[str, Reading]:
    return {reading.window: reading for reading in readings}


def _payload(*buckets: dict, group: str = "Gemini Models") -> dict:
    return {"status": "SUCCESS", "num_turns": 0,
            "command": {"name": "usage", "data": {"groups": [{"name": group, "buckets": list(buckets)}]}}}


def _bucket(window: str, fraction: object = 0.5, reset: object = "2026-11-26T17:01:47Z") -> dict:
    bucket = {"id": f"gemini-{window}", "window": window, "reset_time": reset}
    if fraction is not ...:
        bucket["remaining_fraction"] = fraction
    return bucket


# --- the parser ------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [PAYLOAD, json.loads(PAYLOAD)], ids=["bytes", "dict"])
def test_the_gemini_models_group_maps_5h_and_weekly_to_the_two_windows(payload: object) -> None:
    readings = _by_window(_probe(payload))
    assert set(readings) == {"five_hour", "seven_day"}
    five, seven = readings["five_hour"], readings["seven_day"]
    assert five.used == pytest.approx(1 - 0.7233039140701294, abs=1e-6)
    assert seven.used == pytest.approx(1 - 0.8283479809761047, abs=1e-6)
    assert round(five.used, 4) == 0.2767 and round(seven.used, 4) == 0.1717
    assert (five.resets_at, seven.resets_at) == (FIVE_HOUR_RESET, SEVEN_DAY_RESET)
    assert five.observed_at == seven.observed_at == NOW
    assert five.absent == seven.absent == 0


def test_the_claude_and_gpt_group_gives_no_reading() -> None:
    third_party = json.loads(PAYLOAD)
    groups = third_party["command"]["data"]["groups"]
    third_party["command"]["data"]["groups"] = [g for g in groups if g["name"] != "Gemini Models"]
    assert _probe(third_party) == []
    # Its 1970 resets and full fractions never leak into the Gemini readings.
    readings = _probe(PAYLOAD)
    assert all(r.resets_at is not None and r.resets_at > NOW for r in readings)
    assert all(r.used is not None and r.used > 0 for r in readings)


@pytest.mark.parametrize("reset", ["1970-01-01T00:00:00Z", "2099-01-01T00:00:00Z", "not a time",
                                   "2026-11-26T17:01:47", None, 1795712507, ...])
def test_a_reset_time_out_of_horizon_or_invalid_is_dropped(reset: object) -> None:
    bucket = _bucket("5h", 0.75, reset)
    if reset is ...:
        del bucket["reset_time"]
    five = _by_window(_probe(_payload(bucket)))["five_hour"]
    assert five.resets_at is None
    assert five.used == pytest.approx(0.25)


@pytest.mark.parametrize("fraction", [..., None, "0.5", True, False, -0.1, 1.5, float("nan"), float("inf"), [0.5]])
def test_an_invalid_or_missing_remaining_fraction_stays_unset_never_zero(fraction: object) -> None:
    five = _by_window(_probe(_payload(_bucket("5h", fraction))))["five_hour"]
    assert five.used is None
    assert five.resets_at == FIVE_HOUR_RESET


@pytest.mark.parametrize("fraction", [0, 1, 0.0, 1.0])
def test_a_full_or_empty_pool_is_a_reading(fraction: float) -> None:
    five = _by_window(_probe(_payload(_bucket("5h", fraction))))["five_hour"]
    assert five.used == pytest.approx(1 - fraction)


def test_a_window_the_gemini_group_leaves_out_is_an_explicit_no_reading() -> None:
    readings = _by_window(_probe(_payload(_bucket("weekly", 0.5), _bucket("1h", 0.1))))
    assert readings["seven_day"].used == pytest.approx(0.5)
    assert readings["five_hour"] == usage_readings.missing("five_hour", NOW, 0, NOW)
    assert set(readings) == {"five_hour", "seven_day"}


def test_the_bucket_id_maps_the_window_too() -> None:
    bucket = {"id": "gemini-5h", "remaining_fraction": 0.5, "reset_time": "2026-11-26T17:01:47Z"}
    assert _by_window(_probe(_payload(bucket)))["five_hour"].used == pytest.approx(0.5)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not json",
        b"[]",
        {},
        [],
        None,
        {"status": "ERROR", "command": json.loads(PAYLOAD)["command"]},
        {"status": "SUCCESS"},
        {"status": "SUCCESS", "command": {"name": "usage", "data": {"groups": "x"}}},
        {"status": "SUCCESS", "command": {"name": "models", "data": json.loads(PAYLOAD)["command"]["data"]}},
    ],
    ids=["empty", "text", "list-bytes", "empty-dict", "list", "none", "error", "no-command", "bad-groups",
         "other-command"],
)
def test_no_usage_reply_is_no_observation(payload: object) -> None:
    assert _probe(payload) == []


def test_the_reply_may_follow_a_warning_line() -> None:
    assert _by_window(_probe(b"warning: cache is cold\n" + PAYLOAD))["five_hour"].resets_at == FIVE_HOUR_RESET


def test_a_reply_larger_than_the_reply_cap_is_not_read() -> None:
    padded = json.loads(PAYLOAD)
    padded["pad"] = "x" * usage_readings.MAX_REPLY_BYTES
    assert _probe(json.dumps(padded).encode()) == []


# --- tracking flags --------------------------------------------------------------------


def test_gemini_is_tracked_from_agy_usage_and_cursor_still_is_not() -> None:
    assert "gemini" not in usage_readings.NOT_TRACKED
    assert usage_readings.SOURCES["gemini"] == "agy /usage"
    assert "gemini" in dashboard_usage.HARNESSES
    assert usage_readings.NOT_TRACKED["cursor"]


def test_a_device_declared_gemini_account_gets_its_bot_devices_and_readings(tmp_path: Path) -> None:
    config = MachinesConfig(
        (Device("pc", 1, {"gemini": "gemini-bot"}, home="/home/gemini-bot", accounts={"gemini": "gemini-ultra"}),),
        accounts=(Account("gemini-ultra", "gemini", plan="ultra", signed_in_on=frozenset({"pc"})),),
    )
    db = tmp_path / "orch.db"
    session = _session(db, account="gemini-ultra")
    session.add_usage(_probe(PAYLOAD))
    session.finish(0)
    with sqlite3.connect(db) as connection:
        entry = next(e for e in dashboard_usage.read_usage(connection, NOW + 60, config)["accounts"]
                     if e["account"] == "gemini-ultra")
    assert entry["bots"] == ["gemini-bot"] and entry["devices"] == ["pc"]
    assert entry["not_tracked"] is None and entry["source"] == "agy /usage"
    assert entry["state"] == "ok"
    assert entry["five_hour"]["used"] == pytest.approx(0.276696, abs=1e-6)
    assert entry["seven_day"]["resets_at"] == SEVEN_DAY_RESET


def _session(db: Path, *, account: str | None = None) -> telemetry.Session:
    return telemetry.Session(
        connect=lambda: sqlite3.connect(db), job_id="job-gem", bot="gemini-bot", repo="acme/widgets",
        harness="gemini", flags=harnesses.get("gemini").stream_flags(["agy", "-p", "hi"]), raw_log=None,
        scrub=telemetry.Scrubber({}), log=lambda _m: None, stdout=io.BytesIO(), clock=lambda: NOW,
        device="pc", account=account,
    )


# --- the probe -------------------------------------------------------------------------


def test_the_probe_runs_only_for_a_gemini_job() -> None:
    gemini = harnesses.get("gemini").stream_flags(["agy", "-p", "hi"])
    assert usage_probe.gemini_binary(gemini) == "agy"
    assert usage_probe.claude_binary(gemini) is None
    for argv, harness in ((["claude", "-p"], "claude"), (["codex", "exec", "-"], "codex"),
                          (["cursor-agent", "-p"], "cursor"), (["agy", "chat"], "gemini")):
        assert usage_probe.gemini_binary(telemetry.stream_flags(argv, harness)) is None


def test_the_local_probe_runs_agy_usage_with_every_google_override_unset() -> None:
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, PAYLOAD, b"")

    environ = {name: "SYNTHETIC" for name in DROPPED}
    environ.update({PROOF_ENV: "TEST", TAG_ENV: "TAG", "HOME": "/h", "PATH": "/bin"})
    assert usage_probe.gemini_local("/opt/agy/bin/agy", environ, run=run) == PAYLOAD
    [(argv, kwargs)] = calls
    assert argv == ["/opt/agy/bin/agy", "-p", "/usage", "--output-format", "json"]
    assert "--dangerously-skip-permissions" not in argv
    assert kwargs["env"] == {"HOME": "/h", "PATH": "/bin"}
    assert kwargs["input"] == b""

    def slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    with pytest.raises(usage_probe.ProbeFailed, match=r"^timed out after 30s$"):
        usage_probe.gemini_local("agy", {}, run=slow)

    def failed(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, b"", b"")

    with pytest.raises(usage_probe.ProbeFailed, match=r"^exit 1$"):
        usage_probe.gemini_local("agy", {}, run=failed)


def test_the_remote_probe_runs_agy_from_the_bots_login_shell_with_every_override_unset() -> None:
    script = usage_probe.gemini_remote_script()
    assert script.startswith("exec bash -lc ")
    words = shlex.split(script)
    inner = words[3]
    assert all(f"-u {name} " in inner for name in DROPPED)
    assert inner.startswith("cd && exec /usr/bin/env ") and inner.endswith(' agy "$@"')
    assert words[5:] == ["-p", "/usage", "--output-format", "json"]
    transport = FakeTransport(Exchanged(0, PAYLOAD, ""))
    assert usage_probe.gemini_remote(transport, TARGET) == PAYLOAD
    [(argv, data)] = transport.calls
    assert argv == ssh_argv(TARGET, script) and data == b""
    with pytest.raises(usage_probe.ProbeFailed, match=r"^ssh exit 255$"):
        usage_probe.gemini_remote(FakeTransport(Exchanged(255, b"", "down")), TARGET)


def test_the_remote_probe_script_runs_agy_with_the_overrides_dropped(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    agy = bin_dir / "agy"
    agy.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                   "print(json.dumps({'argv': sys.argv[1:], 'env': sorted(os.environ)}))\n", encoding="utf-8")
    agy.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".bash_profile").write_text(f"export PATH={shlex.quote(str(bin_dir))}:$PATH\n", encoding="utf-8")
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", **{name: "SYNTHETIC" for name in DROPPED}}
    words = shlex.split(usage_probe.gemini_remote_script())
    out = subprocess.run(words[1:], env=env, capture_output=True, check=True, timeout=30).stdout
    seen = json.loads(out.strip().splitlines()[-1])
    assert seen["argv"] == ["-p", "/usage", "--output-format", "json"]
    assert not set(DROPPED) & set(seen["env"])


def test_the_probe_thread_hands_its_readings_to_the_session_and_failures_log_one_line(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    session = _session(db)
    lines: list[str] = []
    usage_probe.start_gemini(session.add_usage, lambda: NOW, lambda: PAYLOAD, lines.append).join(5)
    assert lines == []
    usage_probe.start_gemini(session.add_usage, lambda: NOW, lambda: 1 / 0, lines.append).join(5)
    usage_probe.start_gemini(session.add_usage, lambda: NOW, lambda: b"", lines.append).join(5)

    def down() -> bytes:
        raise usage_probe.ProbeFailed("exit 1")

    usage_probe.start_gemini(session.add_usage, lambda: NOW, down, lines.append).join(5)
    assert lines == ["usage probe: failed (ZeroDivisionError)", "usage probe: no reply",
                     "usage probe: failed (exit 1)"]
    session.finish(0)
    with sqlite3.connect(db) as connection:
        rows = connection.execute(
            "SELECT harness, bot, window, used, resets_at FROM usage_readings ORDER BY window").fetchall()
    assert [row[:3] for row in rows] == [("gemini", "gemini-bot", "five_hour"), ("gemini", "gemini-bot", "seven_day")]
    assert [row[4] for row in rows] == [FIVE_HOUR_RESET, SEVEN_DAY_RESET]


# --- orch job run, end to end with a fake agy ------------------------------------------


FAKE_AGY = """#!{python}
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
argv = sys.argv[1:]
if "/usage" in argv:
    with open(os.path.join(here, "usage-calls"), "a", encoding="utf-8") as seen:
        seen.write(json.dumps({{"argv": argv, "env": sorted(os.environ)}}) + "\\n")
    mode = open(os.path.join(here, "usage-mode"), encoding="utf-8").read().strip()
    if mode == "reply":
        reply = json.load(open({fixture!r}, encoding="utf-8"))
        bucket = reply["command"]["data"]["groups"][0]["buckets"]
        import datetime, time
        for entry, seconds in zip(bucket, (86400, 3600)):
            entry["reset_time"] = datetime.datetime.fromtimestamp(
                int(time.time()) + seconds, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(json.dumps(reply))
    sys.exit(1 if mode == "exit" else 0)
for line in open({stream!r}, encoding="utf-8"):
    sys.stdout.write(line)
"""

MACHINES = """\
devices:
  - name: box
    local: true
    slot_max: 3
    harnesses:
      claude: claude-bot
      gemini: gemini-bot
"""


def _gemini_box_run(tmp_path: Path, mode: str) -> tuple[subprocess.CompletedProcess, Path, Path]:
    machines = tmp_path / "machines.yml"
    machines.write_text(MACHINES, encoding="utf-8")
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=time.time)
    queue.enqueue(
        JobRequest(repo="acme/widgets", ticket_id="12", action="build", recipient="gemini",
                   source_comment_id="1", timeout_seconds=600),
        dedupe_key=f"k-{time.monotonic_ns()}",
    )
    job = pool_claim(queue, "gemini", machines)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    agy = bin_dir / "agy"
    agy.write_text(FAKE_AGY.format(python=sys.executable, fixture=str(FIXTURE),
                                   stream=str(ROOT / "tests" / "fixtures" / "telemetry" / "agy_stream.jsonl")),
                   encoding="utf-8")
    agy.chmod(0o755)
    (bin_dir / "usage-mode").write_text(mode, encoding="utf-8")
    run = _orch(
        "job", "run", "gemini-bot", job.id, "--machines", str(machines), "--db", str(queue.path),
        "--interval", "0.2", "--", str(agy), "-p", "hi",
        env={"ORCH_JOB_STATE_DIR": str(tmp_path / "state"), **{name: "SYNTHETIC" for name in DROPPED}},
    )
    return run, bin_dir, queue.path


def _probe_lines(run: subprocess.CompletedProcess) -> list[str]:
    return [line for line in run.stderr.splitlines() if "usage probe:" in line]


def test_a_box_gemini_job_probes_agy_usage_once_and_stores_its_readings(tmp_path: Path) -> None:
    run, bin_dir, db = _gemini_box_run(tmp_path, "reply")
    assert run.returncode == 0, run.stderr
    calls = [json.loads(line) for line in (bin_dir / "usage-calls").read_text(encoding="utf-8").splitlines()]
    assert [call["argv"] for call in calls] == [["-p", "/usage", "--output-format", "json"]]
    assert not set(DROPPED) & set(calls[0]["env"])
    assert _probe_lines(run) == []
    with sqlite3.connect(db) as connection:
        rows = connection.execute(
            "SELECT harness, bot, device, window, round(used, 4) FROM usage_readings ORDER BY window").fetchall()
    assert rows == [("gemini", "gemini-bot", "box", "five_hour", 0.2767),
                    ("gemini", "gemini-bot", "box", "seven_day", 0.1717)]


@pytest.mark.parametrize(("mode", "line"), [("exit", "usage probe: failed (exit 1)"),
                                            ("silent", "usage probe: no reply")])
def test_a_failed_gemini_probe_logs_one_line_and_never_fails_the_job(tmp_path: Path, mode: str, line: str) -> None:
    run, _, db = _gemini_box_run(tmp_path, mode)
    assert run.returncode == 0, run.stderr
    assert _probe_lines(run) == [f"orch job run: {line}"]
    with sqlite3.connect(db) as connection:
        assert "usage_readings" not in usage_readings.tables(connection) or connection.execute(
            "SELECT count(*) FROM usage_readings").fetchone() == (0,)
