"""Dispatcher constants shared across lib/dispatch modules."""

from __future__ import annotations

import re
from typing import Final, Literal


GITHUB_STATE_LABEL_PREFIX = "orch:state/"
# merged and done are absorbing (#108): no later write moves a ticket out.
FINISHED_STATES = ("merged", "done")
TERMINAL_STATES = frozenset({"merged", "done", "refused", "abandoned"})
CI_INFRA_WAIT = "ci-infra-wait"
CI_RERUN: Final = "ci-rerun"
CI_BASE_RED: Final = "ci-base-red"
CIHold = Literal["ci-rerun", "ci-base-red"]
CI_WAIT_REASONS = ("ci-pending", "approved-ci-pending", CI_RERUN, CI_INFRA_WAIT, CI_BASE_RED)
CI_RERUN_SETTLE_SECONDS = 900.0
CI_INFRA_WAIT_SECONDS = 1800.0
CI_STALE_SECONDS = 7200.0
CI_STALE_REASON = "ci-stale"
CI_INFRA_BLOCKED_REASON = "ci-infra-blocked"
# #615: how long a ready on a CI base waits with no check run and no commit
# status on its head before the ticket needs attention as ci-silent.
CI_SILENT_SECONDS = 600.0
CI_SILENT_REASON = "ci-silent"
UNANNOUNCED_AFTER_CAP = "unannounced-head-after-cap"
CI_RERUN_REFUSED = "ci-rerun-refused"
ORCHESTRATOR = "grok-bot"
# #170: the kinds a worker bot's App login may post once the switch is on.
WORKER_KINDS = frozenset({"plan", "questions", "ready", "verdict", "stuck"})
_NOT_FINISHED = "COALESCE(workflow_state, '') NOT IN ('merged', 'done')"
# #108 (#42 N4): states where no bot is working and no person is waiting on
# the factory. Their tickets are read once every STOPPED_POLL_SECONDS.
STOPPED_STATES = ("handed-off", "capped", "blocked", "refused")
HANDED_OFF_EXTERNALLY = "handed-off-externally"
NOT_STARTED = (None, "intake", "refused", "questions")
SETTLE_CLEARS = {
    "stuck": 0,
    "stuck_reason": None,
    "stuck_source_comment_id": None,
    "stuck_wake_job_id": None,
    "paused_action": None,
    "paused_recipient": None,
    "paused_head": None,
    "paused_generation": None,
    "paused_question_id": None,
}
# #130: an approved ticket on a finish_line: merge project stays watched,
# polled every cycle, until its PR merges or closes.
WATCHED_STATES = ("merge-queue", "updating")
UPDATE_FAILURES_TO_STOP = 2
UPDATE_READY_GRACE_SECONDS = 600.0
# #255: how long a ready on a conflicting PR waits for any check run on its
# head before the dispatcher queues a merge-only base update.
BASE_UPDATE_GRACE_SECONDS = 120.0
STOPPED_POLL_SECONDS = 300
MERGED_BEFORE_HANDOFF = "merged before handoff"
LINEAR_HANDOFF_QUERY = "orch:v1 kind=brief OR orch:v1 kind=go"
REVISE_ROUND_STATES = ("review", "awaiting-ok")
APPROVE_ROUND_STATES = (*REVISE_ROUND_STATES, "fixing", "merge-queue", "waiting-external")
GROK_WAKE_REASONS = (
    "blocked",
    "capped",
    "refused",
    "stuck",
    "unparsed",
    "ship-failed",
    "rejected",
    "stale",
    "done",
    "awaiting-ok",
    "go-not-from-approver",
    "qa-failed",
    "qa-env-failed",
    "qa-skill-drift",
    "ci-rerun-refused",
    CI_STALE_REASON,
)
GROK_WAKE_SKIP = frozenset({"done", "refused", "rejected", "unparsed", "stale"})
CORE_KINDS = ("go", "plan", "ready", "retry")
RETRY_REJECTION_NOTES = {
    "rejected-retry-author": (
        "a retry must come from grok-bot as orchestrator"
        " (with the GitHub App switch on, from an approver)"
    ),
    "rejected-retry-no-reason": "a retry needs Max's reason in the comment body",
    "rejected-retry-not-stuck": "the ticket is not stuck; nothing to retry",
    "rejected-retry-job-active": (
        "a job is already queued or running for the ticket; nothing to retry"
    ),
}
APPROVE_REJECTION_NOTES = {
    "rejected-approve-no-ac": (
        "verdict=APPROVE needs ac=met; the review is given back so the reviewer"
        " can post ac=met or verdict=REVISE with ac=unmet"
    ),
    "rejected-approve-ac-unmet": (
        "verdict=APPROVE with ac=unmet; the review is given back so the reviewer"
        " can post verdict=REVISE with ac=unmet"
    ),
    "rejected-approve-open-blocking": (
        "verdict=APPROVE lists an open [blocking] item; the review is given back"
        " so the reviewer can post verdict=REVISE or close the item"
    ),
}
SETTLE_FAILURES_TO_WAKE = 3
STUCK_WAKE_FAILURE_LIMIT = 2
_QUESTIONS_HEADING = re.compile(
    r"(?:#{1,6}\s*)?(?:\*\*)?questions(?::\*\*|\*\*:?|:)?(?:\s+(?P<rest>.*))?",
    re.IGNORECASE,
)
_LIST_MARKER = re.compile(r"^(?:[-*+]|\d+[.)])\s+")
_FENCE_OPEN = re.compile(r" {0,3}(?P<fence>`{3,}(?=[^`]*$)|~{3,})")
_NO_QUESTIONS = frozenset({"none", "n/a", "no questions"})


SHIP_ERROR_LIMIT = 240
POLL_ERROR_LIMIT = 500
POLL_ERROR_STREAK = 3
POLL_ERROR_RETENTION_SECONDS = 7 * 86400
HARNESS_WORK = "recipient NOT IN ('grok', 'qa')"
_UNVERIFIED_HANDOFF = f"""
    jobs.state = 'done' AND jobs.{HARNESS_WORK}
    AND jobs.handoff_comment_id IS NOT NULL
    AND jobs.finished_at IS NOT NULL AND jobs.finished_at < ?
    AND NOT EXISTS (
        SELECT 1 FROM job_events AS event
        WHERE event.job_id = jobs.id
          AND event.event IN ('handoff-verified', 'handoff-unverified')
    )
"""
_ABANDONED_JOB_DETAIL = "the ticket is abandoned; nothing more starts for it"
# #109: tickets out of the poll loop read per poll to verify a late handoff.
OFF_LOOP_VERIFY_READS = 5


POLL_ERROR_FRAMES = 10
POLL_ERROR_DETAIL_LIMIT = 4000
