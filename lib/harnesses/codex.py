"""OpenAI Codex CLI harness driver (E2-1)."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any

from lib.codex_sandbox import exec_index
from lib.harnesses import AccountIsolation, StreamFlags, register

CODEX_EVENTS = frozenset(
    {
        "thread.started",
        "turn.started",
        "turn.completed",
        "turn.failed",
        "item.started",
        "item.updated",
        "item.completed",
        "error",
    }
)


class CodexDriver:
    name: str = "codex"
    bot_user: str = "codex-bot"
    skills_dir: str = ".agents/skills"
    end_events: frozenset[str] = frozenset({"turn.completed"})

    def family_for_model(self, model: str | None) -> str | None:
        return "openai"

    def account_isolation(self) -> AccountIsolation | None:
        return AccountIsolation("CODEX_HOME", ("OPENAI_API_KEY", "CODEX_API_KEY"))

    def extra_env(self) -> Mapping[str, str]:
        return {}

    def dropped_account_env(self) -> tuple[str, ...]:
        return ()

    def matches_binary(self, basename: str) -> bool:
        return basename == "codex"

    def stream_flags(self, argv: Sequence[str], bot_harness: str | None = None) -> StreamFlags:
        cmd = list(argv)
        if not cmd:
            return StreamFlags(cmd, None, False, "empty command")
        name = os.path.basename(cmd[0])
        rest = cmd[1:]
        harness = "codex" if name == "codex" else None
        index = exec_index(["codex", *rest])
        options = rest[: rest.index("--")] if "--" in rest else rest
        if index is None:
            return StreamFlags(cmd, harness, False, "no exec subcommand")
        if "--json" in options or "--experimental-json" in options:
            return StreamFlags(cmd, harness, False, "json already set")
        reason = "codex" if name == "codex" else "codex wrapper"
        return StreamFlags(
            [*cmd[: index + 1], "--json", *cmd[index + 1 :]],
            "codex",
            True,
            reason,
        )

    def event_kind(self, event: Mapping[str, Any]) -> object:
        return event.get("type")

    def handles_event(self, kind: object) -> bool:
        return isinstance(kind, str) and kind in CODEX_EVENTS

    def parse_event(self, summary: Any, kind: str, event: dict[str, Any], now: float) -> None:
        summary._codex(kind, event, now)

    def probe_usage(self, payload: object, now: float) -> list[Any]:
        from lib import usage_readings

        return usage_readings.parse_codex(payload, now)

    def handoff_marker(self, event: Mapping[str, Any]) -> str | None:
        if event.get("type") != "item.completed":
            return None
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            return text if isinstance(text, str) else None
        return None


DRIVER = CodexDriver()
register(DRIVER)
