from __future__ import annotations

import sqlite3
import sys
from typing import Literal

from lib.dispatch.constants import HANDED_OFF_EXTERNALLY, SETTLE_CLEARS, TERMINAL_STATES
from lib.dispatch.transitions import write_workflow_state
from lib.dispatch.types import LinearState, LinearStateRead, PollSource, TicketRef

LinearMove = Literal["hand-off", "resume", "done", "canceled"]
_LINEAR_MOVES: dict[LinearMove, str] = {
    "hand-off": "_hand_off_externally",
    "resume": "_resume_external",
    "done": "_linear_done",
    "canceled": "_linear_canceled",
}

NOT_STARTED = (None, "intake", "refused", "questions")
HANDOFF_SOURCES = frozenset(
    {
        "planning",
        "plan-check",
        "building",
        "review",
        "fixing",
        "capped",
        "blocked",
        "waiting-max",
        "awaiting-ok",
        "stuck",
    }
)


def linear_move(
    workflow_state: str | None,
    stuck: bool,
    paused_action: str | None,
    linear: LinearState,
    processed: bool,
) -> LinearMove | None:
    if workflow_state in NOT_STARTED or workflow_state in TERMINAL_STATES:
        return None
    if workflow_state == "waiting-max" and paused_action is None:
        return None
    if linear.type == "canceled":
        return "canceled"
    if linear.type == "completed":
        return "done"
    if workflow_state == HANDED_OFF_EXTERNALLY:
        return "resume" if linear.type == "started" and not linear.handoff else None
    if not linear.handoff or workflow_state not in HANDOFF_SOURCES:
        return None
    if workflow_state == "building" and not stuck and not processed:
        return None
    return "hand-off"


class LinearStatesMixin:
    def _read_linear_states(self, source: PollSource) -> None:
        self._linear_fresh: dict[tuple[str, str], LinearState] = {}
        reader = getattr(source, "linear_states", None)
        if reader is None:
            return
        refs = self._linear_state_refs()
        if not refs:
            return
        try:
            read: LinearStateRead | None = reader(refs)
        except Exception as exc:
            reason = type(exc).__name__
            read = LinearStateRead({}, {ref.ticket_id: reason for ref in refs})
        if read is None:
            return
        now = self.queue.now()
        started: list[tuple[str, str, str]] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for ref in refs:
                key = (ref.repo.lower(), ref.ticket_id)
                state = read.states.get(ref.ticket_id)
                if state is not None:
                    connection.execute(
                        """
                        INSERT INTO linear_states (
                            repo, ticket_id, state_name, state_type, read_at, read_error
                        ) VALUES (?, ?, ?, ?, ?, NULL)
                        ON CONFLICT (repo, ticket_id) DO UPDATE SET
                            state_name = excluded.state_name,
                            state_type = excluded.state_type,
                            read_at = excluded.read_at,
                            read_error = NULL
                        """,
                        (*key, state.name, state.type, now),
                    )
                    self._linear_fresh[key] = state
                    continue
                error = read.errors.get(ref.ticket_id)
                if error is None:
                    continue
                if connection.execute(
                    """
                    INSERT INTO linear_states (repo, ticket_id, read_error)
                    VALUES (?, ?, ?)
                    ON CONFLICT (repo, ticket_id) DO UPDATE SET
                        read_error = excluded.read_error
                    WHERE linear_states.read_error IS NULL
                    """,
                    (*key, error),
                ).rowcount:
                    started.append((*key, error))
                    connection.execute(
                        """
                        INSERT INTO poll_errors (repo, ticket_id, error, recorded_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (*key, f"linear-state:{error}", now),
                    )
            connection.commit()
        for repo, ticket_id, error in started:
            print(
                f"orch poll: {repo} {ticket_id} Linear state read failed: {error}",
                file=sys.stderr,
                flush=True,
            )

    def _linear_state_refs(self) -> tuple[TicketRef, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT tracked.repo, tracked.ticket_id
                FROM tracked_tickets AS tracked
                LEFT JOIN ticket_state AS state
                  ON state.repo = tracked.repo AND state.ticket_id = tracked.ticket_id
                WHERE tracked.tracker = 'linear'
                  AND COALESCE(state.workflow_state, '') NOT IN
                      ({", ".join("?" for _ in TERMINAL_STATES)})
                ORDER BY tracked.repo, tracked.ticket_id
                """,
                tuple(sorted(TERMINAL_STATES)),
            ).fetchall()
        return tuple(
            TicketRef(repo=str(row["repo"]), ticket_id=str(row["ticket_id"]), tracker="linear")
            for row in rows
        )

    def _apply_linear_moves(self) -> None:
        for (repo, ticket_id), linear in sorted(self._linear_fresh.items()):
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT workflow_state, stuck, paused_action FROM ticket_state
                    WHERE repo = ? AND ticket_id = ?
                    """,
                    (repo, ticket_id),
                ).fetchone()
            if row is None:
                continue
            state, stuck = row["workflow_state"], bool(row["stuck"])
            move = linear_move(
                state,
                stuck,
                row["paused_action"],
                linear,
                (repo, ticket_id) in self._polled,
            )
            if move is None:
                continue
            try:
                moved = getattr(self, _LINEAR_MOVES[move])(repo, ticket_id, state, stuck)
            except Exception as exc:
                self.record_poll_exception(repo, ticket_id, exc, prefix="linear-move:")
                continue
            if moved and self.log is not None:
                self.log(f"linear {move}: {repo}#{ticket_id} ({linear.name})")

    def _hand_off_externally(
        self, repo: str, ticket_id: str, state: str, stuck: bool
    ) -> bool:
        now = self.queue.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not write_workflow_state(
                connection,
                repo,
                ticket_id,
                HANDED_OFF_EXTERNALLY,
                source="linear-handoff",
                at=now,
                also={
                    "stuck": 0,
                    "external_from_state": state,
                    "external_from_stuck": int(stuck),
                    "external_at": now,
                },
                guard="workflow_state = ? AND stuck = ?",
                guard_args=(state, int(stuck)),
            ):
                connection.rollback()
                return False
            connection.execute(
                "DELETE FROM needs_attention WHERE repo = ? AND ticket_id = ?",
                (repo, ticket_id),
            )
            self._cancel_queued(connection, repo, ticket_id, HANDED_OFF_EXTERNALLY)
            connection.commit()
        return True

    def _resume_external(
        self, repo: str, ticket_id: str, state: str, stuck: bool
    ) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT external_from_state, external_from_stuck FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (repo, ticket_id),
            ).fetchone()
            if row is None or row["external_from_state"] is None:
                connection.rollback()
                return False
            resumed = write_workflow_state(
                connection,
                repo,
                ticket_id,
                str(row["external_from_state"]),
                source="linear-resume",
                at=self.queue.now(),
                also={
                    "stuck": int(row["external_from_stuck"] or 0),
                    "external_from_state": None,
                    "external_from_stuck": None,
                    "external_at": None,
                },
                guard="workflow_state = ?",
                guard_args=(HANDED_OFF_EXTERNALLY,),
            )
            connection.commit()
        return resumed

    def _linear_done(self, repo: str, ticket_id: str, state: str, stuck: bool) -> bool:
        reader = getattr(self, "_fresh_reader", None)
        merged = False
        if reader is not None:
            ticket = reader(
                TicketRef(repo=repo, ticket_id=ticket_id, tracker="linear", allow_merged=True)
            )
            merged = ticket is not None and bool(ticket.merged)
        if merged:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                entered = self._enter_merged(connection, repo, ticket_id, "linear-done")
                connection.commit()
            return entered
        return self._settle(repo, ticket_id, state, "done", "linear-done")

    def _linear_canceled(
        self, repo: str, ticket_id: str, state: str, stuck: bool
    ) -> bool:
        return self._settle(repo, ticket_id, state, "abandoned", "linear-canceled")

    def _settle(
        self, repo: str, ticket_id: str, state: str, to: str, source: str
    ) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not write_workflow_state(
                connection,
                repo,
                ticket_id,
                to,
                source=source,
                at=self.queue.now(),
                also=SETTLE_CLEARS,
                guard="workflow_state = ?",
                guard_args=(state,),
            ):
                connection.rollback()
                return False
            connection.execute(
                "DELETE FROM needs_attention WHERE repo = ? AND ticket_id = ?",
                (repo, ticket_id),
            )
            self._cancel_queued(connection, repo, ticket_id, source)
            connection.commit()
        return True

    def _cancel_queued(
        self, connection: sqlite3.Connection, repo: str, ticket_id: str, reason: str
    ) -> None:
        rows = connection.execute(
            """
            SELECT id FROM jobs
            WHERE lower(repo) = ? AND ticket_id = ? AND state = 'queued'
            ORDER BY created_at, id
            """,
            (repo, ticket_id),
        ).fetchall()
        for row in rows:
            self.queue.cancel(str(row["id"]), reason, connection=connection)
