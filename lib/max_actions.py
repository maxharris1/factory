"""What Max can do from the dashboard for a ticket that waits on him (#822).

Each ticket that waits on Max or is paused gets a `wait`: why it waits
(`Why`) and the comment that explains it. `ACTIONS` names, for every
action, the reasons it applies to. `applicable` reads that table, and the
snapshot writes its answer onto the ticket row as `actions`, so the
Activity page and deploy/factory-dashboard-http.py read one list.

The server builds each action's comment body itself; it is one standard
library file and cannot import this module, so tests/test_max_actions.py
checks that its table matches this one.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from lib.dispatch.constants import HANDED_OFF_EXTERNALLY, NOT_STARTED, TERMINAL_STATES

Why = Literal[
    "awaiting-ok",
    "visual-qa-review",
    "plan-size",
    "question",
    "repeat-stop",
    "unsat",
    "capped",
    "blocked",
    "stuck",
    "waiting-external",
]
WHYS: tuple[Why, ...] = (
    "awaiting-ok",
    "visual-qa-review",
    "plan-size",
    "question",
    "repeat-stop",
    "unsat",
    "capped",
    "blocked",
    "stuck",
    "waiting-external",
)

ActionName = Literal[
    "ok", "request-changes", "rerun-qa", "approve-plan", "split", "another-round", "answer", "retry", "resume", "close",
    "hand-off",
]


@dataclass(frozen=True)
class ActionSpec:
    label: str
    applies: frozenset[Why]
    head_bound: bool = False


ACTIONS: dict[ActionName, ActionSpec] = {
    "ok": ActionSpec("Merge OK", frozenset({"awaiting-ok", "visual-qa-review"}), head_bound=True),
    "request-changes": ActionSpec("Request changes", frozenset({"visual-qa-review"}), head_bound=True),
    "rerun-qa": ActionSpec("Re-run QA", frozenset({"visual-qa-review"}), head_bound=True),
    "approve-plan": ActionSpec("Approve plan as-is", frozenset({"plan-size"})),
    "split": ActionSpec("Split", frozenset({"plan-size"})),
    "another-round": ActionSpec("Another round", frozenset({"capped", "repeat-stop", "unsat"})),
    "answer": ActionSpec("Answer", frozenset({"question"})),
    "retry": ActionSpec("Requeue", frozenset({"stuck"})),
    "resume": ActionSpec("Resume", frozenset({"blocked"})),
    "close": ActionSpec("Close", frozenset(WHYS)),
    "hand-off": ActionSpec("Hand off", frozenset(WHYS)),
}

_PARKED: dict[str, Why] = {
    "waiting-max-size": "plan-size",
    "waiting-max-repeat": "repeat-stop",
    "waiting-max-unsat": "unsat",
}


@dataclass(frozen=True)
class Wait:
    why: Why
    comment_id: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict:
        return {"why": self.why, "comment_id": self.comment_id, "detail": self.detail}


HAND_OFF_FROM = ("waiting-max", "awaiting-ok", "capped", "blocked", "waiting-external")
FIRST_QUESTIONS_PAUSE = (None, "clarify")


def past_go(state: str | None, paused_action: str | None) -> bool:
    if state in NOT_STARTED:
        return False
    return not (state == "waiting-max" and paused_action in FIRST_QUESTIONS_PAUSE)


def may_hand_off(
    state: str | None, stuck: bool, paused_action: str | None, job_in_flight: bool
) -> bool:
    if job_in_flight or not past_go(state, paused_action):
        return False
    if state in HAND_OFF_FROM:
        return True
    return stuck and state not in TERMINAL_STATES and state not in ("handed-off", HANDED_OFF_EXTERNALLY)


def applicable(wait: Wait | None, *, hand_off: bool) -> list[ActionName]:
    if wait is None:
        return []
    return [
        name for name, spec in ACTIONS.items()
        if wait.why in spec.applies and (hand_off or name != "hand-off")
    ]


def wait_of(
    state: str,
    stuck: bool,
    *,
    stuck_reason: str | None = None,
    stuck_comment_id: str | None = None,
    question_id: str | None = None,
    parked_outcome: str | None = None,
    approve_comment_id: str | None = None,
    hold_reasons: str | None = None,
    stop_comment_id: str | None = None,
) -> Wait | None:
    """Why a ticket in `state` waits on Max, or None when it does not.
    `parked_outcome` is the plan or review outcome recorded for the comment
    that parked it at waiting-max."""
    if stuck:
        return Wait("stuck", stuck_comment_id, stuck_reason)
    if state == "waiting-max":
        return Wait(_PARKED.get(parked_outcome or "", "question"), question_id)
    if state == "awaiting-ok":
        reasons = {part.strip() for part in (hold_reasons or "").split(",") if part.strip()}
        if "visual-qa-review" in reasons:
            return Wait("visual-qa-review", approve_comment_id, "visual-qa-review")
        return Wait("awaiting-ok", approve_comment_id)
    if state in ("capped", "blocked"):
        return Wait(state, stop_comment_id or question_id)
    if state == "waiting-external":
        return Wait(state, question_id)
    return None


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def wait_facts(connection: sqlite3.Connection) -> dict[tuple[str, str], dict[str, str | None]]:
    """(repo casefolded, ticket id) -> what `wait_of` reads from orch.db."""
    tables = _tables(connection)
    if "ticket_state" not in tables:
        return {}
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(ticket_state)")}

    def column(name: str) -> str:
        return f"state.{name}" if name in columns else "NULL"

    parked = " UNION ALL ".join(
        f"SELECT repo, ticket_id, comment_id, outcome FROM {table}"
        for table in ("plan_events", "review_events")
        if table in tables
    )
    holds = "ship_holds" in tables
    hold_columns = (
        {str(row[1]) for row in connection.execute("PRAGMA table_info(ship_holds)")}
        if holds
        else set()
    )
    hold_reasons_col = "hold.reasons" if "reasons" in hold_columns else "NULL"
    stop = (
        f"""(SELECT stop.comment_id FROM review_events AS stop
             WHERE stop.repo = state.repo AND stop.ticket_id = state.ticket_id
               AND stop.outcome = CASE state.workflow_state
                   WHEN 'capped' THEN 'capped-review' WHEN 'blocked' THEN 'blocked-review' END
             ORDER BY stop.rowid DESC LIMIT 1)"""
        if "review_events" in tables
        else "NULL"
    )
    running = (
        """(SELECT 1 FROM jobs WHERE lower(jobs.repo) = state.repo
             AND jobs.ticket_id = state.ticket_id AND jobs.state = 'running' LIMIT 1)"""
        if "jobs" in tables
        else "NULL"
    )
    facts: dict[tuple[str, str], dict[str, str | None]] = {}
    for row in connection.execute(
        f"""
        SELECT state.repo, state.ticket_id,
               {column("paused_question_id")} AS question_id,
               {column("stuck_source_comment_id")} AS stuck_comment_id,
               {"parked.outcome" if parked else "NULL"} AS parked,
               {"hold.approve_comment_id" if holds else "NULL"} AS approve_comment_id,
               {hold_reasons_col} AS hold_reasons,
               {stop} AS stop_comment_id,
               {running} AS running
        FROM ticket_state AS state
        {f'''LEFT JOIN ({parked}) AS parked
          ON parked.repo = state.repo AND parked.ticket_id = state.ticket_id
         AND parked.comment_id = {column("paused_question_id")}
         AND parked.outcome IN ({", ".join(repr(key) for key in _PARKED)})'''
         if parked else ""}
        {'''LEFT JOIN ship_holds AS hold
          ON hold.repo = state.repo AND hold.ticket_id = state.ticket_id'''
         if holds else ""}
        """
    ):
        facts[(str(row[0]).casefold(), str(row[1]))] = {
            key: None if value is None else str(value)
            for key, value in zip(
                ("question_id", "stuck_comment_id", "parked", "approve_comment_id",
                 "hold_reasons", "stop_comment_id", "running"),
                tuple(row)[2:],
            )
        }
    return facts
