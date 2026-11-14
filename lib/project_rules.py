"""Each project's factory.md rules as the poller last read them (issue #152).

Every poll cycle the dispatcher stores one row per orch.yml project, taken
from the same parsed FactoryConfig the poll acts on
(LivePollSource.factory_reads, through LivePollSource._factory). The
read-only dashboard reads the rows back for status.json's `projects`; it
never fetches or parses a factory.md itself. A factory.md the poller could
not read is a row with the reason and no values, never a default. Each
cycle replaces every row, so a project dropped from orch.yml loses its row
and a failed read replaces an older good one.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from lib.factory_md import FactoryConfig

TABLE = "project_rules"


@dataclass(frozen=True)
class FactoryRead:
    """One project's factory.md in one poll cycle: its config or the reason
    it could not be read. Exactly one of the two is set."""

    repo: str
    config: FactoryConfig | None
    reason: str | None

    def __post_init__(self) -> None:
        if (self.config is None) == (self.reason is None):
            raise ValueError("a FactoryRead has exactly one of config and reason")


def ensure_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            repo TEXT PRIMARY KEY,
            read_at REAL NOT NULL,
            tickets TEXT,
            ship TEXT,
            rounds_low INTEGER,
            rounds_high INTEGER,
            reason TEXT,
            CHECK (
                (reason IS NULL AND tickets IS NOT NULL AND ship IS NOT NULL
                 AND rounds_low IS NOT NULL AND rounds_high IS NOT NULL)
                OR
                (reason IS NOT NULL AND tickets IS NULL AND ship IS NULL
                 AND rounds_low IS NULL AND rounds_high IS NULL)
            )
        )
        """
    )


def record_reads(
    connection: sqlite3.Connection, reads: Sequence[FactoryRead], now: float
) -> None:
    """Replace every row with this cycle's reads. The caller commits."""
    connection.execute(f"DELETE FROM {TABLE}")
    connection.executemany(
        f"""
        INSERT INTO {TABLE} (
            repo, read_at, tickets, ship, rounds_low, rounds_high, reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                read.repo.lower(),
                now,
                read.config.tickets if read.config else None,
                read.config.ship if read.config else None,
                read.config.rounds_low if read.config else None,
                read.config.rounds_high if read.config else None,
                read.reason,
            )
            for read in reads
        ],
    )


def read_rows(connection: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """Lowercased repo -> row. Empty when the table does not exist yet (a
    database no poller of this version has opened)."""
    present = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)
    ).fetchone()
    if not present:
        return {}
    cursor = connection.execute(
        f"SELECT repo, read_at, tickets, ship, rounds_low, rounds_high, reason FROM {TABLE}"
    )
    cursor.row_factory = sqlite3.Row  # type: ignore[assignment]
    return {str(row["repo"]): row for row in cursor.fetchall()}
