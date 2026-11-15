"""poll_once, handoff verification, poll-error streaks, attention, and PollLoop."""

from __future__ import annotations

import sqlite3
import sys
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from itertools import islice
from typing import TYPE_CHECKING

from lib import project_rules
from lib.dispatch.constants import (
    GITHUB_STATE_LABEL_PREFIX,
    LINEAR_HANDOFF_QUERY,
    OFF_LOOP_VERIFY_READS,
    POLL_ERROR_RETENTION_SECONDS,
    POLL_ERROR_STREAK,
    SHIP_ERROR_LIMIT,
    _UNVERIFIED_HANDOFF,
)
from lib.dispatch.cycle_clock import CycleClock
from lib.dispatch.errors_text import poll_error_detail, poll_error_text
from lib.dispatch.types import PollCycleFailed, PollSource, TicketRef, TrackedTicket
from lib.handoff import HandoffError, parse_comment
from lib.orch_yml import POLL_SECONDS
from lib.queue import Job, _job

if TYPE_CHECKING:
    from lib.dispatcher import Dispatcher


class PollMixin:
    cycle_clock: CycleClock | None = None

    def _phase(self, name: str) -> AbstractContextManager[None]:
        return nullcontext() if self.cycle_clock is None else self.cycle_clock.phase(name)

    def poll_once(self, source: PollSource) -> None:
        begin = getattr(source, "begin_cycle", None)
        end = getattr(source, "end_cycle", None)
        if hasattr(source, "cycle_clock"):
            source.cycle_clock = self.cycle_clock
        if begin is not None:
            begin()
        self._polled = {}
        self._read_started = None
        self._merged_repos = set()
        self.merged_this_poll = set()
        self._fresh_reader = getattr(source, "fresh_ticket", None)
        self._cycle_failures: list[str] = []
        read_from = self.queue.now()
        with self._phase("feed"):
            self._feed(source)
        with self._phase("github-candidates"):
            try:
                github = source.github_candidates(
                    GITHUB_STATE_LABEL_PREFIX, exclude=self._tracked_github_ids()
                )
            except Exception as exc:
                self.record_poll_exception("github", "-", exc)
                self._cycle_failures.append(f"github: {type(exc).__name__}")
                github = ()
            self._discover(github, tracker="github")
        with self._phase("linear-candidates"):
            active_refs = self._tracked_refs()
            try:
                linear = source.linear_candidates(
                    LINEAR_HANDOFF_QUERY,
                    exclude=self._tracked_linear_ids(),
                    reserve=sum(
                        ref.tracker == "linear"
                        for ref in active_refs
                    ),
                )
            except Exception as exc:
                self.record_poll_exception("linear", "-", exc)
                self._cycle_failures.append(f"linear: {type(exc).__name__}")
                linear = ()
            self._discover(linear, tracker="linear")
        with self._phase("tracked-read"):
            read_at = self._read_started = self.queue.now()
            refs = self._tracked_refs(read_at)
            self._source_errors = {}
            try:
                tracked = source.tracked_tickets(refs)
            except Exception as exc:
                self.record_poll_exception("tracked", "-", exc)
                self._cycle_failures.append(f"tracked: {type(exc).__name__}")
                tracked = ()
            read = {(ticket.repo.lower(), ticket.ticket_id) for ticket in tracked}
            self._record_reads(read, read_at)
            for ref in refs:
                identity = (ref.repo.lower(), ref.ticket_id)
                error = self._source_errors.get(identity)
                if error is not None and identity not in read:
                    self._advance_poll_error_streak(*identity, error)
        with self._phase("process"):
            self._ticket_pass, self._deferred_ships = True, {}
            try:
                for ticket in tracked:
                    try:
                        self.process(ticket)
                    except Exception as exc:
                        self._record_poll_error(ticket, exc)
                    else:
                        self._clear_poll_error_streak(ticket)
            finally:
                self._ticket_pass = False
            for key, ticket in self._deferred_ships.items():
                try:
                    self.process(self._polled.get(key, ticket))
                except Exception as exc:
                    self._record_poll_error(ticket, exc)
        with self._phase("merge-queue"):
            self._advance_merge_queues()
        with self._phase("verify-handoffs"):
            self._verify_handoffs(source, read_from)
        with self._phase("reconcile-jobs"):
            try:
                self.reconcile_jobs()
            finally:
                self._read_started = None
        with self._phase("factory-reads"):
            self._record_factory_reads(source)
        self._prune_poll_errors()
        failed = "poll cycle failed: " + ", ".join(self._cycle_failures)
        try:
            if end is not None:
                end()
        except Exception as exc:
            if self._cycle_failures:
                raise PollCycleFailed(f"{failed}; {exc}") from exc
            raise
        if self._cycle_failures:
            raise PollCycleFailed(failed)

    def _verify_handoffs(self, source: PollSource, read_from: float) -> None:
        """#18: check each done job's handoff comment once, on the ticket read.

        Only jobs finished before this poll read its tickets: the handoff
        must be one of the comments read, carry an orch:v1 header, and come
        from a login trusted for the job's recipient. Otherwise the ticket
        is flagged unverified-handoff. The job stays done either way. A
        ticket that left the poll loop (shipped, abandoned) is read here,
        newest job first, only while it has a job to verify.
        """
        tickets = dict(self._polled)
        unread = self._unpolled_handoff_refs(read_from)
        if unread:
            try:
                for ticket in source.tracked_tickets(unread):
                    tickets[(ticket.repo.lower(), ticket.ticket_id)] = ticket
            except Exception as exc:
                self.record_poll_error("verify", "-", type(exc).__name__)
                self._cycle_failures.append(f"verify: {type(exc).__name__}")
        for (repo, ticket_id), ticket in tickets.items():
            with self._connect() as connection:
                rows = connection.execute(
                    f"""
                    SELECT * FROM jobs
                    WHERE lower(repo) = ? AND ticket_id = ? AND {_UNVERIFIED_HANDOFF}
                    ORDER BY finished_at, id
                    """,
                    (repo, ticket_id, read_from),
                ).fetchall()
            for row in rows:
                job = _job(row)
                problem = self._handoff_problem(ticket, job)
                event = "handoff-unverified" if problem else "handoff-verified"
                detail = problem or f"comment {job.handoff_comment_id} checked"
                with self._connect() as connection:
                    self._record_job_event(
                        connection, job, event, detail, self.queue.now()
                    )
                self._log_job_event(job, event, detail)
                if problem:
                    self.flag_attention(
                        ticket.repo,
                        ticket.ticket_id,
                        "unverified-handoff",
                        f"job {job.id[:8]} {job.request.action}: {problem}",
                    )

    def _unpolled_handoff_refs(self, read_from: float) -> tuple[TicketRef, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT tracked.repo, tracked.ticket_id, tracked.tracker
                FROM jobs
                JOIN tracked_tickets AS tracked
                  ON tracked.repo = lower(jobs.repo)
                 AND tracked.ticket_id = jobs.ticket_id
                WHERE {_UNVERIFIED_HANDOFF}
                GROUP BY tracked.repo, tracked.ticket_id, tracked.tracker
                ORDER BY max(jobs.finished_at) DESC, tracked.repo, tracked.ticket_id
                """,
                (read_from,),
            ).fetchall()
        refs = (
            TicketRef(
                repo=str(row["repo"]),
                ticket_id=str(row["ticket_id"]),
                tracker=str(row["tracker"]),
                allow_merged=True,
            )
            for row in rows
            if (str(row["repo"]), str(row["ticket_id"])) not in self._polled
        )
        return tuple(islice(refs, OFF_LOOP_VERIFY_READS))

    def _handoff_problem(self, ticket: TrackedTicket, job: Job) -> str | None:
        found = next(
            (item for item in ticket.comments if item.id == job.handoff_comment_id),
            None,
        )
        if found is None:
            return f"handoff comment {job.handoff_comment_id} is not on the ticket"
        try:
            parse_comment(found.body)
        except HandoffError:
            return f"handoff comment {found.id} has no orch:v1 header"
        if f"{job.request.recipient}-bot" not in self._trusted_bots(found):
            return (
                f"handoff comment {found.id} is not from a login trusted for "
                f"{job.request.recipient}-bot"
            )
        return None

    def _record_factory_reads(self, source: PollSource) -> None:
        """Store each project's factory.md as this cycle read it (#152)."""
        reads = getattr(source, "factory_reads", None)
        if reads is None:
            return
        try:
            cycle = reads()
            with self._connect() as connection:
                project_rules.record_reads(connection, cycle, self.queue.now())
        except Exception as exc:
            self.record_poll_error("factory", "-", type(exc).__name__)

    def _prune_poll_errors(self) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    "DELETE FROM poll_errors WHERE recorded_at < ?",
                    (self.queue.now() - POLL_ERROR_RETENTION_SECONDS,),
                )
        except sqlite3.Error as exc:
            self.record_poll_error("prune", "-", type(exc).__name__)

    def _record_reads(self, keys: set[tuple[str, str]], at: float) -> None:
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO ticket_reads (repo, ticket_id, read_at) VALUES (?, ?, ?)
                ON CONFLICT (repo, ticket_id) DO UPDATE SET read_at = excluded.read_at
                """,
                [(repo, ticket_id, at) for repo, ticket_id in keys],
            )

    def record_poll_error(
        self,
        repo: str,
        ticket_id: str,
        error: str | BaseException,
        detail: str | None = None,
        *,
        prefix: str = "",
    ) -> None:
        self._record_reads({(repo.lower(), ticket_id)}, self.queue.now())
        self._source_errors[(repo.lower(), ticket_id)] = poll_error_text(error)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO poll_errors (
                    repo, ticket_id, error, detail, recorded_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    repo.lower(),
                    ticket_id,
                    prefix + poll_error_text(error),
                    detail,
                    self.queue.now(),
                ),
            )

    def record_poll_exception(
        self,
        repo: str,
        ticket_id: str,
        exc: BaseException,
        *,
        prefix: str = "",
    ) -> None:
        """Record the class in poll_errors.error and the traceback in detail.

        The same traceback goes to stderr, the poller's log.
        """
        error = f"{prefix}{type(exc).__name__}"
        detail = poll_error_detail(exc)
        print(f"orch poll: {repo} {ticket_id} {error}\n{detail}", file=sys.stderr, flush=True)
        self.record_poll_error(repo, ticket_id, error, detail)

    def record_poll_deferral(self, detail: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO poll_deferrals (detail, recorded_at) VALUES (?, ?)",
                (detail, self.queue.now()),
            )

    def flag_attention(
        self,
        repo: str,
        ticket_id: str,
        reason: str,
        detail: str,
    ) -> None:
        """Show a ticket as needs-attention in orch status and the dashboard."""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO needs_attention (repo, ticket_id, reason, detail, raised_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(repo, ticket_id, reason) DO UPDATE SET
                    detail = excluded.detail, raised_at = excluded.raised_at
                """,
                (repo.lower(), ticket_id, reason, detail[:SHIP_ERROR_LIMIT], self.queue.now()),
            )
        if self.log is not None:
            self.log(f"needs attention: {repo}#{ticket_id} {reason}")

    def _flag_attention_once(
        self,
        connection: sqlite3.Connection,
        repo: str,
        ticket_id: str,
        reason: str,
        detail: str,
    ) -> None:
        added = connection.execute(
            """
            INSERT INTO needs_attention (repo, ticket_id, reason, detail, raised_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(repo, ticket_id, reason) DO NOTHING
            """,
            (repo.lower(), ticket_id, reason, detail[:SHIP_ERROR_LIMIT], self.queue.now()),
        ).rowcount
        if added and self.log is not None:
            self.log(f"needs attention: {repo}#{ticket_id} {reason}")

    def clear_attention(self, repo: str, ticket_id: str, reason: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM needs_attention
                WHERE repo = ? AND ticket_id = ? AND reason = ?
                """,
                (repo.lower(), ticket_id, reason),
            )

    def _record_poll_error(self, ticket: TrackedTicket, exc: BaseException) -> None:
        """Record a ticket's failed poll; the POLL_ERROR_STREAK-th failure
        in a row flags poll-errors attention."""
        self.record_poll_exception(ticket.repo, ticket.ticket_id, exc)
        self._advance_poll_error_streak(
            ticket.repo.lower(), ticket.ticket_id, type(exc).__name__
        )

    def _advance_poll_error_streak(self, repo: str, ticket_id: str, error: str) -> None:
        identity = (repo, ticket_id)
        with self._connect() as connection:
            count = connection.execute(
                """
                INSERT INTO poll_error_streaks (repo, ticket_id, count)
                VALUES (?, ?, 1)
                ON CONFLICT(repo, ticket_id) DO UPDATE SET count = count + 1
                RETURNING count
                """,
                identity,
            ).fetchone()[0]
        if count == POLL_ERROR_STREAK:
            self.flag_attention(
                *identity, "poll-errors", f"{count} polls in a row failed: {error}"
            )

    def _clear_poll_error_streak(self, ticket: TrackedTicket) -> None:
        identity = (ticket.repo.lower(), ticket.ticket_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT count FROM poll_error_streaks WHERE repo = ? AND ticket_id = ?",
                identity,
            ).fetchone()
            if row is None:
                return
            connection.execute(
                "DELETE FROM poll_error_streaks WHERE repo = ? AND ticket_id = ?",
                identity,
            )
        if row[0] >= POLL_ERROR_STREAK:
            self.clear_attention(*identity, "poll-errors")

    def _record_job_error(
        self, job: Job, error: str | BaseException, *, prefix: str = ""
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO poll_errors (
                    repo, ticket_id, error, recorded_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    job.request.repo.lower(),
                    job.request.ticket_id,
                    prefix + poll_error_text(error),
                    self.queue.now(),
                ),
            )


class PollLoop:
    def __init__(
        self,
        dispatcher: Dispatcher,
        source: PollSource,
        *,
        sleep: Callable[[float], None] = time.sleep,
        interval_seconds: float | None = None,
    ):
        self.dispatcher = dispatcher
        self.source = source
        self.sleep = sleep
        if interval_seconds is None:
            # orch.yml poll_seconds (30 or 60) when the source carries it.
            config = getattr(source, "config", None)
            interval_seconds = getattr(config, "poll_seconds", POLL_SECONDS)
        self.interval_seconds = interval_seconds
        self.clock = CycleClock()

    def cycle_state(self) -> tuple[float | None, float | None]:
        return self.clock.state()

    def run(
        self,
        *,
        max_polls: int | None = None,
        on_error: Callable[[Exception], None] | None = None,
        after_poll: Callable[[], None] | None = None,
    ) -> None:
        """Poll forever (or `max_polls` times), sleeping between cycles.

        The sleep (`interval_seconds`, orch.yml poll_seconds) starts after a
        cycle finishes, so cycle start to cycle start is the cycle's own work
        plus the interval.

        `after_poll` (the dashboard push, when configured) runs after every
        cycle, including one whose error went to `on_error`. It must not block; if it raises, one line
        naming only the exception class goes to stderr and polling goes on.
        """
        if max_polls is not None and max_polls < 1:
            raise ValueError("max_polls must be positive")
        polls = 0
        while True:
            print(self.clock.start(), flush=True)
            self.dispatcher.cycle_clock = self.clock
            try:
                try:
                    self.dispatcher.poll_once(self.source)
                except Exception as exc:
                    if on_error is None:
                        raise
                    on_error(exc)
                # Not in a `finally`: a stop (SIGTERM, Ctrl-C) or a fatal --once
                # error must not start a push on the way out.
                if after_poll is not None:
                    with self.clock.phase("after-poll"):
                        _after_poll(after_poll)
            finally:
                print(self.clock.end(), flush=True)
            polls += 1
            if max_polls is not None and polls >= max_polls:
                return
            self.sleep(self.interval_seconds)


def _after_poll(hook: Callable[[], None]) -> None:
    try:
        hook()
    except Exception as exc:  # a dashboard push never breaks polling
        print(f"dashboard push: {type(exc).__name__}", file=sys.stderr, flush=True)
