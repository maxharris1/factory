
from __future__ import annotations

import sqlite3

from lib.dispatch.types import TrackedTicket


class EffectiveCapMixin:
    def _over_cap(self, ticket: TrackedTicket, reported_round: int | None) -> bool:
        """A verdict round above the effective cap: no extra round covers it."""
        return reported_round is not None and reported_round > self._effective_cap(ticket)

    def _config_cap(self, ticket: TrackedTicket) -> int:
        return (
            ticket.factory.rounds_low
            if ticket.risk == "low"
            else ticket.factory.rounds_high
        )

    def _effective_cap(
        self,
        ticket: TrackedTicket,
        connection: sqlite3.Connection | None = None,
    ) -> int:
        """The ticket's review round cap: factory.md's for its risk, plus
        one per extra round Max approved (ticket_state.cap_extra)."""
        query = """
            SELECT cap_extra FROM ticket_state
            WHERE repo = ? AND ticket_id = ?
        """
        key = (ticket.repo.lower(), ticket.ticket_id)
        if connection is None:
            with self._connect() as own:
                row = own.execute(query, key).fetchone()
        else:
            row = connection.execute(query, key).fetchone()
        return self._config_cap(ticket) + (int(row["cap_extra"]) if row else 0)
