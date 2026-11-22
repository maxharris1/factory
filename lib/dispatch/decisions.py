from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from lib.approver_commands import parse_slash_go
from lib.dispatch.types import TrackedTicket
from lib.handoff import HandoffError, parse_comment

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_MAX_WORDS = "**Max's words**"


@dataclass(frozen=True)
class Decision:
    question: str
    options: tuple[str, ...]
    choice: str
    answerer: str
    answered_at: float
    url: str | None
    answer_id: str
    position: int


def record_decisions(
    connection: sqlite3.Connection,
    ticket: TrackedTicket,
    question_id: str | None,
    decisions: Iterable[Decision],
) -> None:
    connection.executemany(
        """
        INSERT OR IGNORE INTO decisions (
            repo, ticket_id, answer_id, position, question_id, question,
            options, choice, answerer, answered_at, url
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                ticket.repo.lower(),
                ticket.ticket_id,
                decision.answer_id,
                decision.position,
                question_id,
                decision.question,
                json.dumps(list(decision.options)),
                decision.choice,
                decision.answerer,
                decision.answered_at,
                decision.url,
            )
            for decision in decisions
        ],
    )


def answer_text(body: str) -> str:
    try:
        text = parse_comment(body).body
    except HandoffError:
        slash = parse_slash_go(body)
        text = body if slash is None else slash.instruction or "/go"
    return unquoted(text)


def answer_blocks(body: str) -> list[tuple[str, str]]:
    blocks: list[tuple[list[str], list[str] | None]] = []
    fence: str | None = None
    for line in body.splitlines():
        marker = _FENCE.match(line)
        if fence is None and line == "**Question**":
            blocks.append(([], None))
            continue
        if fence is None and line == "**Answer**" and blocks and blocks[-1][1] is None:
            blocks[-1] = (blocks[-1][0], [])
            continue
        if marker is not None:
            if fence is None:
                fence = marker.group(1)[0] * len(marker.group(1))
            elif marker.group(1).startswith(fence) and line.strip() == marker.group(1):
                fence = None
        if blocks:
            question, choice = blocks[-1]
            (question if choice is None else choice).append(line)
    return [
        ("\n".join(question).strip(), "\n".join(choice).strip())
        for question, choice in blocks
        if choice is not None
    ]


def unquoted(body: str) -> str:
    first, _, rest = body.strip().partition("\n")
    if not first.startswith(_MAX_WORDS):
        return body.strip()
    return "\n".join(line.removeprefix(">").removeprefix(" ") for line in rest.splitlines()).strip()


def chosen_words(body: str) -> str:
    text = answer_text(body)
    blocks = answer_blocks(text)
    return "\n\n".join(choice for _, choice in blocks) if blocks else text
