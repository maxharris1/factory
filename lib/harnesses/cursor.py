"""Cursor CLI (`cursor-agent` / `agent`) harness driver (E2-2, #1411, #1037)."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any

from lib.harnesses import AccountIsolation, StreamFlags, register

CURSOR_BINARIES = frozenset({"cursor-agent", "agent"})
CURSOR_EVENTS = frozenset({"system", "assistant", "user", "tool_call", "thinking", "result"})

_MODEL_FAMILIES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("claude", "opus", "sonnet", "haiku", "fable"), "anthropic"),
    (("gpt", "codex", "o1", "o3", "o4"), "openai"),
    (("gemini",), "google"),
    (("grok",), "xai"),
)


class CursorDriver:
    name: str = "cursor"
    bot_user: str = "cursor-bot"
    skills_dir: str = ".cursor/skills"
    end_events: frozenset[str] = frozenset({"result"})

    def family_for_model(self, model: str | None) -> str | None:
        if not model:
            return None
        lowered = model.strip().lower()
        for prefixes, family in _MODEL_FAMILIES:
            if any(lowered.startswith(prefix) for prefix in prefixes):
                return family
        return None

    def account_isolation(self) -> AccountIsolation | None:
        return AccountIsolation(
            "CURSOR_CONFIG_DIR",
            ("CURSOR_API_KEY", "CURSOR_AUTH_TOKEN"),
        )

    def extra_env(self) -> Mapping[str, str]:
        return {}

    def dropped_account_env(self) -> tuple[str, ...]:
        return ()

    def matches_binary(self, basename: str) -> bool:
        return basename in CURSOR_BINARIES

    def stream_flags(self, argv: Sequence[str], bot_harness: str | None = None) -> StreamFlags:
        cmd = list(argv)
        if not cmd:
            return StreamFlags(cmd, None, False, "empty command")
        name = os.path.basename(cmd[0])
        rest = cmd[1:]
        wrapper = not self.matches_binary(name)
        harness = None if wrapper else "cursor"
        if wrapper and not (rest and rest[0].startswith("-")):
            return StreamFlags(cmd, None, False, "wrapper does not pass harness options")
        if "-p" not in rest and "--print" not in rest:
            return StreamFlags(cmd, harness, False, "not print mode")
        if any(part == "--output-format" or part.startswith("--output-format=") for part in rest):
            return StreamFlags(cmd, harness, False, "output format already set")
        extra = ["--output-format", "stream-json"]
        return StreamFlags(
            [cmd[0], *extra, *rest],
            "cursor",
            True,
            "cursor wrapper" if wrapper else "cursor",
        )

    def handles_event(self, kind: object) -> bool:
        return isinstance(kind, str) and kind in CURSOR_EVENTS

    def parse_event(self, summary: Any, kind: str, event: dict[str, Any], now: float) -> None:
        summary._cursor(kind, event, now)

    def probe_usage(self, payload: object, now: float) -> list[Any]:
        # Cursor CLI `/usage` is interactive-only; there is no non-interactive
        # quota probe that avoids opening credential files (#1411).
        return []

    def handoff_marker(self, event: Mapping[str, Any]) -> str | None:
        if event.get("type") != "result":
            return None
        text = event.get("result")
        return text if isinstance(text, str) else None


DRIVER = CursorDriver()
register(DRIVER)
