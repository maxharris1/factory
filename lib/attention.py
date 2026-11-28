"""The needs_attention table: tickets that need a person (#83, #84, #156).

The dispatcher flags a ship that failed and will not be retried and a job
dedupe dropped; the feeder flags blocker cycles and blockers that were closed
without finishing or do not exist. Each bot's waiter flags its own cleanup
passes failing (#1764). orch status and the dashboard list them.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Mapping

DETAIL_LIMIT = 500
DEDUPE_DROPPED = "dedupe-dropped"
DRIVER_WAKE_UNANSWERED = "driver-wake-unanswered"
CLEANUP_FAILING = "cleanup-failing"

Key = tuple[str, str, str]


def ensure_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS needs_attention (
            repo TEXT NOT NULL,
            ticket_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            detail TEXT,
            raised_at REAL NOT NULL,
            PRIMARY KEY (repo, ticket_id, reason)
        )
        """
    )


def sync(
    connection: sqlite3.Connection,
    reasons: Collection[str],
    repos: Collection[str],
    flags: Mapping[Key, str],
    now: float,
) -> None:
    """Make one owner's flags in repos exactly `flags`.

    reasons are the reasons this owner raises; flags maps (repo, ticket_id,
    reason) to its detail. A flag already raised keeps its raised_at, and a
    flag in a repo outside `repos` (its read failed this poll) is kept.
    """
    reasons = tuple(reasons)
    scope = sorted({repo.lower() for repo in repos})
    if reasons and scope:
        current = connection.execute(
            f"""
            SELECT repo, ticket_id, reason FROM needs_attention
            WHERE reason IN ({",".join("?" * len(reasons))})
              AND repo IN ({",".join("?" * len(scope))})
            """,
            (*reasons, *scope),
        ).fetchall()
        for repo, ticket_id, reason in current:
            if (repo, ticket_id, reason) not in flags:
                connection.execute(
                    "DELETE FROM needs_attention WHERE repo = ? AND ticket_id = ? AND reason = ?",
                    (repo, ticket_id, reason),
                )
    for (repo, ticket_id, reason), detail in sorted(flags.items()):
        connection.execute(
            """
            INSERT INTO needs_attention (repo, ticket_id, reason, detail, raised_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(repo, ticket_id, reason) DO UPDATE SET detail = excluded.detail
            """,
            (repo.lower(), ticket_id, reason, detail[:DETAIL_LIMIT], now),
        )


def raise_flag(connection: sqlite3.Connection, key: Key, detail: str, now: float) -> bool:
    """Raise one flag or update its detail, keeping its raised_at. True when
    this call created the row."""
    ensure_table(connection)
    repo, ticket_id, reason = key
    detail = detail[:DETAIL_LIMIT]
    created = connection.execute(
        """
        INSERT INTO needs_attention (repo, ticket_id, reason, detail, raised_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(repo, ticket_id, reason) DO NOTHING
        """,
        (repo.lower(), ticket_id, reason, detail, now),
    ).rowcount == 1
    if not created:
        connection.execute(
            "UPDATE needs_attention SET detail = ? WHERE repo = ? AND ticket_id = ? AND reason = ?",
            (detail, repo.lower(), ticket_id, reason),
        )
    return created


def clear_flag(connection: sqlite3.Connection, key: Key) -> bool:
    """Delete one flag. True when a row was deleted."""
    ensure_table(connection)
    repo, ticket_id, reason = key
    return connection.execute(
        "DELETE FROM needs_attention WHERE repo = ? AND ticket_id = ? AND reason = ?",
        (repo.lower(), ticket_id, reason),
    ).rowcount == 1
