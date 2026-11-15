"""Max's plain slash commands on a PR (#173)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SlashGo:
    instruction: str | None


def parse_slash_go(body: str) -> SlashGo | None:
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines or lines[0].casefold() != "/go" or len(lines) > 2:
        return None
    return SlashGo(instruction=lines[1] if len(lines) == 2 else None)
