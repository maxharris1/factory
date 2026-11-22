from __future__ import annotations

import json
import sqlite3

from lib.dispatch.types import TrackedTicket
from lib.rule_engine import Escalate, FindingId, Grant, Land, Ruling, Site, Wait

OUTCOME_COLUMN = {Grant: "granted", Land: "landed", Wait: "waiting", Escalate: "escalated"}


class RuleEngineMixin:
    def _log_ruling(
        self,
        connection: sqlite3.Connection,
        ticket: TrackedTicket,
        site: Site,
        subject: str,
        ruling: Ruling,
        finding_ids: tuple[FindingId, ...],
    ) -> bool:
        outcome = OUTCOME_COLUMN[type(ruling.outcome)]
        reason = ruling.outcome.reason if isinstance(ruling.outcome, Escalate) else None
        header = ruling.outcome.header if isinstance(ruling.outcome, Grant) else None
        checks = json.dumps(
            [
                {"name": check.name, "passed": check.passed, "value": check.value}
                for check in ruling.checks
            ]
        )
        before = connection.total_changes
        connection.execute(
            """
            INSERT INTO engine_decisions (
                repo, ticket_id, site, subject, rule, outcome, reason, checks,
                finding_ids, header, decided_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (repo, ticket_id, site, subject) DO UPDATE SET
                rule = excluded.rule,
                outcome = excluded.outcome,
                reason = excluded.reason,
                checks = excluded.checks,
                finding_ids = excluded.finding_ids,
                header = excluded.header,
                decided_at = excluded.decided_at
            WHERE engine_decisions.outcome IS NOT excluded.outcome
                OR engine_decisions.reason IS NOT excluded.reason
                OR engine_decisions.header IS NOT excluded.header
            """,
            (
                ticket.repo,
                ticket.ticket_id,
                site,
                subject,
                ruling.rule,
                outcome,
                reason,
                checks,
                json.dumps(list(finding_ids)),
                header,
                self.queue.now(),
            ),
        )
        written = connection.total_changes > before
        if written:
            line = (
                f"orch poll: engine {ticket.repo} {ticket.ticket_id} site={site}"
                f" subject={subject} rule={ruling.rule} outcome={outcome}"
            )
            print(line if reason is None else f"{line} reason={reason}", flush=True)
        return written
