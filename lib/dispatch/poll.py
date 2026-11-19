"""poll_once, handoff verification, poll-error streaks, attention, and PollLoop."""

from __future__ import annotations

import sqlite3
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from itertools import islice
from typing import TYPE_CHECKING

from lib import project_rules
from lib.dispatch import ci_board
from lib.dispatch.bookkeeping import LockLedger, bookkeeping_write
from lib.dispatch.constants import (
    GITHUB_STATE_LABEL_PREFIX,
    HANDED_OFF_EXTERNALLY,
    HARNESS_WORK,
    LINEAR_HANDOFF_QUERY,
    OFF_LOOP_VERIFY_READS,
    POLL_ERROR_RETENTION_SECONDS,
    POLL_ERROR_STREAK,
    SHIP_ERROR_LIMIT,
    _UNVERIFIED_HANDOFF,
)
from lib.dispatch.cycle_clock import (
    LOCK_ERRORS,
    POLLER_REPO,
    POLLER_TICKET,
    SLOW_CYCLE,
    SLOW_CYCLE_SECONDS,
    CycleClock,
)
from lib.dispatch.errors_text import poll_error_detail, poll_error_text
from lib.dispatch.helpers import _matching_head
from lib.dispatch.types import PollCycleFailed, PollSource, TicketRef, TrackedTicket
from lib.handoff import HandoffError, parse_comment
from lib.orch_yml import POLL_SECONDS
from lib.queue import Job, _job
from lib.wait import is_locked

if TYPE_CHECKING:
    from lib.dispatcher import Dispatcher


class PollMixin:
    cycle_clock: CycleClock | None = None

    @contextmanager
    def _phase(self, name: str) -> Iterator[None]:
        """A lock error ends only its own phase; the next phase still runs (#839)."""
        self.lock_ledger.phase = name
        try:
            with nullcontext() if self.cycle_clock is None else self.cycle_clock.phase(name):
                yield
        except sqlite3.OperationalError as exc:
            if not is_locked(exc):
                raise
            self.lock_ledger.missed("phase")
            self.record_poll_exception(name, "-", exc)

    def poll_once(self, source: PollSource) -> None:
        begin = getattr(source, "begin_cycle", None)
        end = getattr(source, "end_cycle", None)
        if hasattr(source, "cycle_clock"):
            source.cycle_clock = self.cycle_clock
        if begin is not None:
            begin()
        self._polled = {}
        self._ci_log_failures = set()
        self._ci_step_failures = set()
        self._read_started = None
        self._merged_repos = set()
        self.merged_this_poll = set()
        self._fresh_reader = getattr(source, "fresh_ticket", None)
        self._cycle_failures: list[str] = []
        self.lock_ledger.reset()
        if self.feeder is not None:
            self.feeder.ledger = self.lock_ledger
        self._send_attention(self.lock_ledger)
        tracked: tuple[TrackedTicket, ...] = ()
        read_from = self.queue.now()
        with self._phase("linear-states"):
            self._read_linear_states(source)
            self._apply_linear_moves()
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
            refs = self._tracked_refs(read_at, closed=self._closed_probe(source))
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
        with self._phase("linear-moves"):
            self._apply_linear_moves(only=self._polled)
        with self._phase("ci-board"):
            try:
                ci_board.record(
                    self._connect,
                    [
                        self._polled.get((ticket.repo.lower(), ticket.ticket_id), ticket)
                        for ticket in tracked
                    ],
                    self.queue.now(),
                    run_jobs=self.ci_runs,
                    job_log=self.ci_job_log,
                    log=self.log,
                )
            except Exception as exc:
                self.record_poll_exception("ci-board", "-", exc)
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
            reads = self._record_factory_reads(source)
        with self._phase("ci-pool"):
            self._read_ci_pool(reads)
        self._export_eval_log()
        self._prune_poll_errors()
        if not self._send_attention(None):
            self.lock_ledger.missed("attention")
        if self.lock_ledger.misses and self.log is not None:
            self.log(f"lock misses: {self.lock_ledger.summary()}")
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

    def _closed_probe(
        self, source: PollSource
    ) -> Callable[[tuple[TicketRef, ...]], frozenset[tuple[str, str]]] | None:
        """A stopped ticket waits STOPPED_POLL_SECONDS between full reads; a
        cheap state probe still reads it the poll its PR closes (#931)."""
        probe = getattr(source, "closed_pulls", None)
        if probe is None:
            return None

        def closed(refs: tuple[TicketRef, ...]) -> frozenset[tuple[str, str]]:
            try:
                return frozenset(probe(refs))
            except Exception as exc:
                self.record_poll_exception("closed-probe", "-", exc)
                return frozenset()

        return closed

    def _verify_handoffs(self, source: PollSource, read_from: float) -> None:
        """#18: check each done job's handoff comment once, on the ticket read.

        Only jobs finished before this poll read its tickets: the handoff
        must be one of the comments read, carry an orch:v1 header, and come
        from a login trusted for the job's recipient. Otherwise the ticket
        is flagged unverified-handoff. The job stays done either way. A
        ticket that left the poll loop (shipped, abandoned) is read here,
        newest job first, only while it has a job to verify.
        """
        with self._connect() as connection:
            parked = {
                (str(row["repo"]), str(row["ticket_id"]))
                for row in connection.execute(
                    "SELECT repo, ticket_id FROM ticket_state WHERE workflow_state = ?",
                    (HANDED_OFF_EXTERNALLY,),
                )
            }
        tickets = {key: ticket for key, ticket in self._polled.items() if key not in parked}
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
                job = _job(row, connection)
                problem = self._handoff_problem(ticket, job)
                event = "handoff-unverified" if problem else "handoff-verified"
                detail = problem or f"comment {job.handoff_comment_id} checked"
                with self._connect() as connection:
                    self._record_job_event(
                        connection, job, event, detail, self.queue.now()
                    )
                    if problem:
                        self.flag_attention(
                            ticket.repo,
                            ticket.ticket_id,
                            "unverified-handoff",
                            f"job {job.id[:8]} {job.request.action}: {problem}",
                            connection=connection,
                        )
                self._log_job_event(job, event, detail)

    def _unpolled_handoff_refs(self, read_from: float) -> tuple[TicketRef, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT tracked.repo, tracked.ticket_id, tracked.tracker
                FROM jobs
                JOIN tracked_tickets AS tracked
                  ON tracked.repo = lower(jobs.repo)
                 AND tracked.ticket_id = jobs.ticket_id
                LEFT JOIN ticket_state AS state
                  ON state.repo = tracked.repo AND state.ticket_id = tracked.ticket_id
                WHERE {_UNVERIFIED_HANDOFF}
                  AND COALESCE(state.workflow_state, '') != '{HANDED_OFF_EXTERNALLY}'
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

    def _record_factory_reads(self, source: PollSource) -> tuple[project_rules.FactoryRead, ...]:
        """Store each project's factory.md as this cycle read it (#152)."""
        reads = getattr(source, "factory_reads", None)
        if reads is None:
            return ()
        try:
            cycle = reads()
            with self._connect() as connection:
                project_rules.record_reads(connection, cycle, self.queue.now())
        except Exception as exc:
            self.record_poll_error("factory", "-", type(exc).__name__)
            return ()
        return tuple(cycle)

    def _read_ci_pool(self, reads: tuple[project_rules.FactoryRead, ...]) -> None:
        if self.ci_pool is None:
            return
        repos = sorted(
            {read.repo.lower() for read in reads if read.config is not None and read.config.ci_runner}
        )
        try:
            ci_board.read_pool(self._connect, repos, self.ci_pool, self.queue.now(), self.log)
        except Exception as exc:
            self.record_poll_exception("ci-pool", "-", exc)

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
        bookkeeping_write(
            self._connect,
            lambda connection: _write_reads(connection, keys, at),
            site="_record_reads",
            ledger=self.lock_ledger,
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
        identity = (repo.lower(), ticket_id)
        text = prefix + poll_error_text(error)
        now = self.queue.now()
        self._source_errors[identity] = poll_error_text(error)

        def work(connection: sqlite3.Connection) -> None:
            _write_reads(connection, {identity}, now)
            connection.execute(
                """
                INSERT INTO poll_errors (
                    repo, ticket_id, error, detail, recorded_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (*identity, text, detail, now),
            )

        if not bookkeeping_write(
            self._connect, work, site="record_poll_error", ledger=self.lock_ledger
        ):
            _print_deferred(repo, ticket_id, text)

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
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        """Show a ticket as needs-attention in orch status and the dashboard."""
        flag = (detail[:SHIP_ERROR_LIMIT], self.queue.now())
        self._note_attention((repo.lower(), ticket_id, reason), flag, connection)
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

    def clear_attention(
        self,
        repo: str,
        ticket_id: str,
        reason: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self._note_attention((repo.lower(), ticket_id, reason), None, connection)

    def _note_attention(
        self,
        key: tuple[str, str, str],
        flag: tuple[str, float] | None,
        connection: sqlite3.Connection | None,
    ) -> None:
        if connection is None:
            self.attention_outbox[key] = flag
            self._send_attention(self.lock_ledger)
            return
        self.attention_outbox.pop(key, None)
        _put_attention(connection, {key: flag})

    def _write_attention_outbox(
        self, connection: sqlite3.Connection
    ) -> dict[tuple[str, str, str], tuple[str, float] | None]:
        """Write every queued flag and clear in connection's transaction; pass
        the result to _settle_attention once that transaction commits."""
        pending = dict(self.attention_outbox)
        _put_attention(connection, pending)
        return pending

    def _settle_attention(
        self, sent: Mapping[tuple[str, str, str], tuple[str, float] | None]
    ) -> None:
        for key, flag in sent.items():
            if self.attention_outbox.get(key) == flag:
                del self.attention_outbox[key]

    def _send_attention(self, ledger: LockLedger | None) -> bool:
        if not self.attention_outbox:
            return True
        pending = dict(self.attention_outbox)
        if not bookkeeping_write(
            self._connect,
            lambda connection: _put_attention(connection, pending),
            site="attention",
            ledger=ledger,
        ):
            return False
        self._settle_attention(pending)
        return True

    def watch_cycle(self, clock: CycleClock) -> None:
        seconds = clock.last_cycle_seconds or 0.0
        phase, spent = clock.slowest() or ("-", 0.0)
        locked = self.cycle_alarms[LOCK_ERRORS]
        self._sound(
            SLOW_CYCLE,
            seconds > SLOW_CYCLE_SECONDS,
            lambda: f"poll cycle took {seconds:.0f}s (over {SLOW_CYCLE_SECONDS:g}s); "
            f"slowest phase {phase} {spent:.0f}s",
        )
        self._sound(
            LOCK_ERRORS,
            self.lock_ledger.misses > 0,
            lambda: f"{locked.bad} poll cycles in a row hit orch.db lock errors; "
            f"most in phase {self.lock_ledger.worst_phase()} ({self.lock_ledger.summary()})",
        )

    def _sound(self, reason: str, bad: bool, detail: Callable[[], str]) -> None:
        change = self.cycle_alarms[reason].observe(bad)
        if change == "raise":
            self.flag_attention(POLLER_REPO, POLLER_TICKET, reason, detail())
        elif change == "clear":
            self.clear_attention(POLLER_REPO, POLLER_TICKET, reason)

    def _record_poll_error(self, ticket: TrackedTicket, exc: BaseException) -> None:
        """Record a ticket's failed poll; the POLL_ERROR_STREAK-th failure
        in a row flags poll-errors attention."""
        self.record_poll_exception(ticket.repo, ticket.ticket_id, exc)
        self._advance_poll_error_streak(
            ticket.repo.lower(), ticket.ticket_id, type(exc).__name__
        )

    def _advance_poll_error_streak(self, repo: str, ticket_id: str, error: str) -> None:
        identity = (repo, ticket_id)
        now = self.queue.now()
        flagged = [False]

        def work(connection: sqlite3.Connection) -> None:
            count = connection.execute(
                """
                INSERT INTO poll_error_streaks (repo, ticket_id, count)
                VALUES (?, ?, 1)
                ON CONFLICT(repo, ticket_id) DO UPDATE SET count = count + 1
                RETURNING count
                """,
                identity,
            ).fetchone()[0]
            flagged[0] = count == POLL_ERROR_STREAK
            if flagged[0]:
                detail = f"{count} polls in a row failed: {error}"
                _write_attention(
                    connection, (*identity, "poll-errors", detail[:SHIP_ERROR_LIMIT], now)
                )

        written = bookkeeping_write(
            self._connect, work, site="_advance_poll_error_streak", ledger=self.lock_ledger
        )
        if written and flagged[0] and self.log is not None:
            self.log(f"needs attention: {repo}#{ticket_id} poll-errors")

    def _clear_poll_error_streak(self, ticket: TrackedTicket) -> None:
        identity = (ticket.repo.lower(), ticket.ticket_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT count FROM poll_error_streaks WHERE repo = ? AND ticket_id = ?",
                identity,
            ).fetchone()
        if row is None:
            return

        def work(connection: sqlite3.Connection) -> None:
            connection.execute(
                "DELETE FROM poll_error_streaks WHERE repo = ? AND ticket_id = ?",
                identity,
            )
            if row[0] >= POLL_ERROR_STREAK:
                _delete_attention(connection, *identity, "poll-errors")

        bookkeeping_write(
            self._connect, work, site="_clear_poll_error_streak", ledger=self.lock_ledger
        )

    def _record_job_error(
        self, job: Job, error: str | BaseException, *, prefix: str = ""
    ) -> None:
        text = prefix + poll_error_text(error)
        values = (job.request.repo.lower(), job.request.ticket_id, text, self.queue.now())
        if not bookkeeping_write(
            self._connect,
            lambda connection: connection.execute(
                """
                INSERT INTO poll_errors (
                    repo, ticket_id, error, recorded_at
                ) VALUES (?, ?, ?, ?)
                """,
                values,
            ),
            site="_record_job_error",
            ledger=self.lock_ledger,
        ):
            _print_deferred(job.request.repo, job.request.ticket_id, text)

    def _emptied_by_running_job(self, ticket: TrackedTicket) -> bool:
        if not (ticket.pr_commits == 0 and ticket.git_branch_name and ticket.pr_base):
            return False
        with self._connect() as connection:
            return connection.execute(
                f"""
                SELECT 1 FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND state = 'running'
                  AND {HARNESS_WORK}
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone() is not None

    def _reopen_emptied_pull(self, ticket: TrackedTicket) -> bool:
        """GitHub auto-closes a PR whose branch has no commits beyond base and
        freezes its head, so a rebuilt branch's ready is never read until the
        PR is reopened (#705)."""
        if not (
            ticket.closed
            and ticket.pr_commits == 0
            and ticket.git_branch_name
            and ticket.pr_base
        ) or self._finished_identity(ticket.repo, ticket.ticket_id):
            return False
        actions = self._review_actions(ticket)
        tip = actions.branch_tip(ticket, ticket.git_branch_name)
        if _matching_head(tip, ticket.current_head) is not None:
            return False
        if actions.commits_ahead(ticket, base=ticket.pr_base, head=tip) <= 0:
            return False
        actions.reopen(ticket)
        if self.log is not None:
            self.log(f"reopened: {ticket.repo}#{ticket.ticket_id} branch is ahead at {tip}")
        return True


class PollLoop:
    def __init__(
        self,
        dispatcher: Dispatcher,
        source: PollSource,
        *,
        sleep: Callable[[float], None] | None = None,
        interval_seconds: float | None = None,
    ):
        self.dispatcher = dispatcher
        self.source = source
        self.sleep = sleep_on_either_clock if sleep is None else sleep
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
        if hasattr(after_poll, "cycle_clock"):
            after_poll.cycle_clock = self.clock
        while True:
            slow = self.clock.between(self.interval_seconds)
            if slow is not None:
                print(slow, flush=True)
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
            watch = getattr(self.dispatcher, "watch_cycle", None)
            if watch is not None:
                watch(self.clock)
            polls += 1
            if max_polls is not None and polls >= max_polls:
                return
            self.sleep(self.interval_seconds)


def _write_reads(
    connection: sqlite3.Connection, keys: set[tuple[str, str]], at: float
) -> None:
    connection.executemany(
        """
        INSERT INTO ticket_reads (repo, ticket_id, read_at) VALUES (?, ?, ?)
        ON CONFLICT (repo, ticket_id) DO UPDATE SET read_at = excluded.read_at
        """,
        [(repo, ticket_id, at) for repo, ticket_id in keys],
    )


def _write_attention(
    connection: sqlite3.Connection, values: tuple[str, str, str, str, float]
) -> None:
    connection.execute(
        """
        INSERT INTO needs_attention (repo, ticket_id, reason, detail, raised_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(repo, ticket_id, reason) DO UPDATE SET
            detail = excluded.detail, raised_at = excluded.raised_at
        """,
        values,
    )


def _delete_attention(
    connection: sqlite3.Connection, repo: str, ticket_id: str, reason: str
) -> None:
    connection.execute(
        "DELETE FROM needs_attention WHERE repo = ? AND ticket_id = ? AND reason = ?",
        (repo.lower(), ticket_id, reason),
    )


def _put_attention(
    connection: sqlite3.Connection,
    flags: Mapping[tuple[str, str, str], tuple[str, float] | None],
) -> None:
    for (repo, ticket_id, reason), flag in flags.items():
        if flag is None:
            _delete_attention(connection, repo, ticket_id, reason)
        else:
            _write_attention(connection, (repo, ticket_id, reason, *flag))


def _print_deferred(repo: str, ticket_id: str, error: str) -> None:
    print(f"orch poll: deferred poll error {repo} {ticket_id} {error}", file=sys.stderr, flush=True)


def sleep_on_either_clock(
    seconds: float,
    *,
    wall: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    wall_deadline = wall() + seconds
    monotonic_deadline = monotonic() + seconds
    while (left := min(wall_deadline - wall(), monotonic_deadline - monotonic())) > 0:
        sleep(min(1.0, left))


def _after_poll(hook: Callable[[], None]) -> None:
    try:
        hook()
    except Exception as exc:  # a dashboard push never breaks polling
        print(f"dashboard push: {type(exc).__name__}", file=sys.stderr, flush=True)
