"""Tests for E2-3: Gemini CLI harness driver, google model family, accounts, and doctor (#1412)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from lib import (
    account_doctor,
    account_home,
    account_signin,
    factory_md,
    harness_env,
    harnesses,
    lane_limit,
    orch_yml,
    pstack_models,
    pstack_pin,
    queue,
    schema,
    telemetry,
    usage_readings,
    wait,
)
from lib.brief import render_brief
from lib.harnesses import AccountIsolation, HarnessDriver, StreamFlags
from lib.harnesses.gemini import GeminiDriver
from lib.machines_yml import load_machines_yml
from tests.test_brief import job

NOW = 1_800_000_000.0


def _gemini_fp(email: str) -> str:
    return hashlib.sha256(f"gemini\0{email.lower()}".encode()).hexdigest()[:12]


def test_gemini_is_first_class_in_schema_wait_queue_and_registry() -> None:
    assert "gemini" in schema.HARNESSES
    assert wait.OWNING_BOTS["gemini-bot"] == "gemini"
    assert "gemini" in queue.RECIPIENTS
    assert "gemini-bot" in orch_yml.APP_BOTS
    assert ".gemini/skills" in pstack_pin.SKILL_DIRS
    assert pstack_pin.DIR_HARNESS[".gemini/skills"] == "gemini"
    assert "gemini" in harnesses.registered_names()

    driver = harnesses.get("gemini")
    assert isinstance(driver, GeminiDriver)
    assert isinstance(driver, HarnessDriver)
    assert driver.bot_user == "gemini-bot"
    assert driver.skills_dir == ".gemini/skills"
    assert driver.family_for_model("gemini-3.1-pro") == "google"
    assert driver.family_for_model(None) == "google"
    assert harnesses.driver_for_bot("gemini-bot") is driver
    assert driver.account_isolation() == AccountIsolation(
        "GEMINI_CLI_HOME", ("GEMINI_API_KEY", "GOOGLE_API_KEY")
    )
    assert driver.probe_usage({}, NOW) == []
    assert "gemini" in usage_readings.NOT_TRACKED
    assert usage_readings.NOT_TRACKED_REASONS["gemini"]


def test_gemini_account_isolation_scrubs_api_keys_and_sets_cli_home() -> None:
    secret_a = "AIza-SYNTHETIC-GEMINI-KEY"
    secret_b = "AIza-SYNTHETIC-GOOGLE-KEY"
    scrubbed = harness_env.account_environ(
        {
            "PATH": "/usr/bin",
            "GEMINI_API_KEY": secret_a,
            "GOOGLE_API_KEY": secret_b,
            "KEEP": "ok",
        },
        "gemini",
        "/home/gemini-bot/.factory-accounts/gemini-pro",
    )
    assert "GEMINI_API_KEY" not in scrubbed
    assert "GOOGLE_API_KEY" not in scrubbed
    assert scrubbed["GEMINI_CLI_HOME"] == "/home/gemini-bot/.factory-accounts/gemini-pro"
    assert secret_a not in repr(scrubbed)
    assert secret_b not in repr(scrubbed)


@pytest.mark.parametrize(
    ("argv", "bot_harness", "expected"),
    [
        (
            ["gemini", "-p", "hello"],
            "gemini",
            StreamFlags(["gemini", "--output-format", "stream-json", "-p", "hello"], "gemini", True, "gemini"),
        ),
        (
            ["/usr/local/bin/gemini", "--prompt=hi"],
            None,
            StreamFlags(
                ["/usr/local/bin/gemini", "--output-format", "stream-json", "--prompt=hi"],
                "gemini",
                True,
                "gemini",
            ),
        ),
        (
            ["gemini", "-p", "--output-format", "stream-json", "hi"],
            "gemini",
            StreamFlags(
                ["gemini", "-p", "--output-format", "stream-json", "hi"],
                "gemini",
                False,
                "output format already set",
            ),
        ),
        (
            ["gemini", "auth", "status"],
            "gemini",
            StreamFlags(["gemini", "auth", "status"], "gemini", False, "not print mode"),
        ),
        (
            ["/srv/bin/wrap-gemini", "-p", ""],
            "gemini",
            StreamFlags(
                ["/srv/bin/wrap-gemini", "--output-format", "stream-json", "-p", ""],
                "gemini",
                True,
                "gemini wrapper",
            ),
        ),
    ],
)
def test_gemini_stream_flags(argv: list[str], bot_harness: str | None, expected: StreamFlags) -> None:
    assert telemetry.stream_flags(argv, bot_harness) == expected


def test_gemini_summary_parses_stream_events_and_handoff() -> None:
    summary = telemetry.Summary("gemini", lambda value, limit=40: str(value))
    summary.feed({"type": "init", "model": "gemini-3.1-pro"}, NOW)
    summary.feed({"type": "message", "role": "assistant", "content": "working"}, NOW + 1)
    summary.feed(
        {
            "type": "tool_use",
            "tool_id": "g1",
            "tool_name": "run_shell_command",
            "parameters": {"command": "pytest tests/test_a.py"},
        },
        NOW + 2,
    )
    summary.feed(
        {"type": "tool_result", "tool_id": "g1", "status": "success", "output": "5 passed in 0.4s"},
        NOW + 3,
    )
    summary.feed(
        {
            "type": "tool_use",
            "tool_id": "g2",
            "tool_name": "write_file",
            "parameters": {"file_path": "lib/harnesses/gemini.py"},
        },
        NOW + 4,
    )
    summary.feed({"type": "tool_result", "tool_id": "g2", "status": "success", "output": ""}, NOW + 5)
    result_event = {
        "type": "result",
        "status": "success",
        "stats": {"input_tokens": 500, "output_tokens": 120, "cached": 100},
        "response": 'Done.\nORCH-HANDOFF: {"kind": "ready"}',
    }
    summary.feed(result_event, NOW + 6)

    assert summary.model == "gemini-3.1-pro"
    assert summary.turns == 1
    assert summary.commands == 1
    assert summary.test_runs == 1
    assert (summary.tests_passed, summary.tests_failed) == (5, 0)
    assert summary.files_touched == 1
    assert (summary.tokens_in, summary.tokens_cached, summary.tokens_out) == (500, 100, 120)
    assert summary.cli_reason == "success"
    assert telemetry.final_message(result_event) == result_event["response"]


def test_google_family_cross_family_review_and_contradiction_rules(tmp_path: Path) -> None:
    def check_md(builder: str, reviewer: str, b_model: str = "", r_model: str = "") -> list[str]:
        bm = f"\n  model: {b_model}" if b_model else ""
        rm = f"\n  model: {r_model}" if r_model else ""
        p = tmp_path / "factory.md"
        p.write_text(
            f"""---
base_branch: main
tickets: github
builder:
  harness: {builder}{bm}
reviewer:
  harness: {reviewer}{rm}
review:
  rounds:
    low: 1
    high: 3
  verify: none
ship: merge
skills: []
---
""",
            encoding="utf-8",
        )
        _, findings = factory_md.load_factory_md(p)
        return [f.message for f in findings if f.severity == "error"]

    # Same-family google+google is refused
    assert any("same-family reviewer" in m for m in check_md("gemini", "gemini"))
    assert any("same-family reviewer" in m for m in check_md("cursor", "gemini", b_model="gemini-3.1-pro"))
    # Cross-family pairs with anthropic and openai are accepted
    assert check_md("gemini", "claude") == []
    assert check_md("gemini", "codex") == []
    assert check_md("claude", "gemini") == []
    assert check_md("codex", "gemini") == []
    # Contradicting model hint on gemini harness is refused
    assert any("contradicts gemini harness" in m for m in check_md("gemini", "codex", b_model="claude-opus-4-6"))
    # Gemini pstack model map loads cleanly
    assert "gemini" in pstack_models.load_map()


def test_parse_gemini_doctor_variants_and_canary_scrubbing() -> None:
    email = "gemini.canary@example.com"
    secret = "AIza-CANARY-SECRET-888"

    ok_text = account_doctor.parse_gemini("gem-a", "pc", 0, f"\x1b[32m✓\x1b[0m Logged in as {email}\n".encode())
    ok_json = account_doctor.parse_gemini(
        "gem-b",
        "pc",
        0,
        json.dumps({"loggedIn": True, "email": email.upper(), "plan": "ultra"}).encode(),
    )
    ok_google = account_doctor.parse_gemini("gem-c", "pc", 0, b"Authenticated with Google\n")
    api_key = account_doctor.parse_gemini(
        "gem-key", "pc", 0, f"Authenticated via API key {secret}\n".encode()
    )
    logged_out = account_doctor.parse_gemini("gem-out", "pc", 1, b"Not logged in\n")
    garbage = account_doctor.parse_gemini("gem-junk", "pc", 0, f"wat {email} {secret}\n".encode())

    rows = {
        row.account: row
        for row in account_doctor.judge([ok_text, ok_json, ok_google, api_key, logged_out, garbage], NOW)
    }
    assert rows["gem-a"].status == "mismatch"
    assert rows["gem-b"].status == "mismatch"
    assert rows["gem-a"].fingerprint == _gemini_fp(email)
    assert rows["gem-b"].subscription_type == "ultra"
    assert rows["gem-c"].status == "ok"
    assert rows["gem-c"].fingerprint is None
    assert (rows["gem-key"].status, rows["gem-key"].detail) == (
        "signed-out",
        account_doctor.GEMINI_API_KEY_DETAIL,
    )
    assert (rows["gem-out"].status, rows["gem-out"].detail) == ("signed-out", account_doctor.NOT_LOGGED_IN)
    assert (rows["gem-junk"].status, rows["gem-junk"].detail) == ("signed-out", account_doctor.NOT_UNDERSTOOD)

    # Multi-word provider identity strings (e.g. "Logged in as user id 1111") must produce distinct fingerprints
    u1 = account_doctor.parse_gemini("gem-u", "box", 0, b"Logged in as user id 1111\n")
    u2 = account_doctor.parse_gemini("gem-u", "mac", 0, b"Logged in as user id 2222\n")
    split_rows = account_doctor.judge([u1, u2], NOW)
    assert all(r.status == "mismatch" for r in split_rows)
    assert u1.fingerprint != u2.fingerprint

    blob = repr([ok_text, ok_json, ok_google, api_key, logged_out, garbage, rows])
    assert email not in blob
    assert secret not in blob


def test_gemini_not_tracked_reason_surfaces_in_efficiency_and_settings(tmp_path: Path) -> None:
    import sqlite3
    from lib import dashboard_accounts, dashboard_usage
    from lib.dashboard_pages import efficiency, settings
    from lib.machines_yml import Account, Device, MachinesConfig

    config = MachinesConfig(
        (Device("pc", 1, {"gemini": "gemini-bot"}, home="/home/gemini-bot"),),
        accounts=(Account("gemini-ultra", "gemini", plan="ultra", signed_in_on=frozenset({"pc"})),),
    )
    with sqlite3.connect(":memory:") as db:
        db.execute(usage_readings.DDL)
        usage = dashboard_usage.read_usage(db, NOW, config)
        entry = next(e for e in usage["accounts"] if e["account"] == "gemini-ultra")
        assert entry["state"] == "not tracked"
        assert entry["reason"] == usage_readings.NOT_TRACKED_REASONS["gemini"]
        html = efficiency._headroom({"usage": usage, "generated_at_epoch": NOW})
        assert usage_readings.NOT_TRACKED_REASONS["gemini"] in html

        accounts = dashboard_accounts.read_accounts(db, NOW, config)
        acct = next(a for a in accounts if a["id"] == "gemini-ultra")
        assert acct["plan"] == "ultra"
        assert acct["usage_note"] == usage_readings.NOT_TRACKED_REASONS["gemini"]
        facts = dict((k, v) for k, v, _ in settings.account_facts(acct))
        assert facts["plan"] == "ultra"
        assert usage_readings.NOT_TRACKED_REASONS["gemini"] in facts["usage"]


def test_gemini_login_logout_runtime_signout_and_machines_yml(tmp_path: Path) -> None:
    assert (
        account_doctor.login_command("gemini", "/home/gemini-bot/.factory-accounts/gem-a")
        == "env -u GEMINI_API_KEY -u GOOGLE_API_KEY "
        "GEMINI_CLI_HOME=/home/gemini-bot/.factory-accounts/gem-a gemini auth login"
    )
    script = account_doctor.status_script("gemini", [None, "/home/gemini-bot/.factory-accounts/gem-a"])
    assert "gemini auth status" in script
    assert "GEMINI_CLI_HOME=" in script
    assert "GEMINI_API_KEY" in script and "GOOGLE_API_KEY" in script

    logout = account_signin.logout_script("gemini", "/home/gemini-bot/.factory-accounts/gem-a")
    assert "gemini auth logout" in logout
    assert "GEMINI_CLI_HOME=/home/gemini-bot/.factory-accounts/gem-a" in logout

    err = {"type": "error", "message": "Not logged in. Run gemini auth login."}
    assert account_doctor.detect_signout("gemini", err) is True
    assert lane_limit.error_text("gemini", err) == err["message"]

    bot_home = tmp_path / "gemini-home"
    bot_home.mkdir()
    machines = tmp_path / "machines.yml"
    machines.write_text(
        f"""\
orch_host: pc
accounts:
  - {{id: gemini-pro, harness: gemini, signed_in_on: [pc]}}
devices:
  - name: pc
    tailscale_name: pc.example.ts.net
    slot_max: 2
    harnesses: {{gemini: gemini-bot}}
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
    assert config.accounts_on("pc", "gemini") == ("gemini-default", "gemini-pro")
    changes = account_home.prepare(config, "pc", "gemini-bot", bot_home)
    assert any(c.action == "made" and c.path.endswith("gemini-pro") for c in changes)


def test_gemini_pstack_adapter_and_bot_skill_exist_and_verify() -> None:
    assert pstack_pin.verify_pin(pstack_pin.ROOT) == []
    assert (pstack_pin.ROOT / "bots" / "gemini-bot" / "SKILL.md").is_file()
    assert (pstack_pin.ROOT / "config" / "waiter-gemini-bot.example.yml").is_file()
    brief = render_brief(
        job("build", "gemini"),
        bot="gemini-bot",
        context=None,
        factory_root="/home/gemini-bot/factory",
    )
    assert "## Skills" in brief
    assert "`/home/gemini-bot/factory/skills/factory/harness/gemini.md`" in brief
    assert "inherit-parent" in brief
