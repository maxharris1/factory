"""Dispatcher transitions T1, T1P and T2 through T6, plus project hooks.

The code lives in lib/dispatch/ (one module per area; see lib/dispatch/README.md).
This module re-exports every name it has always exported and composes Dispatcher
from the per-area mixins.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
import traceback
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import islice
from pathlib import Path
from typing import Protocol

from lib import attention, project_rules
from lib.device_pool import free_slots, used_slots
from lib.dispatch.caps import (
    CapsMixin,
)
from lib.dispatch.comments import (
    CommentsMixin,
)
from lib.dispatch.constants import (
    BASE_UPDATE_GRACE_SECONDS,
    CORE_KINDS,
    FINISHED_STATES,
    GITHUB_STATE_LABEL_PREFIX,
    GROK_WAKE_REASONS,
    LINEAR_HANDOFF_QUERY,
    MERGED_BEFORE_HANDOFF,
    OFF_LOOP_VERIFY_READS,
    ORCHESTRATOR,
    POLL_ERROR_DETAIL_LIMIT,
    POLL_ERROR_FRAMES,
    POLL_ERROR_LIMIT,
    POLL_ERROR_STREAK,
    SETTLE_FAILURES_TO_WAKE,
    SHIP_ERROR_LIMIT,
    STOPPED_POLL_SECONDS,
    STOPPED_STATES,
    STUCK_WAKE_FAILURE_LIMIT,
    UPDATE_FAILURES_TO_STOP,
    UPDATE_READY_GRACE_SECONDS,
    WATCHED_STATES,
    WORKER_KINDS,
    _ABANDONED_JOB_DETAIL,
    _FENCE_OPEN,
    _LIST_MARKER,
    _NOT_FINISHED,
    _NO_QUESTIONS,
    _QUESTIONS_HEADING,
    _UNVERIFIED_HANDOFF,
)
from lib.dispatch.core import (
    DispatcherBase,
)
from lib.dispatch.detail import (
    DetailMixin,
)
from lib.dispatch.inbox import InboxMixin
from lib.dispatch.eval_log import (
    EvalLogMixin,
)
from lib.dispatch.followups import (
    FollowupsMixin,
)
from lib.dispatch.errors_text import (
    _CREDENTIAL_WORD,
    _PLAIN_MESSAGE,
    _PLAIN_REDACTED,
    _POLL_ERROR_CAUSE,
    _POLL_ERROR_CONTEXT,
    _REDACTED_MARKS,
    _SECRET_ASSIGNMENT,
    _SECRET_HOST_PATH,
    _SECRET_NAME,
    _SECRET_TOKEN,
    _SECRET_URL,
    _cap,
    _named,
    _plain_prefix,
    _redact,
    _redact_spans,
    _without_body,
    poll_error_detail,
    poll_error_text,
    safe_ship_error,
    scrub_detail,
)
from lib.dispatch.helpers import (
    ACTED_VERDICT_OUTCOMES,
    _ANSWERS,
    _HANDOFF_UNREAD,
    _REJECTED_OUTCOMES,
    _acted_verdict,
    _collapse_fences,
    _comment_link,
    _comment_order,
    _detail_project_bot,
    _detail_round_number,
    _detail_routing,
    _family_errors,
    _grok_logical_key,
    _has_questions,
    _holds,
    _is_full_sha,
    _is_patch_id,
    _matching_head,
    _normalize_trusted_logins,
    _question_item,
    _queue_state,
    _redrive_eligible,
    _retry_detail,
    _round_number,
    _route,
    _route_go,
    _scope,
    _section_asks,
    _ship_state,
    _spread,
    _state_for_action,
    ship_mode,
    trusted_bots,
)
from lib.dispatch.holds import (
    HoldsMixin,
)
from lib.dispatch.jobs import (
    JobsMixin,
)
from lib.dispatch.merge_queue import (
    MergeQueueMixin,
)
from lib.dispatch.plan import (
    PlanMixin,
)
from lib.dispatch.poll import (
    PollLoop,
    PollMixin,
    _after_poll,
)
from lib.dispatch.qa import QaMixin
from lib.dispatch.questions import (
    QuestionsMixin,
)
from lib.dispatch.ready import (
    ReadyMixin,
)
from lib.dispatch.rebase import RebaseMixin
from lib.dispatch.retry import (
    RetryMixin,
)
from lib.dispatch.schema import (
    SchemaMixin,
    _add_missing_columns,
)
from lib.dispatch.ship import (
    ReviewShipper,
    ShipMixin,
)
from lib.dispatch.linear_states import LinearStatesMixin
from lib.dispatch.tracking import (
    TrackingMixin,
)
from lib.dispatch.transitions import write_workflow_state
from lib.dispatch.types import (
    LINEAR_STATE_PHASES,
    AlreadyMergedShip,
    CIResult,
    Comment,
    DeviceChecks,
    FailedCheck,
    Feeder,
    GrokDelivery,
    Hook,
    PollCycleFailed,
    PollSource,
    RetryableShipError,
    RetryableStateError,
    ReviewActions,
    RouteError,
    ShipError,
    ShipRequest,
    TicketRef,
    TrackedTicket,
    UpdateClassifier,
)
from lib.dispatch.verdict import (
    ReviseCIGate,
    VerdictMixin,
)
from lib.factory_md import FactoryConfig, family_findings
from lib.grok_delivery import GrokDeliveryError
from lib.handoff import HandoffError, parse_comment
from lib.hold import HoldDecision, LabelEvent, decide
from lib.job_state import (
    DEAD,
    RELEASED_UNSTARTED,
    RUN_DIED,
    STALE,
    JobStateConfig,
    Liveness,
    claimed_too_long,
    derive,
    last_run_end,
    read_placements,
    requeue_unstarted,
    unstarted_too_long,
)
from lib.orch_yml import DEFAULT_JOB_TIMEOUT_MINUTES, POLL_SECONDS, Approvals
from lib.pr_title import SOURCES_TABLE, TITLE_LIMIT, prefixed
from lib.queue import (
    SLOTS_TABLE,
    Job,
    JobRequest,
    QueueError,
    SQLiteJobQueue,
    _job,
    restored_claim,
    retire_requeued,
)
from lib.schema import HARNESSES
from lib.state_backup import RESTORED
from lib.update_check import UpdateClass


class Dispatcher(
    CommentsMixin,
    ReadyMixin,
    QaMixin,
    VerdictMixin,
    CapsMixin,
    MergeQueueMixin,
    HoldsMixin,
    ShipMixin,
    QuestionsMixin,
    PlanMixin,
    RetryMixin,
    RebaseMixin,
    JobsMixin,
    DetailMixin,
    InboxMixin,
    FollowupsMixin,
    TrackingMixin,
    LinearStatesMixin,
    PollMixin,
    EvalLogMixin,
    SchemaMixin,
    DispatcherBase,
):
    """Composed from the lib/dispatch mixins; it defines no methods of its own."""
