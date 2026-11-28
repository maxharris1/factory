"""T6 and job state: reconcile, abandoned and dead jobs, devices, failure waits and stuck wakes."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from lib import device_sync, job_detail, lane_limit, skill_preflight
from lib.device_memory import MEMORY_WAIT
from lib.device_pool import (
    UNREACHABLE_BACKOFF_SECONDS,
    account_loads,
    back_off,
    backed_off_through,
    device_lost_detail,
    free_slots,
    memory_holds,
    orphan_slot,
    placement_blocks,
    release_unstarted_claim,
    used_slots,
)
from lib.dispatch.constants import (
    HANDED_OFF_EXTERNALLY,
    SETTLE_CLEARS,
    STACK_PARENT_CLOSED,
    STUCK_WAKE_FAILURE_LIMIT,
    _ABANDONED_JOB_DETAIL,
)
from lib.dispatch.decisions import record_decisions
from lib.dispatch.helpers import (
    _HANDOFF_UNREAD,
    _REJECTED_OUTCOMES,
    _SETTLING_KINDS,
    _NO_READ_SINCE_FAILURE,
    _UPDATE_NO_HANDOFF_OUTCOMES,
    _append_detail,
    _asks_questions,
    _base_mismatch,
    _comment_order,
    _detail_project_bot,
    _detail_round,
    _detail_round_number,
    _matching_head,
    _merge_only,
    _queue_state,
    _retry_detail,
    _round_number,
    _scope,
)
from lib.dispatch.transitions import write_workflow_state
from lib.dispatch.types import (
    BaseMismatch,
    Comment,
    HandoffCandidate,
    RouteError,
    TicketRef,
    TrackedTicket,
)
from lib.handoff import HandoffError, parse_comment
from lib.job_state import (
    DEAD,
    DEVICE_LOST,
    LAGGING_WAIT_SECONDS,
    LINK_DROP_BACKOFF_SECONDS,
    MAX_LINK_DROP_REQUEUES,
    RELEASED_UNSTARTED,
    RUN_DIED,
    STALE,
    Liveness,
    claimed_too_long,
    derive,
    fail_lag_blocked,
    lagging_end,
    lagging_requeue,
    last_run_end,
    link_drop_end,
    link_drop_requeues,
    link_drop_settled_at,
    read_placements,
    release_link_dropped,
    requeue_lagging,
    requeue_link_drop,
    requeue_signed_out_run,
    requeue_unstarted,
    requeue_usage_limited,
    signout_end,
    unstarted_too_long,
    usage_limit_end,
)
from lib.machines_yml import Device, MachinesConfig, orch_host
from lib.max_actions import may_hand_off
from lib.orch_yml import DEFAULT_JOB_TIMEOUT_MINUTES
from lib.pre_ready import planned
from lib.queue import (
    CHECK_FAILED_EVENTS,
    CHECK_PASSED_EVENTS,
    LOCAL_GATE_UNAVAILABLE,
    QA_RECIPIENT,
    RECIPIENTS,
    SLOTS_TABLE,
    Job,
    JobRequest,
    _job,
    plan_round,
    restored_claim,
    retire_requeued,
)
from lib.schema import HARNESSES


@dataclass(frozen=True)
class _LinkDrop:
    detail: str
    used: int

    @property
    def spent(self) -> str:
        return f"link-drop on {self.detail} ({self.used} free requeues spent)"


def _serving_devices(
    config: MachinesConfig, harness: str, allowed: frozenset[str] | None
) -> list[Device]:
    if allowed is None:
        return [device for device in config.devices if device.eligible(harness)]
    return [
        device for device in config.devices if device.name in allowed and harness in device.harnesses
    ]


def _harnesses_a_lagging_source_checkout_refuses_everywhere(
    rows: list[device_sync.Row], box: str
) -> frozenset[str]:
    source = skill_preflight.ROOT.resolve()
    return frozenset(
        row.harness
        for row in rows
        if row.device == box
        and row.state == device_sync.LAGGING
        and Path(row.checkout).resolve() == source
    )


class JobsMixin:
    def _t5_abandoned(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> str:
        """#19 FU4: Grok's answers state=abandoned records Max dropping the ticket."""
        if not self._trusted(
            ticket, comment, fields, "grok-bot", role="orchestrator"
        ):
            return "rejected-answers-author"
        held = self._held_stop(ticket)
        split = [] if held is None else [self._stop_decision(ticket, held[1], held[0], comment, "Split")]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record_decisions(connection, ticket, None if held is None else held[1].id, split)
            write_workflow_state(
                connection,
                ticket.repo,
                ticket.ticket_id,
                "abandoned",
                source="t5-abandoned",
                at=self.queue.now(),
                insert={"gate_count": 0, "stuck": 0, "generation": 1},
            )
            closed = self._fail_abandoned_queued(connection)
            connection.commit()
        self._log_abandoned_jobs(closed)
        self._write_state_label(ticket, "abandoned")
        return "abandoned"

    def _t5_handed_off(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> str:
        """#891: an approver's answers state=handed-off hands off a ticket that waits on Max."""
        if not (
            self.approvals.may_approve(comment.author)
            and self._trusted(ticket, comment, fields, "grok-bot", role="orchestrator")
        ):
            return "rejected-handoff-author"
        repo, ticket_id = ticket.repo.lower(), ticket.ticket_id
        with self._connect() as connection:
            row = connection.execute(
                "SELECT workflow_state, stuck, paused_action FROM ticket_state"
                " WHERE repo = ? AND ticket_id = ?",
                (repo, ticket_id),
            ).fetchone()
        if row is None:
            return "rejected-handoff-state"
        state, stuck, paused = row["workflow_state"], bool(row["stuck"]), row["paused_action"]
        if state == "handed-off":
            # A replay after the label write below failed.
            self._write_state_label(ticket, "handed-off")
            return "handed-off"

        def settle(connection: sqlite3.Connection) -> bool:
            running = connection.execute(
                "SELECT 1 FROM jobs WHERE lower(repo) = ? AND ticket_id = ? AND state = 'running'",
                (repo, ticket_id),
            ).fetchone()
            if not (
                may_hand_off(state, stuck, paused, running is not None)
                and write_workflow_state(
                    connection,
                    repo,
                    ticket_id,
                    "handed-off",
                    source="answers-handoff",
                    at=self.queue.now(),
                    also=SETTLE_CLEARS,
                    guard="workflow_state = ? AND stuck = ? AND paused_action IS ?",
                    guard_args=(state, int(stuck), paused),
                )
            ):
                return False
            self._settle_failures_for_handoff(connection, repo, ticket_id)
            return True

        if not self._settle(repo, ticket_id, "handed-off", settle):
            return "rejected-handoff-state"
        self._write_state_label(ticket, "handed-off")
        return "handed-off"

    def _settle_failures_for_handoff(
        self, connection: sqlite3.Connection, repo: str, ticket_id: str
    ) -> None:
        now = self.queue.now()
        rows = connection.execute(
            "SELECT * FROM jobs WHERE lower(repo) = ? AND ticket_id = ?"
            " AND state = 'failed' AND failure_handled = 0 AND recipient != ?",
            (repo, ticket_id, QA_RECIPIENT),
        ).fetchall()
        for row in rows:
            job = _job(row, connection)
            connection.execute("UPDATE jobs SET failure_handled = 1 WHERE id = ?", (job.id,))
            self._record_job_event(
                connection, job, "handed-off", "Max handed the ticket off; no T6 retry", now
            )

    def _placement_config(self) -> MachinesConfig | None:
        pool = getattr(self.devices, "pool", None)
        if pool is not None:
            return pool.placement_config()
        return getattr(self.devices, "config", None)

    def _no_free_slot(
        self, config: MachinesConfig | None, *, memory: bool = True
    ) -> Callable[[Job], bool]:
        if config is None:
            return lambda job: False
        used = used_slots(self.queue, config)
        blocked = placement_blocks(self.queue, config, self._head(), memory=memory)
        pins = self._pins()

        def full(job: Job) -> bool:
            harness = job.request.recipient
            allowed = pins.get(job.request.repo.lower())
            return (
                harness in HARNESSES
                and bool(_serving_devices(config, harness, allowed))
                and free_slots(config, harness, used, blocked, allowed=allowed) == 0
            )

        return full

    def _memory_wait(self, config: MachinesConfig | None) -> Callable[[Job], str | None]:
        pool = getattr(self.devices, "pool", None)
        unknown_pins = pool is not None and pool.pins() is None
        holds = {} if config is None or unknown_pins else memory_holds(self.queue)
        if not holds:
            return lambda job: None
        full = self._no_free_slot(config)
        full_apart_from_memory = self._no_free_slot(config, memory=False)
        pins = self._pins()

        def waiting(job: Job) -> str | None:
            if job.state != "queued" or not full(job) or full_apart_from_memory(job):
                return None
            harness = job.request.recipient
            held = ", ".join(
                f"{device.name} ({holds[device.name]})"
                for device in _serving_devices(config, harness, pins.get(job.request.repo.lower()))
                if device.name in holds
            )
            return f"queued; every free {harness} slot is on a memory-held device: {held}"

        return waiting

    def _account_held(self) -> Callable[[Job], bool]:
        config = getattr(self.devices, "config", None)
        if config is None:
            return lambda job: False
        with self.queue.connect() as connection:
            loads = account_loads(connection, config, self.queue.now())
        used = used_slots(self.queue, config)
        blocked = placement_blocks(self.queue, config, self._head())
        pins = self._pins()

        def held(job: Job) -> bool:
            harness = job.request.recipient
            allowed = pins.get(job.request.repo.lower())
            pairs = {
                (account, device.name)
                for device in config.devices
                if device.takes_new(harness) and (allowed is None or device.name in allowed)
                for account in config.accounts_on(device.name, harness)
            }
            return (
                any(loads.holds(account, device) for account, device in pairs)
                and free_slots(config, harness, used, blocked, allowed=allowed) == 0
            )

        return held

    def _pins(self) -> Mapping[str, frozenset[str]]:
        pool = getattr(self.devices, "pool", None)
        pins = None if pool is None else pool.pins()
        if pins is not None:
            return pins
        return {
            repo: project.devices
            for repo, project in self.wake_routes.items()
            if project.devices is not None
        }

    def _head(self) -> str | None:
        pool = getattr(self.devices, "pool", None)
        return None if pool is None else pool.head()

    def _serving(self) -> dict[str, frozenset[str]]:
        pool = getattr(self.devices, "pool", None)
        if pool is None:
            return {}
        return {harness: pool.serving(harness) for harness in HARNESSES}

    def _backed_off_through(self) -> dict[str, float]:
        config = getattr(self.devices, "config", None)
        if config is None:
            return {}
        return backed_off_through(self.queue, config)

    def _lag_blocked_recipients(self) -> frozenset[str]:
        config = getattr(self.devices, "config", None)
        if config is None:
            return frozenset()
        with self.queue.connect() as connection:
            rows = device_sync.read(connection)
        blocked = device_sync.checkout_blocks(rows, self._head(), orch_host(config))
        refused_everywhere = _harnesses_a_lagging_source_checkout_refuses_everywhere(
            rows, orch_host(config)
        )
        return frozenset(
            harness
            for harness in HARNESSES
            if any(device.eligible(harness) for device in config.devices)
            and (
                harness in refused_everywhere
                or all(
                    blocked.blocks(device.name, harness)
                    for device in config.devices
                    if device.eligible(harness)
                )
            )
        )

    def _fail_lag_blocked_past_wait(self, job: Job, now: float, paused: float) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lagging = lagging_requeue(connection, job.id)
            failed = (
                lagging is not None
                and lagging.past_wait(now, since=paused)
                and fail_lag_blocked(
                    connection,
                    job.id,
                    now=now,
                    detail=f"queued; every {job.request.recipient} checkout is lagging",
                )
            )
            connection.commit()
        if failed:
            self._log_job_event(job, "lag-blocked", "queued; every checkout is lagging")
        return failed

    def _restart_queue_wait(self, job: Job, now: float) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs SET last_activity_at = ?
                WHERE id = ? AND state = 'queued'
                """,
                (now, job.id),
            )

    def reconcile_jobs(self) -> None:
        deferred_grok_jobs: set[str] = set()
        self._close_abandoned_jobs()
        self._backstop_driver_wakes()
        self._deliver_grok_jobs(deferred_grok_jobs)
        self._check_devices()
        self._check_job_states()
        self._close_abandoned_jobs()
        now = self.queue.now()
        placement = self._placement_config()
        no_free_slot = self._no_free_slot(placement)
        memory_wait = self._memory_wait(placement)
        account_held = self._account_held()
        lag_blocked = self._lag_blocked_recipients()
        serving = self._serving()
        with self._connect() as connection:
            limited = {
                recipient
                for recipient in RECIPIENTS
                if lane_limit.recipient_paused(connection, recipient, now, serving.get(recipient))
            }
            paused = lane_limit.paused_through(connection, now, serving)
        backed_off = self._backed_off_through()
        for job in self.queue.outstanding():
            if job.state == "queued" and (job.request.recipient in limited or account_held(job)):
                self._restart_queue_wait(job, now)
                continue
            # A queued job's last_activity_at is its queue-wait start: when
            # it was queued, or when a claim that never started was released
            # (#77 F1.2), so a release never times out a job for the wait
            # before its claim. A running job's is its last heartbeat.
            waited_since = job.last_activity_at
            not_waiting_through = max(
                paused.get(job.request.recipient, 0.0),
                backed_off.get(job.request.recipient, 0.0),
            )
            if job.state == "queued" and not_waiting_through > waited_since:
                waited_since = not_waiting_through
                self._restart_queue_wait(job, waited_since)
            if (
                job.state == "queued"
                and job.request.recipient in lag_blocked
                and self._fail_lag_blocked_past_wait(job, now, paused.get(job.request.recipient, 0.0))
            ):
                continue
            detail = memory_wait(job)
            if detail is not None:
                self._record_job_event_once(job, MEMORY_WAIT, detail)
                continue
            if now - waited_since >= job.request.timeout_seconds:
                if job.state == "queued" and no_free_slot(job):
                    self._restart_queue_wait(job, now)
                    continue
                if not self.queue.mark_timed_out(job) and job.state == "running":
                    self._note_timeout_held(job, now)
        for job in self.queue.unhandled_failures():
            self._t6(job)
        self._ensure_all_stuck_wakes()
        self._deliver_grok_jobs(deferred_grok_jobs)
        self._drain_driver_inbox()

    def _close_abandoned_jobs(self) -> None:
        """#109: a queued job on an abandoned ticket never starts.

        Claims already skip it (lib/queue.py claim_order); this fails it with
        its failure handled, so T6 neither retries it nor marks the ticket
        stuck. Abandonment does this in its own transaction; reconciliation
        repeats it for a job requeued since, before Grok delivery and again
        after the job-state checks.
        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            closed = self._fail_abandoned_queued(connection)
            connection.commit()
        self._log_abandoned_jobs(closed)

    def _fail_abandoned_queued(self, connection: sqlite3.Connection) -> list[Job]:
        now = self.queue.now()
        rows = connection.execute(
            """
            SELECT jobs.* FROM jobs
            JOIN ticket_state AS ticket
              ON ticket.repo = lower(jobs.repo) AND ticket.ticket_id = jobs.ticket_id
            WHERE jobs.state = 'queued' AND ticket.workflow_state = 'abandoned'
            ORDER BY jobs.created_at, jobs.id
            """
        ).fetchall()
        closed = [_job(row, connection) for row in rows]
        for job in closed:
            connection.execute(
                """
                UPDATE jobs
                SET state = 'failed', failure_handled = 1,
                    finished_at = ?, last_activity_at = ?
                WHERE id = ? AND state = 'queued'
                """,
                (now, now, job.id),
            )
            self._record_job_event(
                connection, job, "abandoned", _ABANDONED_JOB_DETAIL, now
            )
        return closed

    def _log_abandoned_jobs(self, closed: list[Job]) -> None:
        for job in closed:
            self._log_job_event(job, "abandoned", _ABANDONED_JOB_DETAIL)

    def _note_timeout_held(self, job: Job, now: float) -> None:
        """#87 r3 F2.1: a timed-out job whose worker may still run stays running.

        The queue refuses to time out a running job whose slot records a
        remote start, so T6 never queues attempt 2 beside it. The job
        leaves running through the dead path once its runner and worker are
        confirmed gone (then T6 retries as before), or as lost when its
        device stays unreachable (#662). This only records the hold, once
        per job, so `orch status` and the dashboard's job events show why a
        stale job has not been retried. A failure here never stops the poll.
        """
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                placement = read_placements(connection).get(job.id)
                started = placement is not None and placement.remote_started
                noted = connection.execute(
                    "SELECT 1 FROM job_events WHERE job_id = ? AND event = 'timeout-held'",
                    (job.id,),
                ).fetchone()
                row = connection.execute(
                    "SELECT state FROM jobs WHERE id = ?", (job.id,)
                ).fetchone()
                if not started or noted is not None or row is None or row[0] != "running":
                    connection.commit()
                    return
                assert placement is not None
                detail = (
                    f"no heartbeat for {int((now - job.last_activity_at) // 60)} minutes, "
                    f"past its {int(job.request.timeout_seconds // 60)}-minute timeout, "
                    f"but its worker on {placement.device} is not confirmed gone; "
                    "no retry until it is"
                )
                self._record_job_event(connection, job, "timeout-held", detail, now)
                connection.commit()
        except Exception as exc:
            self.record_poll_error(
                job.request.repo, job.request.ticket_id, exc, prefix="timeout-held:"
            )
            return
        self._log_job_event(job, "timeout-held", detail)

    def _check_devices(self) -> None:
        """F2-02: free gone workers' slots; #662: lose the running jobs of a
        device unreachable for job_states.unreachable_lost_seconds.

        Runs before T6 looks at timeouts, so T6 sees a lost job as a link
        drop in the same poll. #679: machines.yml is reloaded first. #1226:
        each device's memory is read and an over-limit device held. #1101:
        the account doctor runs last when due. A failure of any step is
        recorded and never stops the poll.
        """
        if self.devices is None:
            return
        reload = getattr(self.devices, "reload", None)
        if reload is not None:
            try:
                reload()
            except Exception as exc:
                self.record_poll_error("devices", "-", exc, prefix="machines.yml:")
        try:
            self.devices.reconcile(
                self.queue.now(), self.job_states.unreachable_lost_seconds, self._device_lost
            )
        except Exception as exc:
            self.record_poll_error("devices", "-", exc)
        check_memory = getattr(self.devices, "check_memory", None)
        if check_memory is not None:
            try:
                check_memory(self.log or (lambda _line: None))
            except Exception as exc:
                self.record_poll_error("devices", "-", exc, prefix="memory: ")
        check = getattr(self.devices, "check_sign_ins", None)
        if check is not None:
            try:
                check(self.queue.now())
            except Exception as exc:
                self.record_poll_error("devices", "-", exc, prefix="account doctor:")

    def _check_job_states(self) -> None:
        """#75: requeue jobs claimed but never started; fail dead jobs.

        #91: a placed job whose worker never started and that no live runner
        holds is requeued the same way (lib/job_state.py unstarted_too_long,
        within the free-requeue budget), and a job whose `orch job run`
        recorded that its command died (run-died) and freed its slot is
        failed for T6 once its heartbeat is stale.

        Each running job's state comes from lib/job_state.py. A job claimed
        for more than job_states.release_claimed_seconds goes back to
        queued as the same job row (same round, same T6 attempt). A dead
        job is failed now, so T6 handles it as it would a timeout. Both
        writes are guarded, so a runner that starts or heartbeats in
        between wins. Runs after the device checks and before the timeouts.
        A failure is recorded and never stops the poll.
        """
        try:
            now = self.queue.now()
            with self._connect() as connection:
                connection.execute("BEGIN")
                rows = connection.execute(
                    "SELECT * FROM jobs WHERE state = 'running' ORDER BY created_at, id"
                ).fetchall()
                jobs = [_job(row, connection) for row in rows]
                placements = read_placements(connection)
                connection.commit()
        except Exception as exc:
            self.record_poll_error("job-states", "-", exc)
            return
        check = getattr(self.devices, "liveness", None)
        for job in jobs:
            try:
                placement = placements.get(job.id)
                facts = (
                    check(placement)
                    if check is not None
                    and placement is not None
                    and placement.runner_pid is not None
                    else Liveness()
                )
                state = derive(
                    last_activity_at=job.last_activity_at,
                    now=now,
                    placement=placement,
                    stale_seconds=self.job_states.stale_seconds,
                    liveness=facts,
                )
                if claimed_too_long(
                    state, placement, now, self.job_states.release_claimed_seconds
                ):
                    assert placement is not None
                    self._release_claimed(job, placement.placed_at, now)
                elif unstarted_too_long(
                    state,
                    placement,
                    facts,
                    last_activity_at=job.last_activity_at,
                    now=now,
                    release_claimed_seconds=self.job_states.release_claimed_seconds,
                ):
                    assert placement is not None
                    self._release_unstarted(job, placement.placed_at, placement.runner_pid, now)
                elif (
                    placement is not None
                    and placement.remote_started
                    and facts.runner_alive is False
                    and facts.worker_gone is None
                    and self._link_drop_settled(job, now)
                ):
                    self._fail_dead(job, placement.runner_pid, now, link_drop=placement.device)
                elif (
                    placement is not None
                    and facts.runner_alive is False
                    and facts.worker_gone is False
                    and (handoff := self._handoff_candidate(job, skip_unread=True)) is not None
                ):
                    self._fail_with_leftovers(job, placement.runner_pid, handoff.comment, now)
                elif state == DEAD:
                    assert placement is not None
                    self._fail_dead(job, placement.runner_pid, now)
                elif placement is None and state == STALE:
                    self._fail_died_run(job, now)
            except Exception as exc:
                self.record_poll_error(
                    job.request.repo, job.request.ticket_id, exc, prefix="job-states:"
                )

    def _release_claimed(self, job: Job, placed_at: float, now: float) -> None:
        """Requeue a job its bot claimed and never started (no round charged).

        One transaction, guarded: the job is still running with no
        heartbeat since the claim, and its slot is the same placement with
        no runner recorded. Its slot is freed; a late `orch job run` then
        finds the job queued and refuses it before anything starts. Its
        queue-wait clock (last_activity_at) restarts now, so the timeout
        sweep never fails it for time spent before the release; created_at
        is kept, so it keeps its place in line. On a finished ticket, or for
        a restored claim, the job is cancelled instead (retire_requeued, #108).
        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                f"""
                UPDATE jobs SET state = 'queued', last_activity_at = ?
                WHERE id = ? AND state = 'running' AND last_activity_at = ?
                  AND EXISTS (
                    SELECT 1 FROM {SLOTS_TABLE}
                    WHERE job_id = ? AND placed_at = ? AND runner_pid IS NULL
                  )
                """,
                (now, job.id, job.last_activity_at, job.id, placed_at),
            ).rowcount
            if changed != 1:
                connection.commit()
                return
            release_unstarted_claim(connection, job.id)
            reason = (
                f"{job.request.recipient} claimed it {int((now - placed_at) // 60)} "
                "minutes ago and never started it"
            )
            retired = retire_requeued(connection, job.id, reason, now=now)
            if retired is not None:
                event, detail = "cancelled", retired
            else:
                event, detail = "released-claimed", f"{reason}; requeued, no round charged"
                self._record_job_event(connection, job, event, detail, now)
            connection.commit()
        self._log_job_event(job, event, detail)

    def _release_unstarted(
        self, job: Job, placed_at: float, runner_pid: int | None, now: float
    ) -> None:
        """#91: requeue a placed job whose worker never started (no round charged).

        Guarded like `_release_claimed`: still running, no heartbeat since it
        was read, the same placement and runner, and still no worker pid or
        remote start. Past the free-requeue budget it is failed for T6.
        """
        if runner_pid is None:
            reason = (
                f"{job.request.recipient}'s run never recorded a runner or started a worker "
                f"and nothing has heartbeated for {int((now - job.last_activity_at) // 60)} minutes"
            )
        else:
            reason = f"runner pid {runner_pid} is gone and never started a worker"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = requeue_unstarted(
                connection,
                job.id,
                now=now,
                event=RELEASED_UNSTARTED,
                reason=reason,
                slot_guard=(
                    "placed_at = ? AND runner_pid IS ? AND worker_pid IS NULL "
                    "AND COALESCE(remote_started, 0) = 0"
                ),
                slot_params=(placed_at, runner_pid),
                last_activity_at=job.last_activity_at,
            )
            connection.commit()
        if result is not None:
            self._log_job_event(job, *result)

    def _fail_died_run(self, job: Job, now: float) -> None:
        """#91: fail a job whose `orch job run` recorded that its command died.

        The run freed the slot (its worker was confirmed gone) and the job is
        still running with a stale heartbeat. Only a RUN_DIED recorded at or
        after the last heartbeat counts: a command that exited 0 is left to
        its bot's `orch job done` (or the timeout). Guarded: still running,
        no heartbeat since it was read, and still no slot.
        """
        with self._connect() as connection:
            ended = last_run_end(connection, job.id, job.last_activity_at)
        if ended is None or ended[0] != RUN_DIED:
            return  # read only: most stale slotless jobs (Grok wakes) stop here
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                f"""
                UPDATE jobs
                SET state = 'failed', finished_at = ?, last_activity_at = ?
                WHERE id = ? AND state = 'running' AND last_activity_at = ?
                  AND NOT EXISTS (SELECT 1 FROM {SLOTS_TABLE} WHERE job_id = ?)
                """,
                (now, now, job.id, job.last_activity_at, job.id),
            ).rowcount
            if changed != 1:
                connection.commit()
                return
            detail = (
                f"orch job run's command ended ({ended[1]}) without orch job done "
                "and its slot is free; failed for T6"
            )
            self._record_job_event(connection, job, "dead", detail, now)
            connection.commit()
        self._log_job_event(job, "dead", detail)

    def _link_drop_settled(self, job: Job, now: float) -> bool:
        """#218: the job's latest run ended in a link drop and the device has
        since stopped its worker on its own, reachable or not."""
        with self._connect() as connection:
            dropped = link_drop_end(connection, job.id)
        return dropped is not None and now >= link_drop_settled_at(
            dropped[1], job.request.timeout_seconds
        )

    def _fail_dead(
        self, job: Job, runner_pid: int | None, now: float, *, link_drop: str | None = None
    ) -> None:
        """Fail a job whose runner and worker are gone; T6 takes it from here.

        Guarded like a timeout (no heartbeat since it was read) and on the
        same recorded runner, so a new `orch job run` that took the job
        over in between is left alone. #218: `link_drop` names the
        unreachable device of a link-dropped run whose worker is past its
        device-enforced deadline (`_link_drop_settled`).
        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                f"""
                UPDATE jobs
                SET state = 'failed', finished_at = ?, last_activity_at = ?
                WHERE id = ? AND state = 'running' AND last_activity_at = ?
                  AND EXISTS (
                    SELECT 1 FROM {SLOTS_TABLE} WHERE job_id = ? AND runner_pid = ?
                  )
                """,
                (now, now, job.id, job.last_activity_at, job.id, runner_pid),
            ).rowcount
            if changed != 1:
                connection.commit()
                return
            detail = f"runner pid {runner_pid} and its worker are gone; failed for T6"
            if link_drop is not None:
                detail = (
                    f"link drop on {link_drop}; runner pid {runner_pid} is gone and its "
                    "worker is past its device-enforced deadline; failed for T6"
                )
            ended = last_run_end(connection, job.id, job.last_activity_at)
            if ended is not None:
                detail += f" (its command: {ended[1]})"  # #91: the exit reason
            self._record_job_event(connection, job, "dead", detail, now)
            connection.commit()
        self._log_job_event(job, "dead", detail)

    def _fail_with_leftovers(
        self, job: Job, runner_pid: int | None, handoff: Comment, now: float
    ) -> None:
        """#1209: fail a job whose runner is gone and whose handoff is posted,
        while leftovers keep its worker check RUNNING, so T6 settles it from
        that handoff and the orphan sweep stops its scope."""
        kind = parse_comment(handoff.body).as_dict()["kind"]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                f"""
                UPDATE jobs
                SET state = 'failed', finished_at = ?, last_activity_at = ?,
                    failure_handled = 0
                WHERE id = ? AND state = 'running' AND last_activity_at = ?
                  AND EXISTS (
                    SELECT 1 FROM {SLOTS_TABLE} WHERE job_id = ? AND runner_pid = ?
                  )
                """,
                (now, now, job.id, job.last_activity_at, job.id, runner_pid),
            ).rowcount
            if changed != 1:
                connection.commit()
                return
            detail = (
                f"runner pid {runner_pid} is gone and its {kind} comment {handoff.id} "
                "is posted, but its worker check still says running; slot orphaned, failed for T6"
            )
            self._record_job_event(connection, job, "dead", detail, now)
            orphan_slot(connection, job.id, now)
            connection.commit()
        self._log_job_event(job, "dead", detail)

    def _record_job_event(
        self,
        connection: sqlite3.Connection,
        job: Job,
        event: str,
        detail: str,
        now: float,
    ) -> None:
        connection.execute(
            """
            INSERT INTO job_events (
                job_id, repo, ticket_id, action, recipient, event, detail, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.id,
                job.request.repo.lower(),
                job.request.ticket_id,
                job.request.action,
                job.request.recipient,
                event,
                detail,
                now,
            ),
        )

    def _log_job_event(self, job: Job, event: str, detail: str) -> None:
        if self.log is None:
            return
        self.log(
            f"job {job.id[:8]} {job.request.repo}#{job.request.ticket_id} "
            f"{job.request.action}: {event}: {detail}"
        )

    def _device_lost(self, job_id: str, device: str, reason: str) -> None:
        """#662: fail a running job lost with its unreachable device, unhandled.

        One transaction: the job leaves running, DEVICE_LOST is recorded in
        the link-drop detail format, its slot moves to device_orphans (the
        worker may still run there) and the device is backed off. T6 then
        requeues the same row under the link-drop budget.
        """
        now = self.queue.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lost = self._lose_running_job(connection, job_id, device, reason, now)
            connection.commit()
        if lost is not None:
            self._log_job_event(lost[0], DEVICE_LOST, lost[1])

    def _lose_running_job(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        device: str,
        reason: str,
        now: float,
    ) -> tuple[Job, str] | None:
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        slot = connection.execute(
            f"SELECT worker_tag FROM {SLOTS_TABLE} WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None or str(row["state"]) != "running" or slot is None:
            return None
        job = _job(row, connection)
        connection.execute(
            """
            UPDATE jobs
            SET state = 'failed', finished_at = ?, last_activity_at = ?,
                failure_handled = 0
            WHERE id = ? AND state = 'running'
            """,
            (now, now, job_id),
        )
        detail = device_lost_detail(device, reason, slot["worker_tag"])
        self._record_job_event(connection, job, DEVICE_LOST, detail, now)
        orphan_slot(connection, job_id, now)
        back_off(
            connection, device, until=now + UNREACHABLE_BACKOFF_SECONDS,
            reason=f"{reason}; job {job_id} lost",
        )
        return job, detail

    def _t6(self, job: Job) -> None:
        # One dispatcher runs T6 at a time: orch poll holds the single-poller
        # flock (lib/cli.py _poll_lock), so these reads and writes do not race.
        if job.request.recipient == QA_RECIPIENT:
            self._qa_job_failed(job)
            return
        if job.request.action in ("stuck", "account-signin"):
            self.queue.mark_failure_handled(job.id)
            return
        if job.request.recipient != "grok" and self._finished_identity(
            job.request.repo, job.request.ticket_id
        ):
            # #108: a running job left alone at the merge fails into a
            # finished ticket: no retry, no stuck.
            self._record_job_event_once(
                job, "after-merge", "the ticket is finished; no T6 retry"
            )
            self.queue.mark_failure_handled(job.id)
            return
        if self._workflow_state(job.request.repo, job.request.ticket_id) == HANDED_OFF_EXTERNALLY:
            return
        if self._is_abandoned(job.request.repo, job.request.ticket_id):
            self._record_job_event_once(job, "abandoned", _ABANDONED_JOB_DETAIL)
            self.queue.mark_failure_handled(job.id)
            return
        if self._is_stuck_identity(job.request.repo, job.request.ticket_id):
            self._ensure_stuck_wake(job.request.repo, job.request.ticket_id)
            self.queue.mark_failure_handled(job.id)
            return
        # #91 (PR #88), #105, #193: the job may have posted its result and
        # then failed. That result stands: done, never retried.
        key = (job.request.repo.lower(), job.request.ticket_id)
        if self._holds_retry(job, self._posted_handoff(job, self._polled.get(key))):
            return
        if job.request.recipient != "grok" and self._waiting_max(job):
            self._record_job_event_once(
                job,
                "waiting-max",
                "the ticket waits on Max; his answers resume the paused step; no T6 retry",
            )
            self.queue.mark_failure_handled(job.id)
            return
        if job.request.recipient != "grok" and self._resumed_by_answers(job):
            self._record_job_event_once(
                job,
                "resumed-by-answers",
                "Max's answers already queued the paused step; no T6 retry",
            )
            self.queue.mark_failure_handled(job.id)
            return
        if self._holds_retry(job, self._authoritative_handoff(job)):
            return
        if self._restored_failure_waits(job):
            return
        if job.request.recipient != "grok" and self._stack_parent_closed(
            job.request.repo, job.request.ticket_id
        ):
            self._record_job_event_once(
                job,
                STACK_PARENT_CLOSED,
                "the stack parent closed unmerged; no T6 retry while the child waits for a person",
            )
            return
        polled = self._polled.get(key)
        if polled is not None and _base_mismatch(polled) is not None:
            self._record_job_event_once(
                job,
                "base-mismatch",
                "the PR targets another base; no T6 retry until it is retargeted",
            )
            return
        queue_owned = (
            job.request.action == "fix"
            and _merge_only(job.request.detail)
            and self._queue_owns_base_updates(job.request.repo, job.request.ticket_id)
        )
        if not queue_owned and self._lagging_handled(job):
            return
        if not queue_owned and self._usage_limit_handled(job):
            return
        if not queue_owned and self._signout_handled(job):
            return
        link_drop = self._link_drop(job)
        if link_drop is not None:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                release_link_dropped(connection, job.id)
                connection.commit()
            if job.request.action == "review" and self._review_retry_waits(job):
                return
            if (
                link_drop.used < MAX_LINK_DROP_REQUEUES
                and not queue_owned
                and job.request.action != "update-from-main"
            ):
                self._requeue_link_dropped(job, link_drop)
                return
        if job.request.action == "update-from-main":
            # #130: a failed update counts toward update-failed-2 in place of
            # the generic retry; the queue queues the next attempt.
            counted = self._update_failed(
                repo=job.request.repo,
                ticket_id=job.request.ticket_id,
                source_comment_id=job.request.source_comment_id,
                timeout_seconds=job.request.timeout_seconds,
                project_bot=_detail_project_bot(job.request.detail),
                back_to=_queue_state(polled.factory.ship if polled else "handoff"),
                job_id=job.id,
                why=f"job {job.id} failed",
            )
            self.queue.mark_failure_handled(job.id)
            if counted == "update-failed-1" and polled is not None:
                self._settle_failed_update_head(polled)
            return
        if queue_owned:
            self._record_job_event_once(
                job, "queue-owns-update", "the merge queue updates this head; no T6 retry"
            )
            self.queue.mark_failure_handled(job.id)
            return
        if job.request.attempt == 1:
            if job.request.action == "review" and self._review_retry_waits(job):
                return
            generation = job.request.generation + 1
            request = JobRequest(
                repo=job.request.repo,
                ticket_id=job.request.ticket_id,
                action=job.request.action,
                recipient=job.request.recipient,
                source_comment_id=job.request.source_comment_id,
                timeout_seconds=job.request.timeout_seconds,
                generation=generation,
                attempt=2,
                head=job.request.head,
                detail=_retry_detail(job),
            )
            self.queue.enqueue_retry(
                job.id,
                request,
                dedupe_key=(
                    f"t6-retry:{job.id}:g{generation}"
                ),
            )
            return
        self._mark_stuck_and_wake(
            repo=job.request.repo,
            ticket_id=job.request.ticket_id,
            source_comment_id=job.request.source_comment_id,
            timeout_seconds=job.request.timeout_seconds,
            reason="job-failed-2",
            detail=json.dumps(
                {
                    "project_bot": _detail_project_bot(job.request.detail),
                    "detail": f"{job.request.action} job failed twice"
                    + (f"; last failure {link_drop.spent}" if link_drop else ""),
                    "job_id": job.id,
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        self.queue.mark_failure_handled(job.id)

    def _lagging_handled(self, job: Job) -> bool:
        """#357: a start refused because a checkout lags requeues the same row
        after a backoff, and sticks the ticket device-lagging if it still lags
        past the wait. True when T6 handled the job."""
        now = self.queue.now()
        serving = self._serving()
        with self._connect() as connection:
            lagging = lagging_end(connection, job.id)
            paused = lane_limit.paused_through(connection, now, serving).get(job.request.recipient, 0.0)
        if lagging is None:
            return False
        if job.request.action == "review" and self._review_retry_waits(job):
            return True
        if (
            lagging.past_wait(now, since=paused)
            and job.request.recipient in self._lag_blocked_recipients()
        ):
            self._mark_stuck_and_wake(
                repo=job.request.repo,
                ticket_id=job.request.ticket_id,
                source_comment_id=job.request.source_comment_id,
                timeout_seconds=job.request.timeout_seconds,
                reason="device-lagging",
                detail=json.dumps(
                    {
                        "project_bot": _detail_project_bot(job.request.detail),
                        "detail": (
                            f"{job.request.action} job not started: device still lagging "
                            f"after {int(LAGGING_WAIT_SECONDS // 60)} min; last refusal "
                            f"{lagging.detail}"
                        ),
                        "job_id": job.id,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
            self.queue.mark_failure_handled(job.id)
            return True
        backoff = lagging.backoff()
        if now < (job.finished_at or now) + backoff:
            self._record_job_event_once(
                job, "requeue-deferred", f"preflight lagging; T6 requeues it after a {backoff} s backoff"
            )
            return True
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = requeue_lagging(
                connection, job.id, now=now, reason=f"preflight lagging: {lagging.detail}"
            )
            connection.commit()
        if result is not None:
            self._log_job_event(job, *result)
        return True

    def _usage_limit_handled(self, job: Job) -> bool:
        """#367: a run that hit its account's usage limit requeues the same
        row at once with nothing charged; placement skips that account's
        devices, and the lane gate in claim_order holds it while every
        account serving the lane is limited (#772). True when T6 handled the job."""
        serving = self._serving().get(job.request.recipient)
        with self._connect() as connection:
            detail = usage_limit_end(connection, job.id)
            limit = lane_limit.recipient_paused(
                connection, job.request.recipient, self.queue.now(), serving
            )
        if detail is None:
            return False
        if job.request.action == "review" and self._review_retry_waits(job):
            return True
        if limit is not None:
            self._record_job_event_once(job, "requeue-deferred", lane_limit.label(limit))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            release_link_dropped(connection, job.id)
            result = requeue_usage_limited(
                connection, job.id, now=self.queue.now(), reason=detail
            )
            connection.commit()
        if result is not None:
            self._log_job_event(job, *result)
        return True

    def _signout_handled(self, job: Job) -> bool:
        with self._connect() as connection:
            detail = signout_end(connection, job.id)
        if detail is None:
            return False
        if job.request.action == "review" and self._review_retry_waits(job):
            return True
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            release_link_dropped(connection, job.id)
            result = requeue_signed_out_run(
                connection, job.id, now=self.queue.now(), reason=detail
            )
            connection.commit()
        if result is not None:
            self._log_job_event(job, *result)
        return True

    def _link_drop(self, job: Job) -> _LinkDrop | None:
        """#218: the job's latest run ended in a link drop, unless it is a
        review whose head this poll read as replaced (T6 drops that retry)."""
        with self._connect() as connection:
            dropped = link_drop_end(connection, job.id)
            used = link_drop_requeues(connection, job.id)
        if dropped is None:
            return None
        ticket = self._polled.get((job.request.repo.lower(), job.request.ticket_id))
        if (
            job.request.action == "review"
            and ticket is not None
            and ticket.current_head is not None
            and _matching_head(job.request.head or "", ticket.current_head) is None
        ):
            return None
        link_drop = _LinkDrop(dropped[0], used)
        if used >= MAX_LINK_DROP_REQUEUES:
            self._record_job_event_once(job, "link-drop-budget-spent", link_drop.spent)
        return link_drop

    def _requeue_link_dropped(self, job: Job, link_drop: _LinkDrop) -> None:
        """Requeue the same row once its backoff from the failure has passed;
        until then the failure stays unhandled."""
        now = self.queue.now()
        backoff = LINK_DROP_BACKOFF_SECONDS[link_drop.used]
        if now < (job.finished_at or now) + backoff:
            self._record_job_event_once(
                job, "requeue-deferred", f"link drop; T6 requeues it after a {backoff} s backoff"
            )
            return
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = requeue_link_drop(
                connection, job.id, now=now, reason=f"link drop on {link_drop.detail}"
            )
            connection.commit()
        if result is not None:
            self._log_job_event(job, *result)

    def _restored_failure_waits(self, job: Job) -> bool:
        """#108: settle a failed job restore() marked against its thread
        before T6 retries it or calls the ticket stuck.

        The backup may predate the job's answer. Until a poll reads the
        ticket, T6 waits. A read that shows the job's step answered marks it
        done with that answer. True when T6 does no more with the job now.
        """
        with self._connect() as connection:
            if not restored_claim(connection, job.id):
                return False
        ticket = self._polled.get((job.request.repo.lower(), job.request.ticket_id))
        if ticket is None:
            self._record_job_event_once(
                job,
                "restore-deferred",
                "restored from a backup that may predate its answer; T6 waits "
                "for a poll to read the thread",
            )
            return True
        source = next(
            (item for item in ticket.comments if item.id == job.request.source_comment_id),
            None,
        )
        if source is None:
            return False
        answer = self._answered_by(
            ticket,
            source,
            job.request.action,
            job.request.recipient,
            queued_at=self._first_queued_at(job),
            head=job.request.head,
            round_number=_detail_round_number(job.request.detail),
            plan_round=plan_round(job.request.detail),
        )
        if answer is None:
            return False
        self._done_from_handoff(job, answer)
        return True

    def _handed_off_by_max(self, repo: str, ticket_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT to_state, source FROM ticket_transitions WHERE repo = ? AND ticket_id = ?"
                " ORDER BY id DESC LIMIT 1",
                (repo.lower(), ticket_id),
            ).fetchone()
        return row is not None and (row["to_state"], row["source"]) == ("handed-off", "answers-handoff")

    def _waiting_max(self, job: Job) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT workflow_state FROM ticket_state WHERE repo = ? AND ticket_id = ?",
                (job.request.repo.lower(), job.request.ticket_id),
            ).fetchone()
        return row is not None and row["workflow_state"] == "waiting-max"

    def _resumed_by_answers(self, job: Job) -> bool:
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT 1 FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND action = ?
                  AND dedupe_key LIKE 't5:%'
                  AND rowid > (SELECT rowid FROM jobs WHERE id = ?)
                """,
                (
                    job.request.repo.lower(),
                    job.request.ticket_id,
                    job.request.action,
                    job.id,
                ),
            ).fetchone() is not None

    def _holds_retry(self, job: Job, handoff: Comment | object | None) -> bool:
        if handoff is None:
            return False
        if handoff is _HANDOFF_UNREAD:
            self._record_job_event_once(
                job,
                "done-deferred",
                "a posted builder ready's head is not yet checked against the "
                "PR head; T6 waits for T2 to route it or call it stale",
            )
        elif handoff is _NO_READ_SINCE_FAILURE:
            self._record_job_event_once(
                job,
                "reconcile-deferred",
                "no authoritative ticket read since the job failed; T6 retries "
                "only after a read shows no result",
            )
        else:
            self._done_from_handoff(job, handoff)
        return True

    def _authoritative_handoff(self, job: Job) -> Comment | object | None:
        if self._read_started is None or job.request.action not in _SETTLING_KINDS:
            return None
        tracker = self._tracker(job.request.repo, job.request.ticket_id)
        if tracker is None:
            return None
        key = (job.request.repo.lower(), job.request.ticket_id)
        if self._fresh_reader is None:
            polled = self._polled.get(key)
            if polled is None or self._read_started < (job.finished_at or 0):
                return _NO_READ_SINCE_FAILURE
            return None
        try:
            try:
                fresh = self._observed(
                    self._fresh_reader(TicketRef(job.request.repo, job.request.ticket_id, tracker))
                )
            except BaseMismatch as mismatch:
                fresh = mismatch.ticket
            if fresh is None:
                return None
            fresh = self._routed(fresh)
        except Exception:
            return _NO_READ_SINCE_FAILURE
        self._polled[key] = fresh
        return self._posted_handoff(job, fresh)

    def _posted_handoff(self, job: Job, ticket: TrackedTicket | None) -> Comment | object | None:
        """The result a failed harness job already posted (#91, #105, #193).

        Read from `ticket`, posted after the comment that queued the job and
        no earlier than its first attempt was queued, from a login trusted for
        its `from` and `role`, of a kind in _SETTLING_KINDS for the job's
        action, and not already rejected when it was handled (stale,
        superseded, ...). A ready stale only because its review already
        answered (#71 stale-ready-verdict) still counts. An update job's ready
        is matched by TU's attribution, not by time.

        A review's is a verdict from the job's recipient for the job's head
        (as a prefix, like every head compare) and round, or its questions; a
        review job with no round or no head takes none. A builder's is from
        the job's recipient. Questions count only when T5's _asks_questions
        finds questions in them. A plan
        counts only for the round the job posts, the one T1P accepts. A build
        or fix job's ready is at a new head: one other than the job's own, or
        any head for a job that has none. That ready counts once T2 routed it,
        or while it waits on CI at the polled PR head. One T2 left unhandled
        at another head (its fresh PR read failed) may yet be stale:
        _HANDOFF_UNREAD when no counted handoff comes first.
        """
        candidate = self._handoff_candidate(job, ticket)
        if candidate is None:
            return None
        return _HANDOFF_UNREAD if candidate.unread else candidate.comment

    def _handoff_candidate(
        self, job: Job, ticket: TrackedTicket | None = None, *, skip_unread: bool = False
    ) -> HandoffCandidate | None:
        if ticket is None:
            ticket = self._polled.get((job.request.repo.lower(), job.request.ticket_id))
        if ticket is None:
            return None
        action = job.request.action
        kinds = _SETTLING_KINDS.get(action)
        if kinds is None:
            return None
        if action == "review":
            round_field = _detail_round(job.request.detail)
            if job.request.head is None or round_field is None:
                return None
            bot, role = f"{job.request.recipient}-bot", "reviewer"
        else:
            round_field = None
            bot, role = f"{job.request.recipient}-bot", "builder"
        update = action == "update-from-main"
        comments = sorted(ticket.comments, key=_comment_order)
        ids = [item.id for item in comments]
        after_source = job.request.source_comment_id in ids
        if after_source:
            comments = comments[ids.index(job.request.source_comment_id) + 1 :]
        queued_at = self._first_queued_at(job)
        for item in comments:
            if not update and (
                self._predates(ticket, item, queued_at)
                or (
                    not after_source
                    and self._handled_no_later_than(ticket, item.id, job.request.source_comment_id)
                )
            ):
                continue
            try:
                parsed = parse_comment(item.body)
            except HandoffError:
                continue
            fields = parsed.as_dict()
            kind = fields.get("kind")
            if kind not in kinds:
                continue
            if not self._trusted(ticket, item, fields, bot, role=role):
                continue
            outcome = self._handled_outcome(ticket, item.id)
            if outcome is None and item.edited_at is not None:
                continue
            if kind == "questions":
                if (
                    (update and not after_source)
                    or outcome == "no-questions"
                    or not _asks_questions(fields, parsed.body)
                    or (outcome is not None and outcome.startswith(_REJECTED_OUTCOMES))
                ):
                    continue
                return HandoffCandidate(item, False)
            head = fields.get("head") or ""
            if kind == "verdict":
                if _matching_head(head, job.request.head) is None:
                    continue
                if fields.get("round") != round_field:
                    continue
            elif kind == "plan":
                reported = _round_number(fields.get("round"))
                if reported != plan_round(job.request.detail) or not self._t1p_accepted_or_accepts_plan(
                    ticket, item, reported
                ):
                    continue
            elif job.request.head is not None and _matching_head(head, job.request.head):
                continue
            if (
                outcome is not None
                and outcome != "stale-ready-verdict"
                and outcome.startswith(_REJECTED_OUTCOMES)
            ):
                continue
            if update and (
                (outcome is not None and outcome.startswith(_UPDATE_NO_HANDOFF_OUTCOMES))
                or self._update_job_before(ticket, item) != job.id
            ):
                continue
            if kind == "ready" and self._head_checks_unmet(ticket, item.id):
                continue
            unread = (
                kind == "ready"
                and outcome is None
                and _matching_head(head, ticket.current_head) is None
            )
            if unread and skip_unread:
                continue
            return HandoffCandidate(item, unread)
        return None

    def _head_checks_unmet(self, ticket: TrackedTicket, comment_id: str) -> bool:
        """A ready settles no job (#503, #1463) once any of its head checks
        failed, or while a check factory.md plans for this PR never passed.
        An unavailable local_gate counts as met here: T2 holds that ready as
        local-gate-waiting (#1464). Keyed on the comment, not the job, so a
        T6 retry cannot settle with it."""
        prefix = f"handoff {comment_id}:"
        with self._connect() as connection:
            events = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT DISTINCT event FROM job_events
                    WHERE repo = ? AND ticket_id = ? AND event IN (?, ?, ?, ?, ?)
                      AND substr(detail, 1, ?) = ?
                    """,
                    (
                        ticket.repo.lower(), ticket.ticket_id, *CHECK_FAILED_EVENTS,
                        *CHECK_PASSED_EVENTS, LOCAL_GATE_UNAVAILABLE, len(prefix), prefix,
                    ),
                )
            }
        if events.intersection(CHECK_FAILED_EVENTS):
            return True
        return any(
            check.passed_event not in events and check.unavailable_event not in events
            for check in planned(ticket.factory, ticket.pr_base)
        )

    def _done_from_handoff(self, job: Job, handoff: Comment) -> None:
        """Mark a failed job done with the handoff it already posted."""
        kind = parse_comment(handoff.body).as_dict()["kind"]
        event = f"done-from-{kind}"
        now = self.queue.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE jobs SET state = 'done', handoff_comment_id = ?, failure_handled = 1
                WHERE id = ? AND state = 'failed' AND failure_handled = 0
                """,
                (handoff.id, job.id),
            ).rowcount
            if changed != 1:
                connection.commit()
                return
            where = "for" if kind == "verdict" else "after"
            detail = (
                f"{kind} comment {handoff.id} {where} head {(job.request.head or 'none')[:12]} "
                "was already posted; marked done with it, no T6 retry"
            )
            self._record_job_event(connection, job, event, detail, now)
            connection.commit()
        self._log_job_event(job, event, detail)

    def _open_fix_for_head(self, job: Job) -> bool:
        """A fix job on the same ticket for this job's head that is still open.

        Open is queued, running, or failed with its T6 handling still to
        come (PR #99 r3): T6 handles failures oldest first, so a review that
        failed before its fix would otherwise be retried in the same pass
        that queues the fix's retry, and both would run on the same head.
        """
        if job.request.head is None:
            return False
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT head FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND action = 'fix'
                  AND head IS NOT NULL
                  AND (state IN ('queued', 'running')
                       OR (state = 'failed' AND failure_handled = 0))
                """,
                (job.request.repo.lower(), job.request.ticket_id),
            ).fetchall()
        return any(
            _matching_head(str(row["head"]), job.request.head) is not None for row in rows
        )

    def _review_retry_waits(self, job: Job) -> bool:
        """#91: hold a failed review's T6 retry while a fix for its head is open.

        Open includes a failed fix awaiting its own T6 retry. The retry is
        deferred, not dropped: the failure stays unhandled and is looked at
        again every poll. Once no fix is open, the retry goes ahead if the
        ticket still has this head (a fix that changed nothing and readied
        the same head needs it: that ready's review dedupes onto this failed
        job), and is dropped for good once the head was replaced.
        #71: a head this poll read as replaced is dropped at once, fix or not.
        True when the retry is not queued now.
        """
        ticket = self._polled.get((job.request.repo.lower(), job.request.ticket_id))
        if (
            ticket is not None
            and ticket.current_head is not None
            and _matching_head(job.request.head or "", ticket.current_head) is None
        ):
            self._skip_review_retry(job, ticket.current_head)
            return True
        if self._open_fix_for_head(job):
            self._record_job_event_once(
                job,
                "retry-deferred",
                f"a fix job for head {(job.request.head or '')[:12]} is open; "
                "T6 retry review waits for it",
            )
            return True
        if not self._job_event_recorded(job, "retry-deferred"):
            return False
        if ticket is None:
            return True  # the head is not known this poll; look again next poll
        if _matching_head(job.request.head or "", ticket.current_head) is not None:
            return False
        self._skip_review_retry(job, ticket.current_head)
        return True

    def _job_event_recorded(self, job: Job, event: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM job_events WHERE job_id = ? AND event = ? LIMIT 1",
                (job.id, event),
            ).fetchone()
        return row is not None

    def _record_job_event_once(self, job: Job, event: str, detail: str) -> None:
        now = self.queue.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            seen = connection.execute(
                "SELECT 1 FROM job_events WHERE job_id = ? AND event = ? LIMIT 1",
                (job.id, event),
            ).fetchone()
            if seen is None:
                self._record_job_event(connection, job, event, detail, now)
            connection.commit()
        if seen is None:
            self._log_job_event(job, event, detail)

    def _skip_review_retry(self, job: Job, current_head: str | None) -> None:
        now = self.queue.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE jobs SET failure_handled = 1
                WHERE id = ? AND state = 'failed' AND failure_handled = 0
                """,
                (job.id,),
            ).rowcount
            if changed != 1:
                connection.commit()
                return
            detail = (
                f"head {(job.request.head or '')[:12]} was replaced by "
                f"{(current_head or 'none')[:12]}; no T6 retry review"
            )
            self._record_job_event(connection, job, "retry-skipped", detail, now)
            connection.commit()
        self._log_job_event(job, "retry-skipped", detail)

    def _reject_route(self, ticket: TrackedTicket, error: RouteError) -> None:
        """A restored same-family pair (#206): the ticket goes stuck and no
        job queued under that pair can be claimed."""
        key = (ticket.repo.lower(), ticket.ticket_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._set_stuck_in_transaction(
                connection,
                repo=ticket.repo,
                ticket_id=ticket.ticket_id,
                source_comment_id=error.source_comment_id,
                timeout_seconds=ticket.job_timeout_minutes * 60,
                reason="route-same-family",
            )
            for job in connection.execute(
                """
                SELECT id FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND state = 'queued'
                  AND recipient != 'grok'
                """,
                key,
            ).fetchall():
                self.queue.cancel(
                    str(job["id"]), "route-same-family", connection=connection
                )
            self._ensure_stuck_wake_in_transaction(
                connection,
                repo=ticket.repo,
                ticket_id=ticket.ticket_id,
                detail=self._project_wake_detail(ticket, str(error)),
            )
            connection.commit()

    def _mark_stuck_and_wake(
        self,
        *,
        repo: str,
        ticket_id: str,
        source_comment_id: str,
        timeout_seconds: int,
        reason: str,
        detail: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._set_stuck_in_transaction(
                connection,
                repo=repo,
                ticket_id=ticket_id,
                source_comment_id=source_comment_id,
                timeout_seconds=timeout_seconds,
                reason=reason,
            )
            self._ensure_stuck_wake_in_transaction(
                connection,
                repo=repo,
                ticket_id=ticket_id,
                detail=detail,
            )
            connection.commit()

    def _set_stuck_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        repo: str,
        ticket_id: str,
        source_comment_id: str,
        timeout_seconds: int,
        reason: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO ticket_state (
                repo, ticket_id, gate_count, stuck, stuck_reason,
                stuck_source_comment_id, timeout_seconds, generation
            ) VALUES (?, ?, 0, 1, ?, ?, ?, 1)
            ON CONFLICT(repo, ticket_id)
            DO UPDATE SET
                stuck = 1,
                stuck_reason = excluded.stuck_reason,
                stuck_source_comment_id = excluded.stuck_source_comment_id,
                timeout_seconds = excluded.timeout_seconds
            WHERE ticket_state.workflow_state IS NULL
               OR ticket_state.workflow_state NOT IN ('merged', 'done')
            """,
            (
                repo.lower(),
                ticket_id,
                reason,
                source_comment_id,
                timeout_seconds,
            ),
        )

    def _ensure_all_stuck_wakes(self) -> None:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT repo, ticket_id FROM ticket_state
                WHERE stuck = 1
                """
            ).fetchall()
        for row in rows:
            repo, ticket_id = str(row["repo"]), str(row["ticket_id"])
            try:
                self._ensure_stuck_wake(repo, ticket_id)
            except Exception as exc:
                self.record_poll_exception(repo, ticket_id, exc, prefix="stuck-wake:")

    def _ensure_stuck_wake(self, repo: str, ticket_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_stuck_wake_in_transaction(
                connection,
                repo=repo,
                ticket_id=ticket_id,
                detail=None,
            )
            connection.commit()

    def _with_retry_note(
        self,
        connection: sqlite3.Connection,
        repo: str,
        ticket_id: str,
        detail: str,
    ) -> str:
        retries = connection.execute(
            """
            SELECT comment_id FROM ticket_retries
            WHERE repo = ? AND ticket_id = ? AND outcome NOT LIKE 'rejected-%'
            ORDER BY recorded_at, rowid
            """,
            (repo.lower(), ticket_id),
        ).fetchall()
        if not retries:
            return detail
        return _append_detail(
            detail,
            f"after retry {len(retries)} (comment:{retries[-1]['comment_id']})",
        )

    def _ensure_stuck_wake_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        repo: str,
        ticket_id: str,
        detail: str | None,
    ) -> None:
        state = connection.execute(
            """
            SELECT stuck, stuck_reason, stuck_source_comment_id,
                   timeout_seconds, generation, stuck_wake_job_id, stuck_since,
                   workflow_state
            FROM ticket_state
            WHERE repo = ? AND ticket_id = ?
            """,
            (repo.lower(), ticket_id),
        ).fetchone()
        if (
            state is None
            or not bool(state["stuck"])
            or state["workflow_state"] == "abandoned"
        ):
            return
        wake_job_id = state["stuck_wake_job_id"]
        previous_detail: str | None = None
        stuck_since: float | None = None
        if wake_job_id is not None:
            wake = connection.execute(
                "SELECT state, detail, created_at FROM jobs WHERE id = ?",
                (wake_job_id,),
            ).fetchone()
            if wake is not None and str(wake["state"]) != "failed":
                return
            stuck_since = state["stuck_since"]
            if stuck_since is None and wake is not None:
                stuck_since = wake["created_at"]
            if wake is not None and isinstance(wake["detail"], str):
                previous_detail = job_detail.load_row(connection, str(wake_job_id), wake["detail"])
            failed = connection.execute(
                """
                SELECT count(*) FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND action = 'stuck'
                  AND source_comment_id = ? AND state = 'failed'
                """,
                (repo.lower(), ticket_id, state["stuck_source_comment_id"]),
            ).fetchone()[0]
            if failed >= STUCK_WAKE_FAILURE_LIMIT:
                self._flag_attention_once(
                    connection,
                    repo,
                    ticket_id,
                    "stuck-wake-failing",
                    f"{failed} stuck wakes to Grok failed; no further wake is queued",
                )
                return
        generation = int(state["generation"]) + 1
        source_comment_id = str(state["stuck_source_comment_id"] or "-")
        if detail is None and previous_detail is not None:
            reason = previous_detail
        else:
            reason = self._with_retry_note(
                connection,
                repo,
                ticket_id,
                detail or str(state["stuck_reason"] or "ticket is stuck"),
            )
        reason = self._linked_wake_detail(repo, ticket_id, source_comment_id, reason)
        queued = self.queue.enqueue_in_transaction(
            connection,
            JobRequest(
                repo=repo,
                ticket_id=ticket_id,
                action="stuck",
                recipient="grok",
                source_comment_id=source_comment_id,
                timeout_seconds=int(
                    state["timeout_seconds"] or DEFAULT_JOB_TIMEOUT_MINUTES * 60
                ),
                generation=generation,
                detail=reason,
            ),
            dedupe_key=f"stuck:{_scope(repo, ticket_id)}:g{generation}",
        )
        connection.execute(
            """
            UPDATE ticket_state
            SET generation = ?, stuck_wake_job_id = ?, stuck_since = ?
            WHERE repo = ? AND ticket_id = ?
            """,
            (
                generation,
                queued.id,
                queued.created_at if stuck_since is None else stuck_since,
                repo.lower(),
                ticket_id,
            ),
        )
