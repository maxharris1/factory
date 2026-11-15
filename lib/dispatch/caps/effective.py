from __future__ import annotations

import sqlite3

from lib.dispatch.types import TrackedTicket
from lib.intake_risk import stored_risk


class EffectiveCapMixin:
    def _over_cap(self, ticket: TrackedTicket, reported_round: int | None) -> bool:
        """A verdict round above the effective cap: no extra round covers it."""
        return reported_round is not None and reported_round > self._effective_cap(ticket)

    def _config_cap(self, ticket: TrackedTicket, connection: sqlite3.Connection) -> int:
        stored = stored_risk(connection, ticket.repo, ticket.ticket_id)
        raised_this_poll = ticket.risk != "low"
        if stored == "low" and not raised_this_poll:
            return ticket.factory.rounds_low
        return ticket.factory.rounds_high

    def _effective_cap(
        self,
        ticket: TrackedTicket,
        connection: sqlite3.Connection | None = None,
    ) -> int:
        """The ticket's review round cap: factory.md's for its risk, plus
        one per extra round Max approved (ticket_state.cap_extra)."""
        if connection is None:
            with self._connect() as own:
                return self._effective_cap(ticket, own)
        row = connection.execute(
            "SELECT cap_extra FROM ticket_state WHERE repo = ? AND ticket_id = ?",
            (ticket.repo.lower(), ticket.ticket_id),
        ).fetchone()
        return self._config_cap(ticket, connection) + (int(row["cap_extra"]) if row else 0)
