"""Google Gemini CLI (`gemini`) harness driver (E2-3, #1412)."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any

from lib.harnesses import AccountIsolation, StreamFlags, register

GEMINI_EVENTS = frozenset({"init", "message", "tool_use", "tool_result", "error", "result"})


class GeminiDriver:
    name: str = "gemini"
    bot_user: str = "gemini-bot"
    skills_dir: str = ".gemini/skills"
    end_events: frozenset[str] = frozenset({"result"})

    def family_for_model(self, model: str | None) -> str | None:
        return "google"

    def account_isolation(self) -> AccountIsolation | None:
        return AccountIsolation(
            "GEMINI_CLI_HOME",
            ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        )

    def extra_env(self) -> Mapping[str, str]:
        return {}

    def dropped_account_env(self) -> tuple[str, ...]:
        return ()

    def matches_binary(self, basename: str) -> bool:
        return basename == "gemini"

    def stream_flags(self, argv: Sequence[str], bot_harness: str | None = None) -> StreamFlags:
        cmd = list(argv)
        if not cmd:
            return StreamFlags(cmd, None, False, "empty command")
        name = os.path.basename(cmd[0])
        rest = cmd[1:]
        wrapper = not self.matches_binary(name)
        harness = None if wrapper else "gemini"
        if wrapper and not (rest and rest[0].startswith("-")):
            return StreamFlags(cmd, None, False, "wrapper does not pass harness options")
        has_prompt = any(
            part in ("-p", "--prompt") or part.startswith("--prompt=")
            for part in rest
        )
        if not has_prompt:
            return StreamFlags(cmd, harness, False, "not print mode")
        if any(
            part in ("-o", "--output-format") or part.startswith("--output-format=")
            for part in rest
        ):
            return StreamFlags(cmd, harness, False, "output format already set")
        extra = ["--output-format", "stream-json"]
        return StreamFlags(
            [cmd[0], *extra, *rest],
            "gemini",
            True,
            "gemini wrapper" if wrapper else "gemini",
        )

    def handles_event(self, kind: object) -> bool:
        return isinstance(kind, str) and kind in GEMINI_EVENTS

    def parse_event(self, summary: Any, kind: str, event: dict[str, Any], now: float) -> None:
        summary._gemini(kind, event, now)

    def probe_usage(self, payload: object, now: float) -> list[Any]:
        # Gemini CLI `/stats model` is interactive REPL-only; there is no
        # non-interactive quota command that avoids reading credential files (#1412).
        return []

    def handoff_marker(self, event: Mapping[str, Any]) -> str | None:
        kind = event.get("type")
        if kind == "result":
            for key in ("response", "result"):
                text = event.get(key)
                if isinstance(text, str):
                    return text
            return None
        if kind == "message" and event.get("role") == "assistant":
            text = event.get("content")
            return text if isinstance(text, str) else None
        return None


DRIVER = GeminiDriver()
register(DRIVER)
