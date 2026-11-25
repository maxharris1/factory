"""Tests for E2-2: Cursor CLI harness driver, account doctor, and pstack adapter (#1411, #1037)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from lib import (
    account_doctor,
    account_home,
    account_signin,
    harness_env,
    harnesses,
    lane_limit,
    telemetry,
    usage_readings,
)
from lib.brief import render_brief
from lib.harnesses import AccountIsolation, HarnessDriver, StreamFlags
from lib.harnesses.cursor import CursorDriver
from lib.machines_yml import load_machines_yml
from lib.pstack_pin import ROOT, verify_pin
from tests.test_brief import job

NOW = 1_800_000_000.0


def _cursor_fp(email: str) -> str:
    return hashlib.sha256(f"cursor\0{email.lower()}".encode()).hexdigest()[:12]


def test_cursor_driver_registered_and_satisfies_protocol() -> None:
    assert "cursor" in harnesses.registered_names()
    driver = harnesses.get("cursor")
    assert isinstance(driver, CursorDriver)
    assert isinstance(driver, HarnessDriver)
    assert driver.bot_user == "cursor-bot"
    assert driver.skills_dir == ".cursor/skills"
    assert harnesses.driver_for_bot("cursor-bot") is driver
    assert driver.account_isolation() == AccountIsolation(
        "CURSOR_CONFIG_DIR", ("CURSOR_API_KEY", "CURSOR_AUTH_TOKEN")
    )
    assert driver.family_for_model("claude-sonnet-4-5") == "anthropic"
    assert driver.family_for_model("gpt-5.4") == "openai"
    assert driver.family_for_model("gemini-3.1-pro") == "google"
    assert driver.family_for_model("grok-4") == "xai"
    assert driver.family_for_model("auto") is None
    assert driver.family_for_model(None) is None
    assert driver.probe_usage({}, NOW) == []
    assert usage_readings.NOT_TRACKED_REASONS["cursor"]


def test_cursor_account_isolation_scrubs_tokens_and_sets_config_dir() -> None:
    scrubbed = harness_env.account_environ(
        {
            "PATH": "/usr/bin",
            "CURSOR_API_KEY": "secret-cursor-key",
            "CURSOR_AUTH_TOKEN": "secret-cursor-tok",
            "KEEP_ME": "1",
        },
        "cursor",
        "/home/cursor-bot/.factory-accounts/cursor-pro",
    )
    assert "CURSOR_API_KEY" not in scrubbed
    assert "CURSOR_AUTH_TOKEN" not in scrubbed
    assert scrubbed["CURSOR_CONFIG_DIR"] == "/home/cursor-bot/.factory-accounts/cursor-pro"
    assert scrubbed["KEEP_ME"] == "1"


@pytest.mark.parametrize(
    ("argv", "bot_harness", "expected"),
    [
        (
            ["cursor-agent", "-p", "hello"],
            "cursor",
            StreamFlags(["cursor-agent", "--output-format", "stream-json", "-p", "hello"], "cursor", True, "cursor"),
        ),
        (
            ["/usr/local/bin/agent", "--print", "hi"],
            None,
            StreamFlags(
                ["/usr/local/bin/agent", "--output-format", "stream-json", "--print", "hi"],
                "cursor",
                True,
                "cursor",
            ),
        ),
        (
            ["cursor-agent", "-p", "--output-format", "stream-json", "hi"],
            "cursor",
            StreamFlags(
                ["cursor-agent", "-p", "--output-format", "stream-json", "hi"],
                "cursor",
                False,
                "output format already set",
            ),
        ),
        (
            ["cursor-agent", "status"],
            "cursor",
            StreamFlags(["cursor-agent", "status"], "cursor", False, "not print mode"),
        ),
        (
            ["/srv/bin/wrap-cursor", "-p", "prompt"],
            "cursor",
            StreamFlags(
                ["/srv/bin/wrap-cursor", "--output-format", "stream-json", "-p", "prompt"],
                "cursor",
                True,
                "cursor wrapper",
            ),
        ),
    ],
)
def test_cursor_stream_flags(argv: list[str], bot_harness: str | None, expected: StreamFlags) -> None:
    assert telemetry.stream_flags(argv, bot_harness) == expected


def test_cursor_summary_parses_stream_events_and_handoff() -> None:
    summary = telemetry.Summary("cursor", lambda value, limit=40: str(value))
    summary.feed({"type": "system", "subtype": "init", "model": "claude-sonnet-4-5"}, NOW)
    summary.feed({"type": "thinking", "text": "planning"}, NOW + 1)
    summary.feed(
        {
            "type": "tool_call",
            "subtype": "started",
            "call_id": "c1",
            "tool_call": {"shellToolCall": {"args": {"command": "pytest tests/test_a.py"}}},
        },
        NOW + 2,
    )
    summary.feed(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "c1",
            "tool_call": {
                "shellToolCall": {"result": {"success": {"exitCode": 0, "stdout": "3 passed in 0.2s"}}}
            },
        },
        NOW + 3,
    )
    summary.feed(
        {
            "type": "tool_call",
            "subtype": "started",
            "call_id": "c2",
            "tool_call": {"editToolCall": {"args": {"path": "lib/harnesses/cursor.py"}}},
        },
        NOW + 4,
    )
    summary.feed(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "c2",
            "tool_call": {"editToolCall": {"result": {"success": {}}}},
        },
        NOW + 5,
    )
    result_event = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 2,
        "total_cost_usd": 0.12,
        "usage": {"input_tokens": 100, "output_tokens": 40, "cache_read_input_tokens": 20},
        "result": "Done.\nORCH-HANDOFF: {\"kind\": \"ready\"}",
    }
    summary.feed(result_event, NOW + 6)

    assert summary.model == "claude-sonnet-4-5"
    assert summary.commands == 1
    assert summary.test_runs == 1
    assert (summary.tests_passed, summary.tests_failed) == (3, 0)
    assert summary.files_touched == 1
    assert summary.turns == 2
    assert summary.tokens_in == 120
    assert summary.tokens_out == 40
    assert summary.cli_reason == "success"
    assert telemetry.final_message(result_event) == result_event["result"]


def test_cursor_runtime_signout_and_limit_detection() -> None:
    err = {"type": "result", "is_error": True, "result": "Not logged in. Run cursor-agent login."}
    assert account_doctor.detect_signout("cursor", err) is True
    assert lane_limit.error_text("cursor", err) == err["result"]
    limit_event = {"type": "result", "is_error": True, "result": "Usage limit reached. Try again at Jan 2 at 3pm."}
    assert lane_limit.detect("cursor", limit_event, NOW) is not None


def test_parse_cursor_doctor_variants_and_canary_scrubbing() -> None:
    email = "cursor.canary@example.com"
    secret = "cur-key-SECRET-999"

    ok_text = account_doctor.parse_cursor("cur-a", "pc", 0, f"\x1b[32m✓\x1b[0m Logged in as {email}\n".encode())
    ok_json = account_doctor.parse_cursor(
        "cur-b",
        "pc",
        0,
        json.dumps({"loggedIn": True, "email": email.upper(), "subscriptionType": "ultra"}).encode(),
    )
    ok_no_email = account_doctor.parse_cursor("cur-c", "pc", 0, b"Logged in\n")
    api_key = account_doctor.parse_cursor(
        "cur-key", "pc", 0, f"Logged in using an API key {secret}\n".encode()
    )
    logged_out = account_doctor.parse_cursor("cur-out", "pc", 1, b"\x1b[31m\xe2\x9c\x97\x1b[0m Not logged in\n")
    garbage = account_doctor.parse_cursor("cur-junk", "pc", 0, f"unexpected {email} {secret}\n".encode())

    rows = {
        row.account: row
        for row in account_doctor.judge([ok_text, ok_json, ok_no_email, api_key, logged_out, garbage], NOW)
    }
    # cur-a and cur-b share the same Cursor email -> both flagged as mismatch via hash, never raw email
    assert rows["cur-a"].status == "mismatch"
    assert rows["cur-b"].status == "mismatch"
    assert rows["cur-a"].fingerprint == _cursor_fp(email)
    assert rows["cur-b"].subscription_type == "ultra"
    assert rows["cur-c"].status == "ok"
    assert rows["cur-c"].fingerprint is None
    assert (rows["cur-key"].status, rows["cur-key"].detail) == ("signed-out", account_doctor.CURSOR_API_KEY)
    assert (rows["cur-out"].status, rows["cur-out"].detail) == ("signed-out", account_doctor.NOT_LOGGED_IN)
    assert (rows["cur-junk"].status, rows["cur-junk"].detail) == ("signed-out", account_doctor.NOT_UNDERSTOOD)

    blob = repr([ok_text, ok_json, ok_no_email, api_key, logged_out, garbage, rows])
    assert email not in blob
    assert secret not in blob


def test_cursor_login_logout_commands_and_status_script() -> None:
    assert (
        account_doctor.login_command("cursor", "/home/cursor-bot/.factory-accounts/cur-a")
        == "env -u CURSOR_API_KEY -u CURSOR_AUTH_TOKEN "
        "CURSOR_CONFIG_DIR=/home/cursor-bot/.factory-accounts/cur-a cursor-agent login"
    )
    assert (
        account_doctor.login_command("cursor", None)
        == "env -u CURSOR_API_KEY -u CURSOR_AUTH_TOKEN -u CURSOR_CONFIG_DIR cursor-agent login"
    )
    script = account_doctor.status_script("cursor", [None, "/home/cursor-bot/.factory-accounts/cur-a"])
    assert "cursor-agent status" in script
    assert "CURSOR_CONFIG_DIR=" in script
    assert "CURSOR_API_KEY" in script
    assert "CURSOR_AUTH_TOKEN" in script

    logout = account_signin.logout_script("cursor", "/home/cursor-bot/.factory-accounts/cur-a")
    assert "cursor-agent logout" in logout
    assert "CURSOR_CONFIG_DIR=/home/cursor-bot/.factory-accounts/cur-a" in logout


def test_cursor_machines_yml_signed_in_on_and_account_home_prepare(tmp_path: Path) -> None:
    bot_home = tmp_path / "cursor-home"
    bot_home.mkdir()
    machines = tmp_path / "machines.yml"
    machines.write_text(
        f"""\
orch_host: pc
accounts:
  - {{id: cursor-pro, harness: cursor, signed_in_on: [pc]}}
devices:
  - name: pc
    tailscale_name: pc.example.ts.net
    slot_max: 2
    harnesses: {{cursor: cursor-bot}}
    workdir_root: /home/{{bot}}/factory-work
    ssh_alias: pc-{{bot}}
    factory_root: /home/{{bot}}/factory
    home: {bot_home.as_posix()}
""",
        encoding="utf-8",
    )
    config, findings = load_machines_yml(machines, overlay=False)
    assert config is not None
    assert not [f for f in findings if f.severity == "error"]
    assert config.accounts_on("pc", "cursor") == ("cursor-default", "cursor-pro")
    assert config.account_home("pc", "cursor", "cursor-pro") == f"{bot_home.as_posix()}/.factory-accounts/cursor-pro"

    changes = account_home.prepare(config, "pc", "cursor-bot", bot_home)
    assert any(c.action == "made" and c.path.endswith("cursor-pro") for c in changes)
    assert (bot_home / ".factory-accounts" / "cursor-pro").is_dir()


def test_cursor_pstack_adapter_is_pinned_and_rendered_in_brief() -> None:
    assert verify_pin(ROOT) == []
    brief = render_brief(
        job("build", "cursor"),
        bot="cursor-bot",
        context=None,
        factory_root="/home/cursor-bot/factory",
    )
    assert "## Skills" in brief
    assert "`/home/cursor-bot/factory/skills/factory/harness/cursor.md`" in brief
    assert "`skills/factory/harness/cursor.md`" in brief
    assert "inherit-parent" in brief
