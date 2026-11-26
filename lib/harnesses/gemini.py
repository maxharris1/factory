from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any

from lib.harnesses import AccountIsolation, StreamFlags, register

GEMINI_EVENTS = frozenset({"init", "step_update", "result"})
AUTH_OVERRIDES = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_CLOUD_ACCESS_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_GENAI_USE_GCA",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GEMINI_CLI_USE_COMPUTE_ADC",
    "CLOUD_SHELL",
    "GOOGLE_GEMINI_BASE_URL",
    "GEMINI_CLI_CUSTOM_HEADERS",
)
UNSET = (*AUTH_OVERRIDES, "GEMINI_CLI_HOME")


class GeminiDriver:
    name: str = "gemini"
    bot_user: str = "gemini-bot"
    skills_dir: str = ".gemini/skills"
    end_events: frozenset[str] = frozenset({"result"})

    def family_for_model(self, model: str | None) -> str | None:
        return "google"

    def account_isolation(self) -> AccountIsolation | None:
        # The #1639 live check of agy 1.5.15 found that only a different HOME
        # moves its login, so each device's gemini-bot login is its one account.
        return None

    def extra_env(self) -> Mapping[str, str]:
        return {}

    def dropped_account_env(self) -> tuple[str, ...]:
        return ()

    def matches_binary(self, basename: str) -> bool:
        return basename == "agy"

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
        print_mode = any(
            part in ("-p", "--print", "--prompt") or part.startswith(("--print=", "--prompt="))
            for part in rest
        )
        if not print_mode:
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

    def event_kind(self, event: Mapping[str, Any]) -> object:
        return event.get("event")

    def handles_event(self, kind: object) -> bool:
        return isinstance(kind, str) and kind in GEMINI_EVENTS

    def parse_event(self, summary: Any, kind: str, event: dict[str, Any], now: float) -> None:
        summary._gemini(kind, event, now)

    def probe_usage(self, payload: object, now: float) -> list[Any]:
        """The readings in `agy -p /usage --output-format json`'s reply."""
        from lib import usage_readings

        return usage_readings.parse_agy_usage(payload, now)

    def handoff_marker(self, event: Mapping[str, Any]) -> str | None:
        if event.get("event") != "result":
            return None
        payload = event.get("result")
        text = payload.get("response") if isinstance(payload, dict) else None
        return text if isinstance(text, str) else None


DRIVER = GeminiDriver()
register(DRIVER)
