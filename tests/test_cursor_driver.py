"""Tests for E2-2: Cursor CLI harness driver, account doctor, and pstack adapter (#1411, #1037)."""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from lib import (
    account_doctor,
    harness_env,
    harnesses,
    lane_limit,
    telemetry,
    usage_readings,
)
from lib.brief import render_brief
from lib.harnesses import HarnessDriver, StreamFlags
from lib.harnesses.cursor import CursorDriver
from lib.machines_yml import Account, load_machines_yml
from lib.orch_yml import load_orch_yml
from lib.remote import SSH_UNREACHABLE, Exchanged
from lib.pstack_pin import ROOT, verify_pin
from tests.test_brief import job

NOW = 1_800_000_000.0


def test_cursor_driver_registered_and_satisfies_protocol() -> None:
    assert "cursor" in harnesses.registered_names()
    driver = harnesses.get("cursor")
    assert isinstance(driver, CursorDriver)
    assert isinstance(driver, HarnessDriver)
    assert driver.bot_user == "cursor-bot"
    assert driver.skills_dir == ".cursor/skills"
    assert harnesses.driver_for_bot("cursor-bot") is driver
    assert driver.account_isolation() is None
    assert driver.family_for_model("claude-sonnet-4-5") == "anthropic"
    assert driver.family_for_model("gpt-5.4") == "openai"
    assert driver.family_for_model("gemini-3.1-pro") == "google"
    assert driver.family_for_model("grok-4") == "xai"
    assert driver.family_for_model("auto") is None
    assert driver.family_for_model(None) is None
    assert driver.probe_usage({}, NOW) == []
    assert usage_readings.NOT_TRACKED["cursor"]


def test_a_cursor_account_cannot_run_from_a_folder_of_its_own() -> None:
    assert "cursor" not in harness_env.ACCOUNT_ISOLATION
    with pytest.raises(ValueError):
        harness_env.harness_env("cursor", "/home/cursor-bot/.factory-accounts/cursor-ultra")
    with pytest.raises(ValueError):
        harness_env.account_environ({"PATH": "/usr/bin"}, "cursor", "/home/cursor-bot/.factory-accounts/x")


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


def _status_json(status: str, message: str | None = None, **user: object) -> str:
    reply: dict[str, object] = {"status": status, "isAuthenticated": status == "authenticated"}
    if message is not None:
        reply["message"] = message
    if user:
        reply["userInfo"] = user
    return json.dumps(reply, indent=2)


SIGNED_IN_REPLIES = {
    "email": _status_json("authenticated", email="cursor.canary@example.com", userId=42, teamName="Canary Team"),
    "user-id": _status_json("authenticated", "Logged in (user details not available)", userId="synthetic-user-a"),
    "no-details": _status_json("authenticated", "Logged in (user details not available)"),
    "lookup-failed": _status_json("authenticated", "Logged in (unable to fetch user details)"),
}
SIGNED_OUT_REPLIES = {
    "not-logged-in": (_status_json("unauthenticated", "Not logged in"), account_doctor.NOT_LOGGED_IN),
    "partial": (_status_json("partially-authenticated", "Partially authenticated (missing refresh token)"),
                account_doctor.PARTIAL_LOGIN),
}


def _judged(*probes: tuple[str, str, int, str]) -> list[account_doctor.SignIn]:
    return account_doctor.judge(
        [account_doctor.parse_cursor(account, device, code, reply.encode()) for account, device, code, reply in probes],
        NOW,
    )


@pytest.mark.parametrize("reply", SIGNED_IN_REPLIES.values(), ids=SIGNED_IN_REPLIES.keys())
def test_every_signed_in_status_reply_reads_ok(reply: str) -> None:
    [row] = _judged(("cursor-ultra", "pc", 0, reply))
    assert (row.status, row.detail, row.auth_method) == ("ok", "", "login")


@pytest.mark.parametrize(("reply", "detail"), SIGNED_OUT_REPLIES.values(), ids=SIGNED_OUT_REPLIES.keys())
def test_every_signed_out_status_reply_reads_signed_out(reply: str, detail: str) -> None:
    [row] = _judged(("cursor-ultra", "pc", 0, reply))
    assert (row.status, row.detail) == ("signed-out", detail)


def test_a_status_error_or_unknown_reply_is_inconclusive() -> None:
    error, junk = _judged(
        ("cursor-ultra", "pc", 1, _status_json("error", "Status check error: fetch failed")),
        ("cursor-ultra", "box", 0, "\u2713 Logged in as cursor.canary@example.com\n"),
    )
    assert (error.status, error.detail) == ("signed-out", account_doctor.EXIT_NONZERO)
    assert (junk.status, junk.detail) == ("signed-out", account_doctor.NOT_UNDERSTOOD)


def test_different_user_ids_are_different_sign_ins() -> None:
    a, b = _judged(
        ("cursor-a", "pc", 0, SIGNED_IN_REPLIES["user-id"]),
        ("cursor-b", "pc", 0, SIGNED_IN_REPLIES["user-id"].replace("synthetic-user-a", "synthetic-user-b")),
    )
    assert (a.status, b.status) == ("ok", "ok")
    assert a.fingerprint and b.fingerprint and a.fingerprint != b.fingerprint


def test_one_account_signed_in_as_two_users_is_a_mismatch() -> None:
    box, mac = _judged(
        ("cursor-ultra", "box", 0, SIGNED_IN_REPLIES["user-id"]),
        ("cursor-ultra", "mac", 0, SIGNED_IN_REPLIES["user-id"].replace("synthetic-user-a", "synthetic-user-b")),
    )
    assert (box.status, mac.status) == ("mismatch", "mismatch")
    assert box.detail == account_doctor.SPLIT_IDENTITY.format("mac")


def test_two_accounts_on_one_user_are_a_mismatch_and_the_email_is_never_kept() -> None:
    email = SIGNED_IN_REPLIES["email"]
    a, b = _judged(("cursor-a", "pc", 0, email), ("cursor-b", "box", 0, email))
    assert (a.status, b.status) == ("mismatch", "mismatch")
    assert a.fingerprint == hashlib.sha256(b"cursor\x0042").hexdigest()[:12]
    assert "canary" not in repr((a, b)).lower()


def test_the_login_step_unsets_the_token_variables() -> None:
    assert (
        account_doctor.login_command("cursor", None)
        == "env -u CURSOR_API_KEY -u CURSOR_AUTH_TOKEN -u CURSOR_CONFIG_DIR cursor-agent login"
    )


ULTRA_MACHINES = """\
orch_host: pc
accounts:
  - {id: cursor-ultra, harness: cursor, plan: ultra, label: "Cursor Ultra"}
devices:
""" + "".join(
    f"""\
  - name: {name}
    tailscale_name: {name}.example.ts.net
    slot_max: 2
    harnesses: {{cursor: {{bot: cursor-bot, account: cursor-ultra}}}}
    workdir_root: /home/{{bot}}/work
    ssh_alias: {name}-{{bot}}
    factory_root: /home/{{bot}}/factory
"""
    for name in ("box", "mac", "pc")
)


def _load(tmp_path: Path, text: str) -> tuple[object, list[object]]:
    path = tmp_path / "machines.yml"
    path.write_text(text, encoding="utf-8")
    return load_machines_yml(path, overlay=False)


def test_one_cursor_account_is_each_devices_cursor_login(tmp_path: Path) -> None:
    config, findings = _load(tmp_path, ULTRA_MACHINES)
    assert config is not None and findings == []
    for device in ("box", "mac", "pc"):
        assert config.accounts_on(device, "cursor") == ("cursor-ultra",)
        assert config.account_home(device, "cursor", "cursor-ultra") is None


def test_a_cursor_account_signed_in_from_a_folder_is_refused(tmp_path: Path) -> None:
    text = ULTRA_MACHINES.replace('label: "Cursor Ultra"', 'label: "Cursor Ultra", signed_in_on: [pc]')
    text = text.replace("{bot: cursor-bot, account: cursor-ultra}", "cursor-bot").replace(
        "factory_root: /home/{bot}/factory\n", "factory_root: /home/{bot}/factory\n    home: /home/{bot}\n"
    )
    config, findings = _load(tmp_path, text)
    assert config is None
    assert any("signed_in_on is not supported for a cursor account" in item.message for item in findings)


class CursorTransport:
    def __init__(self) -> None:
        self.scripts: dict[str, str] = {}

    def run(self, argv: object, *, timeout: float) -> int:
        return 0

    def exchange(self, argv: list[str], *, timeout: float, input: bytes = b"") -> Exchanged:
        alias, script = argv[-2], argv[-1]
        self.scripts[alias] = script
        if alias == "mac-cursor-bot":
            return Exchanged(SSH_UNREACHABLE, b"", "")
        reply = SIGNED_IN_REPLIES["user-id"] if alias == "box-cursor-bot" else SIGNED_OUT_REPLIES["not-logged-in"][0]
        return Exchanged(0, b"\nORCH-ACCOUNTS-BEGIN\n" + reply.encode() + b"\nORCH-ACCOUNT 0 0\n", "")


def test_the_doctor_checks_the_account_on_every_device_and_writes_its_rows(tmp_path: Path) -> None:
    config, _ = _load(tmp_path, ULTRA_MACHINES)
    transport = CursorTransport()
    with contextlib.closing(sqlite3.connect(tmp_path / "orch.db", isolation_level=None)) as connection:
        account_doctor.run(connection, config, transport, NOW, None)
        rows = set(connection.execute(f"SELECT account, device, status FROM {account_doctor.TABLE}"))
    assert rows == {
        ("cursor-ultra", "box", "ok"),
        ("cursor-ultra", "mac", "unreachable"),
        ("cursor-ultra", "pc", "signed-out"),
    }
    for script in transport.scripts.values():
        assert "cursor-agent status" in script
        assert "unset " in script and "CURSOR_API_KEY" in script and "CURSOR_AUTH_TOKEN" in script
        assert "CURSOR_CONFIG_DIR=" not in script
        assert "auth.json" not in script and "security " not in script


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


def _proposed_diff() -> str:
    record = (ROOT / "docs" / "decisions" / "1411-cursor-accounts.md").read_text(encoding="utf-8")
    return record.split("```diff\n", 1)[1].split("```", 1)[0]


def test_the_shipped_machines_yml_does_not_declare_the_account_yet() -> None:
    config, _ = load_machines_yml(ROOT / "machines.yml", overlay=False)
    assert config is not None and config.accounts == ()


def test_the_proposed_machines_yml_declares_the_account_on_box_mac_and_pc(tmp_path: Path) -> None:
    (tmp_path / "machines.yml").write_bytes((ROOT / "machines.yml").read_bytes())
    (tmp_path / "proposal.diff").write_text(_proposed_diff(), encoding="utf-8")
    subprocess.run(["git", "apply", "proposal.diff"], cwd=tmp_path, check=True, capture_output=True)
    config, findings = load_machines_yml(tmp_path / "machines.yml", overlay=False)
    assert config is not None
    assert not [item for item in findings if item.severity == "error"]
    assert [item for item in config.accounts if item.harness == "cursor"] == [
        Account("cursor-ultra", "cursor", plan="ultra", label="Cursor Ultra")
    ]
    for device in ("box", "mac", "pc"):
        assert config.accounts_on(device, "cursor") == ("cursor-ultra",)


def test_the_example_declares_the_cursor_login_beside_the_shared_pool() -> None:
    config, findings = load_machines_yml(ROOT / "config" / "machines.example.yml", overlay=False)
    assert config is not None and not [item for item in findings if item.severity == "error"]
    for device in ("box", "pc"):
        assert config.accounts_on(device, "cursor") == ("cursor-ultra",)
    assert config.account("cursor-grok-ultra").harness == "external"


def test_the_decision_record_holds_maxs_yes_and_orch_yml_runs_cursor() -> None:
    record = (ROOT / "docs" / "decisions" / "1411-cursor-accounts.md").read_text(encoding="utf-8")
    decision = " ".join(record.partition("## Build/review harness decision")[2].split())
    assert '"Yes: allow Cursor to build and review, and drop it from refused_harnesses"' in decision
    assert "https://github.com/maxharris1/factory/pull/1556#issuecomment-7165146292" in decision
    shipped, findings = load_orch_yml(ROOT / "orch.yml", allow_placeholders=True)
    assert shipped is not None and not [item for item in findings if item.severity == "error"]
    assert "cursor" not in shipped.refused_harnesses
