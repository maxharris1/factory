"""Ticket discovery and tracking: feeder intake, tracked refs and terminal states."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping

from lib.dispatch.constants import STOPPED_POLL_SECONDS, STOPPED_STATES, WATCHED_STATES
from lib.dispatch.helpers import _matching_head, _spread
from lib.dispatch.types import Comment, PollSource, TicketRef, TrackedTicket
from lib.handoff import HandoffError, parse_comment
from lib.pr_title import SOURCES_TABLE, TITLE_LIMIT, prefixed


class TrackingMixin:
    def _feed(self, source: PollSource) -> None:
        """Run the backlog feeder, then start tracking what it fed.

        A fed ticket is read fresh and discovered here, so its go queues
        the job in this same poll (issue #85: N eligible tickets are N
        queued jobs within one poll). A feeder error is recorded and the
        poll goes on.
        """
        if self.feeder is None:
            return
        try:
            report = self.feeder.feed_once()
        except Exception as exc:
            self.record_poll_exception("feeder", "-", exc)
            return
        fresh_reader = getattr(source, "fresh_ticket", None)
        if fresh_reader is None:
            return
        for ref in getattr(report, "fed", ()):
            try:
                ticket = fresh_reader(ref)
            except Exception as exc:
                self.record_poll_exception(ref.repo, ref.ticket_id, exc)
                continue
            if ticket is not None and not ticket.merged:
                self._discover((ticket,), tracker=ref.tracker)

    def record_terminal(
        self,
        repo: str,
        ticket_id: str,
        state: str,
        ticket: TrackedTicket | None = None,
    ) -> None:
        """The poll saw a terminal ticket: for a merged PR, _enter_merged.

        ticket, when the source could build it, gets the orch:state/merged
        label, best effort: a failed write is a poll_errors row and never
        undoes the state.
        """
        if state not in ("merged", "handed-off", "done"):
            raise ValueError(f"{state!r} is not a completed terminal state")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if state == "merged":
                entered = self._enter_merged(connection, repo, ticket_id, "observed")
            else:
                entered = False
                connection.execute(
                    """
                    INSERT INTO ticket_state (
                        repo, ticket_id, gate_count, stuck, workflow_state
                    ) VALUES (?, ?, 0, 0, ?)
                    ON CONFLICT(repo, ticket_id)
                    DO UPDATE SET workflow_state = excluded.workflow_state
                    WHERE COALESCE(ticket_state.workflow_state, '')
                          NOT IN ('merged', 'done')
                    """,
                    (repo.lower(), ticket_id, state),
                )
            connection.commit()
        if not entered:
            return
        if self.log is not None:
            self.log(f"merged: {repo}#{ticket_id} (merge observed by the poll)")
        if ticket is None:
            return
        try:
            self._review_actions(ticket).set_state(ticket, "merged")
        except Exception as exc:
            self.record_poll_error(repo, ticket_id, f"merged-label:{type(exc).__name__}")

    def _enter_merged(
        self,
        connection: sqlite3.Connection,
        repo: str,
        ticket_id: str,
        reason: str,
    ) -> bool:
        """The one merged transition, inside the caller's transaction.

        merged is absorbing, and nothing that keeps a ticket on a person's
        screen survives it: stuck, the paused step and every needs-attention
        flag are cleared, and the ticket's queued harness jobs are
        cancelled. Grok wakes (a ship's done wake) and running jobs are
        left alone. True when the ticket was not already merged.
        """
        key = (repo.lower(), ticket_id)
        prior = connection.execute(
            "SELECT workflow_state FROM ticket_state WHERE repo = ? AND ticket_id = ?",
            key,
        ).fetchone()
        connection.execute(
            """
            INSERT INTO ticket_state (
                repo, ticket_id, gate_count, stuck, workflow_state
            ) VALUES (?, ?, 0, 0, 'merged')
            ON CONFLICT(repo, ticket_id) DO UPDATE SET
                workflow_state = 'merged',
                stuck = 0,
                stuck_reason = NULL,
                stuck_source_comment_id = NULL,
                stuck_wake_job_id = NULL,
                paused_action = NULL,
                paused_recipient = NULL,
                paused_head = NULL,
                paused_generation = NULL,
                paused_question_id = NULL
            """,
            key,
        )
        connection.execute(
            "DELETE FROM needs_attention WHERE repo = ? AND ticket_id = ?", key
        )
        queued = connection.execute(
            """
            SELECT id FROM jobs
            WHERE lower(repo) = ? AND ticket_id = ? AND state = 'queued'
              AND recipient != 'grok'
            ORDER BY created_at, id
            """,
            key,
        ).fetchall()
        for row in queued:
            self.queue.cancel(str(row["id"]), f"merged:{reason}", connection=connection)
        entered = prior is None or prior["workflow_state"] != "merged"
        if entered:
            self.merged_this_poll.add(repo.lower())
        return entered

    def _title_pull(self, ticket: TrackedTicket) -> None:
        """Record the ticket's source issue and, once per PR, prefix its title.

        Issue #115: the first time the poller sees an open PR, a title that
        does not start with the source (`#115: ` or `AUT-2284: `) is renamed.
        The rename is recorded, so a title a person edits later is left
        alone; a failed rename is retried on the next poll.
        """
        if ticket.source_issue is None or ticket.pr_number is None:
            return
        key = (ticket.repo.lower(), ticket.ticket_id)
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT pr_number, titled_at FROM {SOURCES_TABLE} "
                "WHERE repo = ? AND ticket_id = ?",
                key,
            ).fetchone()
            titled = (
                row is not None
                and row["pr_number"] == ticket.pr_number
                and row["titled_at"] is not None
            )
            connection.execute(
                f"""
                INSERT INTO {SOURCES_TABLE} (repo, ticket_id, source, pr_number, titled_at)
                VALUES (?, ?, ?, ?, NULL)
                ON CONFLICT(repo, ticket_id) DO UPDATE SET
                    source = excluded.source,
                    pr_number = excluded.pr_number,
                    titled_at = CASE
                        WHEN {SOURCES_TABLE}.pr_number = excluded.pr_number
                        THEN {SOURCES_TABLE}.titled_at
                    END
                """,
                (*key, ticket.source_issue, ticket.pr_number),
            )
        if titled or ticket.pr_title is None:
            return
        wanted = prefixed(ticket.source_issue, ticket.pr_title)[:TITLE_LIMIT]
        if wanted != ticket.pr_title:
            self._review_actions(ticket).set_title(ticket, wanted)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE {SOURCES_TABLE} SET titled_at = ? WHERE repo = ? AND ticket_id = ?",
                (self.queue.now(), *key),
            )

    def _discover(
        self,
        candidates: tuple[TrackedTicket, ...],
        *,
        tracker: str,
    ) -> None:
        for ticket in candidates:
            if ticket.factory.tickets != tracker:
                continue
            for item in sorted(
                ticket.comments,
                key=lambda value: (value.created_at, value.id),
            ):
                try:
                    fields = parse_comment(item.body).as_dict()
                except HandoffError:
                    continue
                if not self._starts_tracking(ticket, item, fields):
                    continue
                with self._connect() as connection:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO tracked_tickets (
                            repo, ticket_id, tracker, tracked_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            ticket.repo.lower(),
                            ticket.ticket_id,
                            tracker,
                            self.queue.now(),
                        ),
                    )
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO ticket_state (
                            repo, ticket_id, gate_count, stuck, generation
                        ) VALUES (?, ?, 0, 0, 1)
                        """,
                        (ticket.repo.lower(), ticket.ticket_id),
                    )
                break

    def _starts_tracking(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> bool:
        """A trusted Grok brief/go, or (ready_starts_review) the builder's ready.

        The ready path applies T2's own trust rule (factory.md's builder,
        role=builder, a trusted_logins author) and needs the ready head to
        name the PR head, so T2 can then queue the review in the same cycle.
        An unlabelled PR admitted through the issue #59 ready exception
        starts tracking only that way: its Grok brief or go still needs the
        orch:state/ label.
        """
        if fields["kind"] in ("brief", "go"):
            if ticket.admitted_by_ready:
                return False
            return self._trusted(
                ticket,
                comment,
                fields,
                "grok-bot",
                role="orchestrator",
            )
        if fields["kind"] != "ready" or not ticket.ready_starts_review:
            return False
        return (
            self._trusted(
                ticket,
                comment,
                fields,
                f"{ticket.factory.builder_harness}-bot",
                role="builder",
            )
            and _matching_head(fields["head"], ticket.current_head) is not None
        )

    def _tracked_refs(self, cycle: int | None = None) -> tuple[TicketRef, ...]:
        """The tracked tickets poll_once reads (#108).

        Every ticket that is not merged, done or abandoned, plus a merged or
        handed-off one with a completed ship intent left to replay or an
        external ship left to settle, and any ticket with a follow-up filing
        not yet linked. A handed-off ticket stays polled, so
        its merge is seen and a trusted ready may re-enter review.

        With cycle, a ticket in a stopped state (handed-off, capped, blocked,
        refused, or stuck) with nothing to replay is offered only once its
        turn came: once every STOPPED_POLL_SECONDS, spread across cycles by a
        hash of its identity. It stays offered until a read reaches it (a
        ticket or a poll error), so a cycle that reads no Linear, or defers
        the read, only delays it. Every other ticket is offered every cycle.
        """
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT repo, ticket_id, tracker, workflow_state, stuck, replay,
                       unsettled, unlinked
                FROM (
                    SELECT tracked.repo, tracked.ticket_id, tracked.tracker,
                           state.workflow_state,
                           COALESCE(state.stuck, 0) AS stuck,
                           EXISTS (
                               SELECT 1
                               FROM ship_intents AS ship
                               LEFT JOIN handled_comments AS handled
                                 ON handled.repo = ship.repo
                                AND handled.ticket_id = ship.ticket_id
                                AND handled.comment_id = ship.comment_id
                               WHERE ship.repo = tracked.repo
                                 AND ship.ticket_id = tracked.ticket_id
                                 AND ship.status = 'completed'
                                 AND handled.comment_id IS NULL
                           ) AS replay,
                           EXISTS (
                               SELECT 1
                               FROM external_ship_intents AS external
                               WHERE external.repo = tracked.repo
                                 AND external.ticket_id = tracked.ticket_id
                                 AND external.settled = 0
                           ) AS unsettled,
                           EXISTS (
                               SELECT 1
                               FROM followup_filings AS filing
                               WHERE filing.repo = tracked.repo
                                 AND filing.ticket_id = tracked.ticket_id
                                 AND filing.status IN ('pending', 'filed')
                           ) AS unlinked
                    FROM tracked_tickets AS tracked
                    LEFT JOIN ticket_state AS state
                      ON state.repo = tracked.repo
                     AND state.ticket_id = tracked.ticket_id
                )
                WHERE workflow_state IS NULL
                   OR workflow_state NOT IN ('merged', 'done', 'abandoned')
                   OR (workflow_state = 'merged' AND (replay OR unsettled))
                   OR unlinked
                ORDER BY repo, ticket_id
                """
            ).fetchall()
        every = max(1, STOPPED_POLL_SECONDS // max(1, self._poll_seconds))
        refs: list[TicketRef] = []
        stopped_keys: set[tuple[str, str]] = set()
        for row in rows:
            state = row["workflow_state"]
            catching_up = (
                bool(row["replay"]) or bool(row["unsettled"]) or bool(row["unlinked"])
            )
            stopped = bool(row["stuck"]) or state in STOPPED_STATES
            if cycle is not None and stopped and not catching_up:
                key = (str(row["repo"]).lower(), str(row["ticket_id"]))
                stopped_keys.add(key)
                if (cycle + _spread(row["repo"], row["ticket_id"])) % every == 0:
                    self._stopped_due.add(key)
                if key not in self._stopped_due:
                    continue
            refs.append(
                TicketRef(
                    repo=str(row["repo"]),
                    ticket_id=str(row["ticket_id"]),
                    tracker=str(row["tracker"]),
                    allow_merged=state == "merged"
                    or bool(row["unlinked"])
                    or (
                        state in ("handed-off", *WATCHED_STATES)
                        and bool(row["replay"])
                    ),
                )
            )
        if cycle is not None:
            self._stopped_due &= stopped_keys
        return tuple(refs)

    def active_job_tickets(self) -> frozenset[tuple[str, str]]:
        """(repo, ticket) pairs with a queued or running job, repo lowercased.

        Only the Linear webhook poller reads this: those tickets are reread
        in full every cycle (docs/SPEC.md section 7).
        """
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT repo, ticket_id
                FROM jobs
                WHERE state IN ('queued', 'running')
                """
            ).fetchall()
        return frozenset(
            (str(row["repo"]).lower(), str(row["ticket_id"])) for row in rows
        )

    def unpolled_linear_tickets(self) -> frozenset[str]:
        """Tracked Linear tickets that poll_once no longer reads.

        Merged, done or abandoned with nothing left to replay or settle
        (_tracked_refs); a handed-off ticket is still polled (#108). Only the Linear webhook poller reads this, to ack
        their dirty rows unread (docs/SPEC.md section 7).
        """
        polled = {
            ref.ticket_id for ref in self._tracked_refs() if ref.tracker == "linear"
        }
        return self._tracked_linear_ids() - polled

    def _tracked_github_ids(self) -> frozenset[tuple[str, str]]:
        """(repo, PR number) of every tracked GitHub ticket, repo lowercased."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT repo, ticket_id FROM tracked_tickets WHERE tracker = 'github'"
            ).fetchall()
        return frozenset(
            (str(row["repo"]).lower(), str(row["ticket_id"])) for row in rows
        )

    def _tracked_linear_ids(self) -> frozenset[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT ticket_id
                FROM tracked_tickets
                WHERE tracker = 'linear'
                """
            ).fetchall()
        return frozenset(str(row["ticket_id"]) for row in rows)
