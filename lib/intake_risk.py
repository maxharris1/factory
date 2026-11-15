from __future__ import annotations

import sqlite3

INTAKE_RISK_TABLE = "intake_risk"
INTAKE_RISK_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {INTAKE_RISK_TABLE} (
    repo TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    risk TEXT NOT NULL CHECK (risk IN ('low', 'high')),
    PRIMARY KEY (repo, ticket_id)
)
"""


def stored_risk(connection: sqlite3.Connection, repo: str, ticket_id: str) -> str | None:
    row = connection.execute(
        f"SELECT risk FROM {INTAKE_RISK_TABLE} WHERE repo = ? AND ticket_id = ?",
        (repo.lower(), ticket_id),
    ).fetchone()
    return None if row is None else str(row[0])
