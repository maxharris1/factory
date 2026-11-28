from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable
from typing import Literal, Protocol, runtime_checkable

from lib.dispatch.rebase import answers_rebase_review
from lib.dispatch.types import Comment, ReviewActions, RoundGrant, TrackedTicket
from lib.dispatch.verdict.convergence import policy_name, stop_policy
from lib.dispatch.verdict.repeat import StopRecord
from lib.handoff import HandoffError, parse_comment
from lib.queue import SQLiteJobQueue
from lib.rule_engine import (
    Escalate,
    FindingId,
    Grant,
    Land,
    Ruling,
    Site,
    StopFacts,
    Wait,
    comment_id,
    decide,
    finding_id,
)

OUTCOME_COLUMN = {Grant: "granted", Land: "landed", Wait: "waiting", Escalate: "escalated"}


def engine_marker(site: Site, subject: str) -> str:
    return f"<!-- orch-engine site={site} subject={subject} -->"


def _valid_ids(texts: Iterable[str]) -> tuple[FindingId, ...]:
    valid = []
    for text in texts:
        try:
            valid.append(finding_id(text))
        except ValueError:
            continue
    return tuple(sorted(valid))


_NOTICE_OPENINGS: dict[Site, str] = {
    "cap": "Factory granted one more review round at the cap in code, because blocking findings {ids}",
    "repeat": "Factory resumed the paused fix in code at a first repeat stop, because finding {ids}",
}


def _notice(site: Site, ids: list[str], header: str, subject: str) -> str:
    return (
        f"{_NOTICE_OPENINGS[site].format(ids=', '.join(ids))} had no earlier extra round. It needs no action.\n\n"
        f"Validated header: `{header}`\n\n"
        f"{engine_marker(site, subject)}\n"
    )


def prior_grants(history: Iterable[StopRecord], verdict_id: str) -> frozenset[FindingId] | None:
    released = [record for record in history if record.released_by is not None and record.verdict_id != verdict_id]
    if not all(record.complete for record in released):
        return None
    return frozenset(_valid_ids(found for record in released for found in record.finding_ids))


@runtime_checkable
class ReviewPort(Protocol):
    def _stop_history(self, ticket: TrackedTicket) -> tuple[StopRecord, ...]: ...

    def _effective_cap(
        self,
        ticket: TrackedTicket,
        connection: sqlite3.Connection | None = None,
    ) -> int: ...

    def _apply_round_grant(
        self,
        ticket: TrackedTicket,
        grant: RoundGrant,
        *,
        trusted_review: bool = False,
    ) -> tuple[str, bool, bool] | None: ...

    def _after_lift(self, ticket: TrackedTicket, queued: bool, reopened: bool) -> None: ...

    def _review_actions(self, ticket: TrackedTicket) -> ReviewActions: ...


class ReviewCoordinator:
    def __init__(self, *, queue: SQLiteJobQueue, connect: Callable[[], sqlite3.Connection], port: ReviewPort) -> None:
        self.queue = queue
        self.connect = connect
        self.port = port

    def on_cap(self, ticket: TrackedTicket, verdict: Comment, stop: Literal["capped", "stalled"]) -> bool:
        try:
            subject = comment_id(verdict.id)
        except ValueError:
            return False
        history = self.port._stop_history(ticket)
        named = next((record.finding_ids for record in history if record.verdict_id == verdict.id), frozenset())
        ids = _valid_ids(named)
        if len(ids) != len(named):
            ids = ()
        prior = prior_grants(history, verdict.id)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            logged = connection.execute(
                """
                SELECT outcome, notice_status FROM engine_decisions
                WHERE lower(repo) = ? AND ticket_id = ? AND site = 'cap' AND subject = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, subject),
            ).fetchone()
            if logged is None:
                state = connection.execute(
                    "SELECT review_round, stuck FROM ticket_state WHERE repo = ? AND ticket_id = ?",
                    (ticket.repo.lower(), ticket.ticket_id),
                ).fetchone()
                round_number = int(state["review_round"])
                if answers_rebase_review(connection, ticket, round_number):
                    ruling = Ruling("first-cap-grant", Escalate("rebase-review"), ())
                    self.log_ruling(connection, ticket, "cap", subject, ruling, ids)
                    granted = False
                else:
                    auto_prior = self._auto_extra_finding_ids(connection, ticket, verdict.id)
                    combined_prior = None if (prior is None or auto_prior is None) else prior | auto_prior
                    ruling = decide(
                        StopFacts(
                            site="cap",
                            stop=stop,
                            verdict_id=subject,
                            finding_ids=ids,
                            repeat_stops=0,
                            prior_grants=combined_prior,
                            round=round_number,
                            cap=self.port._effective_cap(ticket, connection),
                            stuck=bool(state["stuck"]),
                            policy=policy_name(stop_policy(ticket.factory)),
                        )
                    )
                    self.log_ruling(connection, ticket, "cap", subject, ruling, ids)
                    granted = isinstance(ruling.outcome, Grant)
            else:
                granted = logged["outcome"] == "granted" and logged["notice_status"] != "void"
            connection.commit()
        if not granted:
            return False
        self.apply_engine_grants(ticket)
        return True

    def _auto_extra_finding_ids(
        self, connection: sqlite3.Connection, ticket: TrackedTicket, verdict_id: str
    ) -> frozenset[FindingId] | None:
        rows = connection.execute(
            """
            SELECT comment_id FROM auto_extra_comments
            WHERE repo = ? AND ticket_id = ? AND comment_id != ?
            """,
            (ticket.repo.lower(), ticket.ticket_id, verdict_id),
        ).fetchall()
        if not rows:
            return frozenset()
        from lib.evidence.findings import blocking_ids

        bodies = {item.id: item.body for item in ticket.comments}
        found: list[str] = []
        for row in rows:
            body = bodies.get(str(row["comment_id"]))
            if body is None:
                return None
            try:
                parsed = parse_comment(body)
            except HandoffError:
                return None
            found.extend(blocking_ids(parsed.body))
        return frozenset(_valid_ids(found))

    def on_repeat(
        self,
        connection: sqlite3.Connection,
        ticket: TrackedTicket,
        verdict: Comment,
        named: frozenset[str],
        stops: int,
        prior: frozenset[FindingId] | None,
        round_number: int,
    ) -> bool:
        try:
            subject = comment_id(verdict.id)
        except ValueError:
            return False
        ids = _valid_ids(named)
        if len(ids) != len(named):
            ids = ()
        state = connection.execute(
            "SELECT stuck FROM ticket_state WHERE repo = ? AND ticket_id = ?",
            (ticket.repo.lower(), ticket.ticket_id),
        ).fetchone()
        auto_prior = self._auto_extra_finding_ids(connection, ticket, verdict.id)
        combined_prior = None if (prior is None or auto_prior is None) else prior | auto_prior
        ruling = decide(
            StopFacts(
                site="repeat",
                stop="repeat",
                verdict_id=subject,
                finding_ids=ids,
                repeat_stops=stops,
                prior_grants=combined_prior,
                round=round_number,
                cap=self.port._effective_cap(ticket, connection),
                stuck=bool(state["stuck"]),
                policy=policy_name(stop_policy(ticket.factory)),
            )
        )
        self.log_ruling(connection, ticket, "repeat", subject, ruling, ids)
        return isinstance(ruling.outcome, Grant)

    def apply_engine_grants(self, ticket: TrackedTicket) -> None:
        with self.connect() as connection:
            pending = connection.execute(
                """
                SELECT site, subject, finding_ids, header FROM engine_decisions
                WHERE lower(repo) = ? AND ticket_id = ? AND site IN ('cap', 'repeat')
                  AND outcome = 'granted' AND notice_status = 'pending'
                ORDER BY decided_at, subject
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchall()
        for row in pending:
            site: Literal["cap", "repeat"] = row["site"]
            subject = str(row["subject"])
            if site == "cap":
                stop = next((item for item in ticket.comments if item.id == subject), None)
                if stop is None:
                    continue
                result = self.port._apply_round_grant(ticket, RoundGrant.engine(stop))
                if result is None or result[0] != "queued-fix":
                    self._mark_notice(ticket, site, subject, "void")
                    continue
            self.port._after_lift(ticket, queued=True, reopened=False)
            self.port._review_actions(ticket).comment_once(
                ticket,
                _notice(site, json.loads(row["finding_ids"]), str(row["header"]), subject),
                engine_marker(site, subject),
            )
            self._mark_notice(ticket, site, subject, "posted")

    def _mark_notice(
        self, ticket: TrackedTicket, site: Literal["cap", "repeat"], subject: str, status: Literal["void", "posted"]
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE engine_decisions SET notice_status = ?
                WHERE lower(repo) = ? AND ticket_id = ? AND site = ? AND subject = ?
                """,
                (status, ticket.repo.lower(), ticket.ticket_id, site, subject),
            )
            connection.commit()

    def log_ruling(
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
                finding_ids, header, decided_at, notice_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (repo, ticket_id, site, subject) DO UPDATE SET
                rule = excluded.rule,
                outcome = excluded.outcome,
                reason = excluded.reason,
                checks = excluded.checks,
                finding_ids = excluded.finding_ids,
                header = excluded.header,
                decided_at = excluded.decided_at,
                notice_status = excluded.notice_status
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
                "pending" if isinstance(ruling.outcome, Grant) else None,
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
