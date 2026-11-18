"""The one writer of ticket_state.workflow_state and its ticket_transitions log."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence

from lib.attention import DEDUPE_DROPPED
from lib.dispatch.constants import CI_INFRA_BLOCKED_REASON, CI_SILENT_REASON, TERMINAL_STATES


def write_workflow_state(
    connection: sqlite3.Connection,
    repo: str,
    ticket_id: str,
    state: str,
    *,
    source: str,
    at: float,
    also: Mapping[str, object] | None = None,
    guard: str | None = None,
    guard_args: Sequence[object] = (),
    insert: Mapping[str, object] | None = None,
) -> bool:
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    key = (repo.lower(), ticket_id)
    row = connection.execute(
        "SELECT workflow_state FROM ticket_state WHERE repo = ? AND ticket_id = ?",
        key,
    ).fetchone()
    if row is None:
        if insert is None:
            return False
        columns = {**insert, "workflow_state": state}
        connection.execute(
            f"""
            INSERT INTO ticket_state (repo, ticket_id, {", ".join(columns)})
            VALUES (?, ?, {", ".join("?" for _ in columns)})
            """,
            (*key, *columns.values()),
        )
        prior = None
    else:
        updates = {"workflow_state": state, **(also or {})}
        written = connection.execute(
            f"""
            UPDATE ticket_state SET {", ".join(f"{name} = ?" for name in updates)}
            WHERE repo = ? AND ticket_id = ?{f" AND ({guard})" if guard else ""}
            """,
            (*updates.values(), *key, *guard_args),
        ).rowcount
        if not written:
            return False
        prior = row[0]
    if state in TERMINAL_STATES:
        connection.execute("DELETE FROM ci_waits WHERE repo = ? AND ticket_id = ?", key)
        connection.execute("DELETE FROM ci_silence WHERE repo = ? AND ticket_id = ?", key)
        connection.execute(
            "DELETE FROM needs_attention WHERE repo = ? AND ticket_id = ? AND reason IN (?, ?)",
            (*key, CI_SILENT_REASON, CI_INFRA_BLOCKED_REASON),
        )
    if prior != state:
        if prior is not None:
            connection.execute(
                "DELETE FROM needs_attention WHERE repo = ? AND ticket_id = ? AND reason = ?",
                (*key, DEDUPE_DROPPED),
            )
        connection.execute(
            """
            INSERT INTO ticket_transitions (
                repo, ticket_id, from_state, to_state, at, source
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (*key, prior, state, at, source),
        )
    return True
