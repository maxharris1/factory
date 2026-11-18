"""Max's plain slash commands on a PR (#173)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class SlashGo:
    instruction: str | None


@dataclass(frozen=True)
class SlashQa:
    ruling: Literal["right", "wrong"]
    runner: str | None
    reason: str | None


def parse_slash_go(body: str) -> SlashGo | None:
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines or lines[0].casefold() != "/go" or len(lines) > 2:
        return None
    return SlashGo(instruction=lines[1] if len(lines) == 2 else None)


def parse_slash_qa(body: str) -> SlashQa | None:
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines or len(lines) > 2:
        return None
    words = lines[0].casefold().split()
    if len(words) not in (2, 3) or words[0] != "/qa":
        return None
    ruling = words[1]
    if ruling != "right" and ruling != "wrong":
        return None
    return SlashQa(
        ruling=ruling,
        runner=words[2] if len(words) == 3 else None,
        reason=lines[1] if len(lines) == 2 else None,
    )
