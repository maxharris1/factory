from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import threading
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from lib import cli, lane_limit, schema, self_deploy, usage_readings, usage_refresh
from lib.device_pool import DevicePool
from lib.queue import JobRequest, SQLiteJobQueue
from lib.remote import SSH_UNREACHABLE, Exchanged
from lib.usage_probe import REQUEST
from tests.pool_helpers import poller_adopts
from tests.test_account_doctor import EMAIL_B, ORG_B, SIGNED_OUT_CLAUDE, claude_reply, framed
from tests.test_remote_start import config_of

NOW = 1_800_000_000.0
RESET = "2027-01-15T10:00:00Z"

MACHINES = """\
accounts:
  - {id: claude-b, harness: claude, signed_in_on: [far]}
devices:
  - name: pc
    local: true
    slot_max: 4
    home: /home/{bot}
    harnesses: {claude: claude-bot}
  - name: far
    tailscale_name: far.example.ts.net
    slot_max: 4
    harnesses: {claude: claude-bot, codex: codex-bot, cursor: cursor-bot, gemini: gemini-bot}
    workdir_root: /home/{bot}/work
    ssh_alias: far-{bot}
    factory_root: /srv/factory
    home: /home/{bot}
"""

CURSOR_SIGNED_IN = json.dumps(
    {"status": "authenticated", "isAuthenticated": True, "userInfo": {"userId": 7}}
).encode()
GEMINI_MODELS = b"gemini-3.1-pro  Gemini 3.1 Pro\nORCH-ACCOUNT-STDERR\n"


def get_usage(percent: float) -> bytes:
    window = {"utilization": percent, "resets_at": RESET}
    return json.dumps({"type": "control_response", "response": {
        "subtype": "success", "request_id": "orch-usage",
        "response": {"rate_limits_available": True, "rate_limits": {"five_hour": window, "seven_day": window}},
    }}).encode() + b"\n"


def rollout(percent: float) -> bytes:
    limits = {key: {"used_percent": percent, "window_minutes": minutes, "resets_in_seconds": 7200}
              for key, minutes in (("primary", 300), ("secondary", 10080))}
    return json.dumps({"rate_limits": limits, "timestamp": "2027-01-15T07:00:00Z"}).encode() + b"\n"


def agy_usage(remaining: float) -> bytes:
    buckets = [{"id": f"gemini-{w}", "window": w, "reset_time": RESET, "remaining_fraction": remaining}
               for w in ("5h", "weekly")]
    return b"Warning: a banner\n" + json.dumps({"status": "SUCCESS", "command": {
        "name": "usage", "data": {"groups": [{"name": "Gemini Models", "buckets": buckets}]}}}).encode()


class Transport:
    def __init__(self) -> None:
        self.status: dict[str, bytes | Exchanged] = {
            "far-claude-bot": framed((0, 0, claude_reply()), (1, 0, claude_reply(org=ORG_B, email=EMAIL_B))),
            "far-codex-bot": framed((0, 0, b"Logged in using ChatGPT\n")),
            "far-cursor-bot": framed((0, 0, CURSOR_SIGNED_IN)),
            "far-gemini-bot": framed((0, 0, GEMINI_MODELS)),
        }
        self.usage: dict[tuple[str, str | None], bytes | Exchanged] = {
            ("far-claude-bot", None): get_usage(20),
            ("far-claude-bot", "claude-b"): get_usage(40),
            ("far-codex-bot", None): rollout(30),
            ("far-gemini-bot", None): agy_usage(0.9),
        }
        self.reads: list[tuple[str, str, str | None, bytes]] = []

    def run(self, argv: Sequence[str], *, timeout: float) -> int:
        return 0

    def exchange(self, argv: Sequence[str], *, timeout: float, input: bytes = b"") -> Exchanged:
        alias, script = argv[-2], argv[-1]
        if "ORCH-ACCOUNTS-BEGIN" in script:
            reply = self.status[alias]
        else:
            assert timeout <= usage_refresh.DEADLINE_SECONDS
            found = re.search(r"\.factory-accounts/([\w-]+)", script)
            home = found[1] if found else None
            self.reads.append((argv[argv.index("-l") + 1], alias, home, input))
            reply = self.usage[alias, home]
        return reply if isinstance(reply, Exchanged) else Exchanged(0, reply, "")


@pytest.fixture
def world(tmp_path: Path) -> tuple[SQLiteJobQueue, sqlite3.Connection, Path]:
    path = tmp_path / "machines.yml"
    path.write_text(MACHINES, encoding="utf-8")
    (tmp_path / "state").mkdir()
    queue = SQLiteJobQueue(tmp_path / "state" / "d.db", clock=lambda: NOW)
    usage_readings.migrate(queue.path, queue.connect, directory=tmp_path / "backups")
    with contextlib.closing(queue.connect()) as connection:
        yield queue, connection, path


def refresh(world: tuple[SQLiteJobQueue, sqlite3.Connection, Path], transport: Transport,
            now: float = NOW) -> dict[tuple[str, str], usage_refresh.Result]:
    _, connection, path = world
    results = usage_refresh.run(connection, config_of(path), transport, now, None)
    return {(r.account, r.device): r for r in results}


def stored(connection: sqlite3.Connection) -> dict[tuple[str, str, str], tuple[object, ...]]:
    return {
        (str(row.account), str(row.device), str(row.window)): (row.harness, row.bot, row.used, row.observed_at)
        for row in usage_readings.stored(connection) or []
    }


def test_one_pass_reads_claude_codex_and_gemini_for_the_signed_in_pairs(world) -> None:
    transport = Transport()
    results = refresh(world, transport)

    assert {key: (r.outcome, r.source) for key, r in results.items() if r.outcome == "read"} == {
        ("claude-default", "far"): ("read", "get_usage"),
        ("claude-b", "far"): ("read", "get_usage"),
        ("codex-default", "far"): ("read", "rollout"),
        ("gemini-default", "far"): ("read", "agy /usage"),
    }
    assert sorted(transport.reads, key=str) == sorted([
        ("claude-bot", "far-claude-bot", None, REQUEST),
        ("claude-bot", "far-claude-bot", "claude-b", REQUEST),
        ("codex-bot", "far-codex-bot", None, b""),
        ("gemini-bot", "far-gemini-bot", None, b""),
    ], key=str)
    _, connection, _ = world
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_readings_land_under_their_account_and_device(world) -> None:
    refresh(world, Transport())
    _, connection, _ = world

    rows = stored(connection)
    assert rows[("claude-default", "far", "five_hour")][:3] == ("claude", "claude-bot", 0.2)
    assert rows[("claude-b", "far", "five_hour")][:3] == ("claude", "claude-bot", 0.4)
    assert rows[("codex-default", "far", "seven_day")][:3] == ("codex", "codex-bot", 0.3)
    assert rows[("gemini-default", "far", "five_hour")][:3] == ("gemini", "gemini-bot", pytest.approx(0.1))
    assert {account for account, _, _ in rows} == {"claude-default", "claude-b", "codex-default", "gemini-default"}
    jobs = {job for (job,) in connection.execute(f"SELECT job_id FROM {usage_readings.ACCOUNT_READINGS}")}
    assert jobs == {"orch-usage-refresh"}


def test_a_second_pass_keeps_the_newer_reading(world) -> None:
    _, connection, _ = world
    transport = Transport()
    refresh(world, transport, NOW)
    usage_readings.store(
        connection, harness="claude", bot="claude-bot", device="far", job_id="job-7",
        readings=usage_readings.parse_get_usage(get_usage(60), NOW + 120), account="claude-default",
    )
    connection.commit()

    transport.usage["far-claude-bot", None] = get_usage(10)
    refresh(world, transport, NOW + 60)

    assert stored(connection)[("claude-default", "far", "five_hour")] == ("claude", "claude-bot", 0.6, NOW + 120)
    assert stored(connection)[("claude-b", "far", "five_hour")][3] == NOW + 60


def test_a_signed_out_or_unreachable_pair_is_not_probed(world) -> None:
    transport = Transport()
    transport.status["far-claude-bot"] = framed((0, 0, claude_reply()), (1, 1, SIGNED_OUT_CLAUDE))
    transport.status["far-codex-bot"] = Exchanged(SSH_UNREACHABLE, b"", "down")
    transport.status["far-gemini-bot"] = framed((0, 0, claude_reply()))
    transport.status["far-cursor-bot"] = framed((0, 0, claude_reply(org=ORG_B, email=EMAIL_B)))
    results = refresh(world, transport)

    assert {key: r.outcome for key, r in results.items()} == {
        ("claude-default", "pc"): "unreachable",
        ("claude-default", "far"): "read",
        ("claude-b", "far"): "signed-out",
        ("codex-default", "far"): "unreachable",
        ("cursor-default", "far"): "signed-out",
        ("gemini-default", "far"): "signed-out",
    }
    assert [(alias, home) for _, alias, home, _ in transport.reads] == [("far-claude-bot", None)]


def test_a_mismatched_pair_is_not_probed_and_a_cursor_pair_is_not_tracked(world) -> None:
    transport = Transport()
    transport.status["far-claude-bot"] = framed((0, 0, claude_reply()), (1, 0, claude_reply()))
    results = refresh(world, transport)

    assert results["claude-b", "far"].outcome == "mismatch"
    assert results["claude-default", "far"].outcome == "mismatch"
    assert (results["cursor-default", "far"].outcome, "orch usage record" in results["cursor-default", "far"].detail) \
        == ("not tracked", True)
    assert {alias for _, alias, _, _ in transport.reads} == {"far-codex-bot", "far-gemini-bot"}


def test_a_failed_probe_fails_only_its_own_pair(world) -> None:
    transport = Transport()
    transport.usage["far-claude-bot", "claude-b"] = Exchanged(1, b"", "boom")
    transport.usage["far-codex-bot", None] = Exchanged(SSH_UNREACHABLE, b"", "", timed_out=True)
    transport.usage["far-gemini-bot", None] = b"not json"
    results = refresh(world, transport)

    assert {key: (r.outcome, r.source or r.detail) for key, r in results.items() if r.device == "far"} == {
        ("claude-default", "far"): ("read", "get_usage"),
        ("claude-b", "far"): ("probe failed", "ssh exit 1"),
        ("codex-default", "far"): ("probe failed", "timed out after 30s"),
        ("cursor-default", "far"): ("not tracked", usage_readings.NOT_TRACKED["cursor"]),
        ("gemini-default", "far"): ("probe failed", usage_refresh.NO_READING),
    }
    _, connection, _ = world
    assert {account for account, _, _ in stored(connection)} == {"claude-default"}


def test_a_harness_has_a_reader_or_a_not_tracked_reason() -> None:
    for harness in schema.HARNESSES:
        assert (harness in usage_refresh.READERS) != (harness in usage_readings.NOT_TRACKED), harness


def test_a_usage_held_account_takes_work_after_a_refresh(world) -> None:
    queue, connection, path = world
    poller_adopts(queue, path)
    config = config_of(path)
    for account in ("claude-default", "claude-b"):
        usage_readings.store(
            connection, harness="claude", bot="claude-bot", device="far", job_id="job-1",
            readings=usage_readings.parse_get_usage(get_usage(96), NOW - 60), account=account,
        )
    connection.execute(lane_limit.ACCOUNT_DDL)
    connection.execute(
        f"INSERT INTO {lane_limit.ACCOUNT_TABLE} (account, harness, bot, recipient, limited_at, until, source, job_id)"
        " VALUES ('claude-default', 'claude', 'claude-bot', 'claude', ?, ?, 'usage limit', 'job-1')",
        (NOW - 30, NOW + 7200),
    )
    connection.commit()
    queue.enqueue(JobRequest("owner/repo", "t", "build", "claude", "go-t", 600), dedupe_key="claude:t")
    pool = DevicePool(queue, config)
    assert pool.claim("claude") is None
    assert "claude-default" in lane_limit.account_limits(connection, NOW)

    transport = Transport()
    transport.usage["far-claude-bot", None] = get_usage(1)
    refresh(world, transport)

    assert "claude-default" not in lane_limit.account_limits(connection, NOW)
    claimed = pool.claim("claude")
    assert claimed is not None
    assert claimed[1].account == "claude-default"


def test_a_read_still_running_at_the_deadline_fails_only_its_own_pair(world, monkeypatch) -> None:
    monkeypatch.setattr(usage_refresh, "DEADLINE_SECONDS", 0.2)
    release = threading.Event()

    class Stuck(Transport):
        def exchange(self, argv: Sequence[str], *, timeout: float, input: bytes = b"") -> Exchanged:
            if argv[-2] == "far-codex-bot" and "ORCH-ACCOUNTS-BEGIN" not in argv[-1]:
                release.wait(5)
            return super().exchange(argv, timeout=timeout, input=input)

    try:
        results = refresh(world, Stuck())
    finally:
        release.set()

    assert (results["codex-default", "far"].outcome, results["codex-default", "far"].detail) == (
        "probe failed", "timed out after 0.2s")
    assert results["claude-default", "far"].outcome == "read"
    _, connection, _ = world
    assert "codex-default" not in {account for account, _, _ in stored(connection)}


REFRESHED = [
    "claude-default@pc unreachable 5h - 7d - unreachable: no ssh_alias for claude on pc",
    "claude-default@far ok 5h 20% 7d 20% read now",
    "claude-b@far signed-out 5h 40% 7d 40% 2h old (get_usage); signed-out: not logged in",
    "codex-default@far ok 5h 30% 7d 30% 1h old (rollout)",
    "cursor-default@far ok 5h - 7d - not tracked: " + usage_readings.NOT_TRACKED["cursor"],
    "gemini-default@far ok 5h - 7d - probe failed: ssh exit 1",
    "6 pairs: 2 read, 1 signed-out, 1 unreachable, 1 not tracked, 1 probe failed",
]


def test_orch_usage_refresh_prints_the_pairs_and_creates_no_job_row(world, monkeypatch, capsys) -> None:
    queue, connection, path = world
    transport = Transport()
    transport.status["far-claude-bot"] = framed((0, 0, claude_reply()), (1, 1, SIGNED_OUT_CLAUDE))
    transport.usage["far-gemini-bot", None] = Exchanged(1, b"", "boom")
    usage_readings.store(
        connection, harness="claude", bot="claude-bot", device="far", job_id="job-1",
        readings=usage_readings.parse_get_usage(get_usage(40), NOW - 7200), account="claude-b",
    )
    connection.commit()
    monkeypatch.setattr(cli, "SshTransport", lambda: transport)
    monkeypatch.setattr(time, "time", lambda: NOW)
    monkeypatch.setattr(self_deploy, "own_repo", lambda _root: "owner/factory")
    argv = ["usage", "refresh", "--db", str(queue.path), "--machines", str(path)]

    assert cli.main(argv) == 0
    assert capsys.readouterr().out.splitlines() == REFRESHED
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0

    assert cli.main([*argv, "--json"]) == 0
    pairs = {(p["account"], p["device"]): p for p in json.loads(capsys.readouterr().out)}
    assert len(pairs) == 6
    assert all(set(p) == {"account", "device", "harness", "status", "outcome", "detail",
                          "five_hour", "seven_day", "reading_age", "source"} for p in pairs.values())
    assert {key: pairs["claude-b", "far"][key] for key in ("status", "five_hour", "reading_age", "source")} == {
        "status": "signed-out", "five_hour": 0.4, "reading_age": 7200, "source": "get_usage"}
    assert (pairs["cursor-default", "far"]["status"], pairs["cursor-default", "far"]["five_hour"]) == ("ok", None)


def test_orch_usage_refresh_exit_codes(world, tmp_path: Path, monkeypatch) -> None:
    queue, _, path = world
    monkeypatch.setattr(cli, "SshTransport", Transport)
    db, machines = ["--db", str(queue.path)], ["--machines", str(path)]

    assert cli.main(["usage", "refresh", *db, *machines, "--bogus"]) == 2
    assert cli.main(["usage", "refresh", *db, "--machines", str(tmp_path / "absent.yml")]) == 2
    assert cli.main(["usage", "refresh", "--db", str(tmp_path / "missing-dir" / "d.db"), *machines]) == 1
    assert cli.main(["usage", "refresh", "--db", str(tmp_path), *machines]) == 1


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("doc", ["USAGE", "README.md", "docs/ACCOUNTS.md"])
def test_usage_refresh_is_in_the_help_text(doc: str) -> None:
    text = " ".join((cli.USAGE if doc == "USAGE" else (ROOT / doc).read_text(encoding="utf-8")).split())

    for phrase in ("usage refresh [--db PATH] [--machines PATH] [--json]", "orch usage record",
                   "reads no credential file"):
        assert phrase in text, phrase
