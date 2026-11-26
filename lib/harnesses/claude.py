"""Claude Code CLI harness driver (E2-1)."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any

from lib.harnesses import AccountIsolation, StreamFlags, register

CLAUDE_BG_WAIT_ENV = "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"
CLAUDE_SECURE_STORAGE_ENV = "CLAUDE_SECURESTORAGE_CONFIG_DIR"
CLAUDE_EVENTS = frozenset({"system", "assistant", "user", "result"})


class ClaudeDriver:
    name: str = "claude"
    bot_user: str = "claude-bot"
    skills_dir: str = ".claude/skills"
    end_events: frozenset[str] = frozenset({"result"})

    def family_for_model(self, model: str | None) -> str | None:
        return "anthropic"

    def account_isolation(self) -> AccountIsolation | None:
        return AccountIsolation(
            "CLAUDE_CONFIG_DIR",
            ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
        )

    def extra_env(self) -> Mapping[str, str]:
        return {CLAUDE_BG_WAIT_ENV: "0"}

    def dropped_account_env(self) -> tuple[str, ...]:
        return (CLAUDE_SECURE_STORAGE_ENV,)

    def matches_binary(self, basename: str) -> bool:
        return basename == "claude"

    def stream_flags(self, argv: Sequence[str], bot_harness: str | None = None) -> StreamFlags:
        cmd = list(argv)
        if not cmd:
            return StreamFlags(cmd, None, False, "empty command")
        name = os.path.basename(cmd[0])
        rest = cmd[1:]
        wrapper = name != "claude"
        harness = None if wrapper else "claude"
        if wrapper and not (rest and rest[0].startswith("-")):
            return StreamFlags(cmd, None, False, "wrapper does not pass harness options")
        if "-p" not in rest and "--print" not in rest:
            return StreamFlags(cmd, harness, False, "not print mode")
        if any(part == "--output-format" or part.startswith("--output-format=") for part in rest):
            return StreamFlags(cmd, harness, False, "output format already set")
        extra = ["--output-format", "stream-json"]
        if "--verbose" not in rest:
            extra.append("--verbose")
        return StreamFlags(
            [cmd[0], *extra, *rest],
            "claude",
            True,
            "claude wrapper" if wrapper else "claude",
        )

    def event_kind(self, event: Mapping[str, Any]) -> object:
        return event.get("type")

    def handles_event(self, kind: object) -> bool:
        return isinstance(kind, str) and kind in CLAUDE_EVENTS

    def parse_event(self, summary: Any, kind: str, event: dict[str, Any], now: float) -> None:
        summary._claude(kind, event, now)

    def probe_usage(self, payload: object, now: float) -> list[Any]:
        from lib import usage_readings

        if isinstance(payload, (bytes, bytearray)):
            return usage_readings.parse_get_usage(bytes(payload), now)
        return usage_readings.parse_rate_limit_event(payload, now)

    def handoff_marker(self, event: Mapping[str, Any]) -> str | None:
        if event.get("type") != "result":
            return None
        text = event.get("result")
        return text if isinstance(text, str) else None


DRIVER = ClaudeDriver()
register(DRIVER)
