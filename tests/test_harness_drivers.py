"""Tests for E2-1: pluggable HarnessDriver protocol and Claude/Codex drivers."""

from __future__ import annotations

import json
import pytest

from lib import harness_env, harnesses, telemetry
from lib.harnesses import AccountIsolation, HarnessDriver, StreamFlags
from lib.harnesses.claude import CLAUDE_BG_WAIT_ENV, CLAUDE_SECURE_STORAGE_ENV, ClaudeDriver
from lib.harnesses.codex import CodexDriver


def test_builtin_claude_and_codex_drivers_are_registered_and_satisfy_protocol() -> None:
    names = harnesses.registered_names()
    assert "claude" in names
    assert "codex" in names
    for name in ("claude", "codex"):
        driver = harnesses.get(name)
        assert isinstance(driver, HarnessDriver)
        assert driver.name == name
        assert harnesses.driver_for_bot(driver.bot_user) is driver


def test_every_registered_driver_names_its_own_stream_event_kinds() -> None:
    for driver in harnesses.all_drivers():
        assert callable(getattr(type(driver), "event_kind", None)), driver.name
    assert harnesses.get("gemini").event_kind({"event": "result", "type": "x"}) == "result"
    assert harnesses.get("claude").event_kind({"event": "result", "type": "x"}) == "x"


def test_unregistered_harness_lookup_fails_cleanly() -> None:
    assert harnesses.find("nonexistent-harness") is None
    assert harnesses.driver_for_bot("ghost-bot") is None
    with pytest.raises(KeyError, match="no harness driver registered"):
        harnesses.get("nonexistent-harness")
    with pytest.raises(ValueError, match="cannot run from an account folder"):
        harness_env.harness_env("nonexistent-harness", home="/tmp/x")


def test_claude_driver_contract() -> None:
    driver = harnesses.get("claude")
    assert isinstance(driver, ClaudeDriver)
    assert driver.bot_user == "claude-bot"
    assert driver.skills_dir == ".claude/skills"
    assert driver.family_for_model("opus") == "anthropic"
    assert driver.family_for_model(None) == "anthropic"
    iso = driver.account_isolation()
    assert iso == AccountIsolation(
        "CLAUDE_CONFIG_DIR",
        ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    )
    assert driver.extra_env() == {CLAUDE_BG_WAIT_ENV: "0"}
    assert CLAUDE_SECURE_STORAGE_ENV in driver.dropped_account_env()
    assert driver.handoff_marker({"type": "result", "result": "ORCH-HANDOFF: ok"}) == "ORCH-HANDOFF: ok"
    assert driver.handoff_marker({"type": "assistant", "message": {}}) is None

    # Usage probing supports both get_usage control replies and stream rate_limit_event dicts.
    now = 1_800_000_000.0
    event = {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "unifiedWindows": {
                "five_hour": {"utilization": 0.25, "resetsAt": int(now) + 3600},
            }
        },
    }
    readings = driver.probe_usage(event, now)
    assert len(readings) == 1
    assert readings[0].window == "five_hour"
    assert readings[0].used == pytest.approx(0.25)

    reply = json.dumps(
        {
            "type": "control_response",
            "response": {
                "request_id": "orch-usage",
                "subtype": "success",
                "response": {
                    "rate_limits_available": True,
                    "rate_limits": {
                        "five_hour": {"utilization": 40, "resets_at": "2027-01-15T12:00:00Z"},
                    },
                },
            },
        }
    ).encode()
    assert driver.probe_usage(reply, 1_800_000_000.0)


def test_codex_driver_contract() -> None:
    driver = harnesses.get("codex")
    assert isinstance(driver, CodexDriver)
    assert driver.bot_user == "codex-bot"
    assert driver.skills_dir == ".agents/skills"
    assert driver.family_for_model("gpt-5") == "openai"
    assert driver.family_for_model(None) == "openai"
    iso = driver.account_isolation()
    assert iso == AccountIsolation("CODEX_HOME", ("OPENAI_API_KEY", "CODEX_API_KEY"))
    assert driver.extra_env() == {}
    assert driver.dropped_account_env() == ()
    assert (
        driver.handoff_marker(
            {"type": "item.completed", "item": {"type": "agent_message", "text": "ORCH-HANDOFF: done"}}
        )
        == "ORCH-HANDOFF: done"
    )
    assert driver.handoff_marker({"type": "turn.completed"}) is None

    now = 1_800_000_000.0
    rollout = {
        "timestamp": "2027-01-15T08:00:00Z",
        "rate_limits": {
            "primary": {"window_minutes": 300, "used_percent": 30, "resets_in_seconds": 3600},
        },
    }
    readings = driver.probe_usage(rollout, now)
    assert any(r.window == "five_hour" and r.used == pytest.approx(0.30) for r in readings)


def test_custom_driver_plugs_into_harness_env_and_telemetry_without_code_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DummyDriver:
        name = "dummy"
        bot_user = "dummy-bot"
        skills_dir = ".dummy/skills"
        end_events = frozenset({"dummy.done"})

        def family_for_model(self, model: str | None) -> str | None:
            return "dummy-family"

        def account_isolation(self) -> AccountIsolation | None:
            return AccountIsolation("DUMMY_HOME", ("DUMMY_API_KEY",))

        def extra_env(self) -> dict[str, str]:
            return {"DUMMY_MODE": "batch"}

        def dropped_account_env(self) -> tuple[str, ...]:
            return ("DUMMY_EXTRA_SECRET",)

        def matches_binary(self, basename: str) -> bool:
            return basename == "dummy-cli"

        def stream_flags(self, argv, bot_harness=None) -> StreamFlags:
            return StreamFlags([*argv, "--stream"], "dummy", True, "dummy")

        def event_kind(self, event) -> object:
            return event.get("type")

        def handles_event(self, kind: object) -> bool:
            return kind == "dummy.done"

        def parse_event(self, summary, kind: str, event: dict, now: float) -> None:
            summary.turns += 1
            summary.cli_reason = "dummy-ok"

        def probe_usage(self, payload: object, now: float) -> list:
            return []

        def handoff_marker(self, event) -> str | None:
            return event.get("final") if event.get("type") == "dummy.done" else None

    monkeypatch.setitem(harnesses._REGISTRY, "dummy", DummyDriver())
    env = harness_env.harness_env("dummy", home="/srv/dummy-acct")
    assert env["DUMMY_HOME"] == "/srv/dummy-acct"
    assert env["DUMMY_MODE"] == "batch"

    scrubbed = harness_env.account_environ(
        {"PATH": "/usr/bin", "DUMMY_API_KEY": "secret", "DUMMY_EXTRA_SECRET": "secret2"},
        "dummy",
        "/srv/dummy-acct",
    )
    assert "DUMMY_API_KEY" not in scrubbed
    assert "DUMMY_EXTRA_SECRET" not in scrubbed
    assert scrubbed["DUMMY_HOME"] == "/srv/dummy-acct"

    flags = telemetry.stream_flags(["dummy-cli", "run"], "dummy")
    assert flags == StreamFlags(["dummy-cli", "run", "--stream"], "dummy", True, "dummy")
    assert telemetry.final_message({"type": "dummy.done", "final": "ORCH-HANDOFF: ok"}) == "ORCH-HANDOFF: ok"

    summary = telemetry.Summary("dummy", lambda v, limit=40: str(v))
    summary.feed({"type": "dummy.done", "final": "ok"}, 100.0)
    assert summary.turns == 1
    assert summary.cli_reason == "dummy-ok"
