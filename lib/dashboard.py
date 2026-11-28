"""Read-only, redacted dashboard snapshot (status.json, the pages and tickets.json).

`orch dashboard snapshot` opens the dispatcher database with a SQLite
`mode=ro` URI and reads the tail of poller.log. It never writes either.
It writes status.json, the self-contained pages (PAGES: index.html,
job.html, activity.html, efficiency.html, evals.html, health.html, settings.html, ticket.html; see
lib/dashboard_page.py) and tickets.json to an output directory, each
atomically (temp file in the same directory, fsync, then rename), as one
set: a failed rename puts back every file already renamed. Each page's inline script fetches status.json every 1 to 2
seconds and updates in place. The Evals table is not in status.json; the
page fetches evals.json for each new status.json, again on later polls
until its generated_at_epoch catches up, and redraws it from any bundle
newer than the table on the page. A malformed bundle (a missing or unknown
field, a value of a type or form collect_weekly never writes, more than KEEP
weeks or weeks out of order, or a state that disagrees with its weeks or
skipped count) leaves the table as it is.
Without script (or when status.json cannot
be fetched) the server-rendered page reloads every refresh_seconds (15 to
30). The job page's diagram is model/chart.mmd (lib/dashboard_flow.py).

Secrets: only allowlisted columns are selected (never comment bodies or
environment values, and of a head only the 7-character short sha in `ci_runs`; the environment is not read except for the
ORCH_* path variables). SQLite takes the `round` field out of jobs.detail
with json_extract, so no detail value is read into Python; the round is
kept only when it is digits/digits.
Every free-text value (stuck reasons, poll_errors, deferrals, log lines)
goes through `redact`. It runs its rules
on the text as written and again on a decoded copy (percent and JSON
escapes undone, so encoded secrets are seen); what the decoded pass finds is
cut out of the original text with the rest of its token, so decoding only
ever widens what is hidden. The log tail also goes through
`redact_key_blocks`, which removes private-key blocks that span lines, with
markers matched in decoded form. The finished snapshot is walked once more so every
string in it has been through `redact` before it is written. Enumerated
fields (including ticket workflow states) outside their known values are
shown as "other".

Health (#92): the component heartbeats in orch.health.db beside the
database (lib/health.py, also opened read-only) become `health`; each
down component (including one machines.yml or orch.yml expects that has
never beaten), each harness with queued jobs but no live waiter, and an
orch.health.db that cannot be read is a needs-attention item linked to the
Health tab, and such a queued job carries `no_waiter`. With no
orch.health.db nothing is flagged.

Projects (#152): `projects` is each orch.yml project's tracker, ship mode
and round caps as the poller last read its factory.md (lib/project_rules.py).
An unreadable or not-yet-read factory.md is `unknown` with the reason.

Relay (#535): `relay` is {tracked, threads} for the Activity page. Threads
are the RELAY_TICKETS tickets that moved most recently, newest first, with
stopped tickets kept until they move again. Each is the ticket's reference
with its newest RELAY_STEPS `handoffs` (rows as in handoff_savings.recent)
and `verdicts` (rows as in poller.verdicts, plus `chip` and `words`, None
when the outcome has no plain words). `now` is where the ticket rests,
from its ticket record: {kind: "stopped", state, stopped_in, age_seconds},
{kind: "shipped", how, age_seconds}, {kind: "idle", state, age_seconds,
waiting_on} (who a held PR waits on, or None), or None for a ticket with
no record. A running or queued job is not in `now`: the page joins it
from `running` and `queued`.

Exposure (#1014): each `running` row's `exposure`, and each job in a
ticket flow visit's `jobs`, is its job_exposure row (lib/exposure.py):
{model, effort, harness, cli_version, brief_sha256, skill_paths, host,
factory_revision}, every field "unknown" when none was recorded, plus
`round`, the review round the job ran in: its detail's round number, else
its qa_runs round, else "unknown". skill_paths is a list, or
"unknown". A digest is a list of 16-character parts the page joins,
since a whole one reads as an opaque token and would be redacted; a value
that is not a digest is one part.

CI (#717): `ci_runs` is {runs, failures} from the ci_jobs table the poller
writes. A run is one ticket head's jobs, from every workflow; its status
is derived, never stored: any job in progress, or some done and some
queued, is in_progress; all queued is queued; all completed is failed when
any conclusion is in CHECK_FAILED, else passed. `runs` lists only queued
and in_progress runs, newest first: {repo, ticket_id, source, pr, tracker,
head_short, status, age_seconds, jobs}. A job is {name, status,
conclusion, run_id, job_id, runner_kind, runner_name, created_at,
started_at, completed_at, elapsed_seconds, queue_seconds}; runner_kind None means not looked up yet.
`failures` is the newest RECENT_CI_FAILURES failed jobs that completed in
the last CI_FAILURE_WINDOW at the current head: the ticket reference, head_short,
name, conclusion, run_id, job_id, tests (the first CI_TESTS_SHOWN failing
test names), more_tests, age_seconds since the job completed, and
run_checks, the {status, conclusion} of every job at that head. No URL
is stored, since redaction
hides every URL: the pages build the github.com PR, commit, job log and
issue-search links from these parts.

CI pool (#823): `ci_pool` is {state, age_seconds, runners} from the
ci_runners table: the self-hosted runners of each project whose factory.md
sets ci_runner, as the poller last read them. A runner is {repo, name, os,
status, busy, labels}, ordered by repo then name; status is online or
offline. age_seconds is the age of the oldest repo's read. state is unread
with no rows, live while age_seconds is at most CI_POOL_STALE_SECONDS,
else stale.

CI attention (#955): `ci_attention` is {base_red, stale}: base_red counts
the open tickets whose red CI is held because the PR base fails the same
check, stale the open tickets flagged needs-attention `ci-stale`.

CI lane (#823): `ci_lane` is lib/ci_lane.ci_lane(snapshot): the runner
slots, the GitHub-hosted, queued and failed groups and one card per ticket
with its progress counts. The script draws it; the server render works it
out again from ci_pool, ci_runs and tickets.

QA (#790): `qa` is {runs, cap, runners, queued} from the qa_runs and
qa_lanes tables the QA worker writes. `runs` holds each tracked ticket's
runs at the head of its newest run row of any scope or state, by ticket
and primary before shootout, then leaves out that head's scope none and
cancelled or stale runs, so an older head never shows: {repo,
ticket_id, runner, arm, state, scope, result, lanes, passed, total,
elapsed_seconds, run}. `lanes`
counts the distinct slices launched (base checks left out). `passed` and
`total` are set only once the run is reported: over the newest attempt of
each head slice (regression slices run on the base commit and are left
out), `total` is the scenarios asked and `passed` those its lane result
marks verified. `elapsed_seconds` runs from started_at to now, or
to finished_at once reported. `run` is the run id as two 16-character
halves (a whole 32-hex id would be redacted), or None when the id is not
32 hex; the page joins them into the `/qa/runs/<run_id>` evidence link.
The cloud agents' URLs are not carried. `cap` is orch.yml
qa.max_cloud_lanes (None without an OrchConfig), the lane cap each runner
has. `runners` is {runner, open} for each name in QA_RUNNERS, open being
its lanes launching or running. `queued` is {repo, count} of queued qa
jobs per repo, by repo. Missing tables read as no runs, no open lanes
and no queued jobs.

Ticket rows (#670, #671): each status.json `tickets` row carries
`state_age_seconds`, its record's value below (null when unknown), and
`shipped_at` and `shipped_how`, its record's `shipped.at` and `shipped.how`
(both null when the ticket is not shipped; `shipped_at` is also null when
the time is unknown). An `abandoned` ticket with a stored `closed_at` (#991)
counts its current visit, and so its age and `stopped.at`, from that close.

Titles (#1751): each feeder item and each `tickets` row carries `title`,
the feeder_items title through `_clean` capped at 120. A ticket row's is
looked up in the whole feeder_items table by repo (casefolded) and issue
(its source without the leading `#`, else its ticket id), null with no
match or no table.

Blockers (#1757): each feeder item in FEEDER_WAITING carries `blocked_by`,
a list of {kind, ref, path} read from its raw stored waiting_reason. `kind`
is `ticket` or `file`, and `path` is the held file or null. A `#N` label
takes the item's repo, and `ref` and `path` go through `_clean`. Three
whole reasons are read: `waiting on <labels>` drops each bracket note,
`waiting on file <path> held by <labels>` gives `file` entries, and
`waiting on a blocker cycle: A -> B -> A` drops the item itself at both
ends of the loop. Anything else gives [], and other items have no key.

Ticket records (#530): tickets.json holds one record per tracked ticket
(the tickets in status.json's `tickets`), built from the database whether
or not the ticket has a job. The snapshot carries them under RECORDS, so
the final scrub covers every string; the writer keeps that key out of
status.json and the pages. The file is compact JSON with sorted keys, and
every <, > and & is written as a JSON escape:

    {schema: 1, generated_at_epoch, tickets: {"<repo>#<ticket_id>": record}}
    record:   {repo, ticket_id, state, state_age_seconds, round, hops,
               off_chart_count, terminal, hops_to_shipped, all_durations_known,
               visits, shipped, stopped, jobs}
    visits:   job_flow's visits, unchanged: [{state, who, started_at,
               duration_seconds, time_known, off_chart, reason, current,
               jobs: [{job, action, recipient, exposure}]}], every job
               of the ticket in exactly one visit
    shipped:  null | {at, how: "merged" | "handed-off", intake_to_ship_seconds,
               revise_rounds, jobs, tokens_in, tokens_out, cost_usd}
    stopped:  null | {state, at, stopped_in, reason, ways_out: [{to, label}]}
    jobs:     the ticket's status.json running rows, newest first, then its
               queued rows, each without `flow`

A key is the stored repo and ticket id, redacted but not cut short, so it
can be longer than the record's shown `repo` and `ticket_id`. Tickets that
redaction alone makes equal take a `~<n>` suffix.

`shipped` is set when the ticket's state is on the Shipped shelf: `at` is
when it first reached a Shipped state, and `how` is "handed-off" when that
state was handed-off. Its totals cover every job the ticket ever had; tokens
and cost are null when no job reported them. `stopped` is set when the board
puts the ticket in its Stopped column; a stuck flag on a state that is not a
stop reads as stopped in "stuck". A record is shaped so the ticket page's
`_detail_model({"flow": record}, chart)` reads it as it reads a job row. A
job whose ticket is not tracked has no record.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import shutil
import tempfile
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple
from urllib.parse import unquote

import yaml

from lib import account_doctor, attention, epoch, exposure
from lib import health as component_health
from lib import live_settings
from lib.config_overlay import apply_overlay
from lib.dashboard_accounts import read_accounts
from lib.dashboard_config import REFRESH_DEFAULT, REFRESH_MAX, REFRESH_MIN
from lib.dashboard_evals import evals_json
from lib.dashboard_flow import (
    JobStep,
    Transition,
    anchor_close,
    job_flow,
    load_chart,
    shipped_facts,
    stop_facts,
)
from lib.dashboard_telemetry import attach as attach_telemetry
from lib.dashboard_telemetry import _maybe_count, read_telemetry, ticket_totals
from lib.dashboard_usage import read_usage
from lib.dispatch.constants import (
    CI_BASE_RED,
    CI_INFRA_BLOCKED_REASON,
    CI_RERUN_REFUSED,
    CI_STALE_REASON,
    CI_WAIT_REASONS,
    HANDED_OFF_EXTERNALLY,
    OK_OUTCOMES,
    OK_WITHOUT_APPROVE,
)
from lib.ci_lane import ci_lane
from lib.dispatch.types import CHECK_FAILED
from lib.dashboard_page import PAGES, STATIC_ASSETS, render_pages, render_static_assets
from lib.dashboard_page import duration as _duration
from lib.dashboard_pages import qa_evidence
from lib.dispatch.questions import extract_open_questions
from lib.factory_md import QA_RUNNERS
from lib.feeder import STATES as FEEDER_STATES
from lib.dispatch.cycle_clock import LOCK_ERRORS, SLOW_CYCLE
from lib.feeder_gates import ATTENTION_REASONS as FEEDER_ATTENTION_REASONS
from lib.feeder import FEEDER_TABLE, feeder_counts, feeder_items, last_feeder_poll
from lib.handoff import STATES as WORKFLOW_STATES
from lib.heal import POLLER_SERVICE
from lib.job_state import (
    DEAD,
    JOB_STATES,
    RUNNER_GONE,
    STALE,
    Alive,
    JobStateConfig,
    derive,
    liveness,
    read_placements,
    stale_cause,
)
from lib.linear_webhook import read_health
from lib.load import load_yaml
from lib.machines_yml import DEFAULT_ORCH_HOST
from lib.orch_yml import POLL_SECONDS, POLL_SECONDS_ALLOWED, OrchConfig
from lib.pr_title import SOURCE, read_sources
from lib.project_rules import read_rows as read_project_rules
from lib.qa_cloud import qa_id
from lib.queue import (
    ACTIONS, LOCAL_GATE_PASSED, RECIPIENTS, STATES, PriorityMark, passed_head, priority_marks,
)
from lib.role_router import role_matrix
from lib.schema import TICKETS
from lib import max_actions
from lib.status import PAUSED_STATES, _hold_approvers
from lib.wait import OWNING_BOTS
from lib.worktrees import pid_alive

SCHEMA = "factory-dashboard/v1"
RECORDS = "ticket_records"
EVALS = "evals"
TICKETS_SCHEMA = 1
LOG_TAIL_LINES = 50
LOG_TAIL_BYTES = 256 * 1024
RAW_LINE_LIMIT = 4096
LINE_LIMIT = 300
RECENT_ERRORS = 20
RECENT_DEFERRALS = 10
RECENT_HANDOFFS = 12
RECENT_VERDICTS = 12
RELAY_TICKETS = 12
RELAY_STEPS = 40
SETTINGS_AUDIT = "settings-audit.jsonl"
SETTINGS_RESULTS = 50
AUDIT_KEYS = ("id", "actor", "requested_at", "applied_at", "device", "field", "previous", "new",
              "result", "error", "backup", "restarted", "pending", "version", "target", "account")
AUDIT_RESULTS = ("applied", "pending", "ok", "signed-out", "mismatch", "expired", "cancelled", "failed", "refused")
# The Linear share of the hourly API budget the poller allows itself
# (lib/poll_source.py LINEAR_*_PER_HOUR * LINEAR_SHARE).
LINEAR_BUDGET_REQUESTS_PER_HOUR = 1250
LINEAR_BUDGET_COMPLEXITY_PER_HOUR = 1_500_000
# Poll health from the age of the last finished cycle (orch poll), scaled to
# the poller's own poll_seconds (30 or 60): ok up to two cycles plus 30
# seconds, late up to five cycles, stale after that.


def health_thresholds(poll_seconds: int) -> tuple[int, int]:
    """(ok, late) cycle-age limits in seconds for a `poll_seconds` poller."""
    return 2 * poll_seconds + 30, 5 * poll_seconds


HEALTH_OK_SECONDS, HEALTH_LATE_SECONDS = health_thresholds(POLL_SECONDS)
# A handled orch:v1 comment's outcome as the dispatcher writes it.
_OUTCOME = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
_ROUND = re.compile(r"[0-9]{1,3}/[0-9]{1,3}")
_DETAIL_ROUND = "CASE WHEN json_valid(detail) THEN json_extract(detail, '$.round') END"
MACHINES_MAX_BYTES = 256 * 1024
WAIT_RECIPIENTS = ("claude", "codex", "cursor", "gemini")
HARNESSES = ("claude", "codex", "cursor", "gemini")
TRACKERS = ("github", "linear")
ATTENTION_REASONS = (
    "ship-failed", "dedupe-dropped", "edited-comment", "base-mismatch", "stack-retarget-failed",
    "stack-parent-closed", "deploy-conflict", "linear-reopen-failed", "linear-not-reopened",
    SLOW_CYCLE, LOCK_ERRORS, CI_INFRA_BLOCKED_REASON, CI_RERUN_REFUSED, CI_STALE_REASON, OK_WITHOUT_APPROVE,
    *FEEDER_ATTENTION_REASONS, account_doctor.REASON, attention.CLEANUP_FAILING,
)

HANDOFF_SAVINGS_DEFINITION = (
    "scripted = jobs for claude, codex or cursor that `orch wait` claimed and "
    "the bot finished with an orch:v1 handoff comment (jobs.state = 'done' "
    "with a handoff_comment_id). Each one is a handoff routed from an orch:v1 "
    "comment to a bot by script, with no bot messaging another bot. "
    "scripted_in_flight = such jobs claimed and still running. "
    "bot_to_bot = jobs routed to grok: each one wakes Grok Bot to read the "
    "ticket and hand off by hand (stops, clarify, refusals, ship failures). "
    "scripted_share = scripted / (scripted + bot_to_bot). The database does not "
    "record bot messages sent outside the factory, so grok wakes are the "
    "measurable stand-in for bot-to-bot handoffs. "
    "engine_handled = granted plus landed rulings in the engine_decisions log, "
    "and engine_escalated = escalated rulings in that log."
)
SCRIPTED_SHARE_DEFINITION = (
    "Scripted share is the part of handoffs a script routed straight to the "
    "next bot, scripted / (scripted + grok wakes), rather than one bot waking another."
)


class DashboardError(RuntimeError):
    pass


# --- redaction --------------------------------------------------------------

_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s'\"<>]+")
_USERINFO = re.compile(r"[^\s:/@'\"]+:[^\s/@'\"]+@[A-Za-z0-9.\-]+")
# The optional quote after a name catches JSON and repr keys ("token": ...).
_AUTH_HEADER = re.compile(
    r"(?i)\b((?:proxy-)?authorization|x-api-key|api-key|cookie|set-cookie)"
    r"([\"']?\s*[:=]\s*)[^\r\n]*"
)
_SCHEME_TOKEN = re.compile(r"(?i)\b(bearer|basic|token)(\s+)([A-Za-z0-9._~+/=\-]{6,})")
_KNOWN_TOKEN = re.compile(
    r"\b(?:"
    r"gh[pousr]_[A-Za-z0-9_]{16,}"
    r"|github_pat_[A-Za-z0-9_]{16,}"
    r"|lin_(?:api|oauth)_[A-Za-z0-9_]{16,}"
    r"|glpat-[A-Za-z0-9_\-]{16,}"
    r"|xox[abposr]-[A-Za-z0-9\-]{10,}"
    r"|sk-[A-Za-z0-9_\-]{16,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|eyJ[A-Za-z0-9_\-]{8,}(?:\.[A-Za-z0-9_\-]+)*"
    r")"
)
SECRET_NAME = (
    r"[A-Za-z0-9_\-]*"
    r"(?:token|secret|passw(?:or)?d|passphrase|pwd|api[_\-]?key|key|auth"
    r"|credential|session|cookie|signature)"
    r"[A-Za-z0-9_\-]*"
)
_SECRET_ASSIGNMENT = re.compile(
    rf"(?i)\b({SECRET_NAME})"
    r"([\"']?\s*[:=]\s*)"
    # A quoted string (escapes allowed, closing quote optional when cut short)
    # or a bare value.
    r"(\"(?:[^\"\\\n]|\\.)*\"?|'(?:[^'\\\n]|\\.)*'?|[^\s,;}\]]+)"
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
# 32+ token characters; _opaque keeps ones that read like names, not keys.
_OPAQUE = re.compile(r"(?<![A-Za-z0-9_\-+/=])[A-Za-z0-9_\-+/=]{32,}")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# JSON string escapes that can hide a URL or a quoted key: \/ \" \' and
# \u00XX (ASCII only, so no lone surrogate can reach the UTF-8 writer).
_JSON_ESCAPE = re.compile(r"\\(?:u00([0-7][0-9A-Fa-f])|([/\"']))")
DECODE_ROUNDS = 8
REDACT_PASSES = 4
ENCODED_MARK = "[redacted-encoded]"
# PEM/OpenSSH/PGP armor. A block without its END runs to the end of the text.
_KEY_MARKER = r"-----\s*{0} [A-Z0-9 ]{{1,60}}-----"
_KEY_BEGIN = re.compile(_KEY_MARKER.format("BEGIN"))
_KEY_END = re.compile(_KEY_MARKER.format("END"))
_KEY_BLOCK = re.compile(
    _KEY_MARKER.format("BEGIN") + r"[\s\S]*?(?:" + _KEY_MARKER.format("END") + r"|\Z)"
)
_KEY_ORPHAN_END = re.compile(r"\A[\s\S]*?" + _KEY_MARKER.format("END"))
KEY_BLOCK_MARK = "[redacted-key-block]"


# Each rule returns None to leave a match alone, or (keep, marker): keep the
# first `keep` characters of the match (a key name, a scheme) and replace the
# rest with the marker.
Rule = Callable[[re.Match[str]], "tuple[int, str] | None"]


def _whole(marker: str) -> Rule:
    return lambda match: (0, marker)


def _scheme_token(match: re.Match[str]) -> tuple[int, str] | None:
    scheme, space, value = match.groups()
    # "Bearer x" is always a credential; "token"/"basic" need a token-like value.
    if scheme.lower() != "bearer" and not (
        any(ch.isdigit() for ch in value) or "=" in value or len(value) >= 16
    ):
        return None
    return len(scheme) + len(space), "[redacted]"


def _auth_header(match: re.Match[str]) -> tuple[int, str]:
    return len(match.group(1)) + len(match.group(2)), "[redacted]"


def _assignment(match: re.Match[str]) -> tuple[int, str] | None:
    if match.group(3).startswith("[redacted"):
        return None
    return len(match.group(1)) + len(match.group(2)), "[redacted]"


def _opaque(match: re.Match[str]) -> tuple[int, str] | None:
    value = match.group(0)
    digits = sum(ch.isdigit() for ch in value)
    letters = sum(ch.isalpha() for ch in value)
    # A repo path like owner1/some-long-repo-name has at most one digit.
    return (0, "[redacted]") if digits >= 2 and letters >= 2 else None


_RULES = (
    (_KEY_BLOCK, _whole(KEY_BLOCK_MARK)),
    (_KEY_ORPHAN_END, _whole(KEY_BLOCK_MARK)),
    (_URL, _whole("[redacted-url]")),
    (_AUTH_HEADER, _auth_header),
    (_SCHEME_TOKEN, _scheme_token),
    (_KNOWN_TOKEN, _whole("[redacted-token]")),
    (_USERINFO, _whole("[redacted-credentials]")),
    (_EMAIL, _whole("[redacted-email]")),
    (_SECRET_ASSIGNMENT, _assignment),
    (_OPAQUE, _opaque),
)
_NON_OPAQUE_RULES = tuple(item for item in _RULES if item[0] is not _OPAQUE)
MARKER = -1  # origin of a character a rule inserted


def _apply_rules(
    text: str,
    rules: Sequence[tuple[re.Pattern[str], Callable[[re.Match[str]], tuple[int, str] | None]]] = _RULES,
) -> tuple[str, list[int], set[int]]:
    """Run every rule in order over text.

    Returns the redacted text, the origin of each of its characters (an
    index into `text`, or MARKER for inserted marker text) and the set of
    indices into `text` that some rule removed.
    """
    origin = list(range(len(text)))
    removed: set[int] = set()
    for pattern, rule in rules:
        pieces: list[str] = []
        origins: list[int] = []
        last = 0
        for match in pattern.finditer(text):
            decision = rule(match)
            if decision is None:
                continue
            keep, marker = decision
            cut = match.start() + keep
            pieces.append(text[last:cut])
            origins.extend(origin[last:cut])
            pieces.append(marker)
            origins.extend([MARKER] * len(marker))
            removed.update(index for index in origin[cut : match.end()] if index >= 0)
            last = match.end()
        if pieces:
            pieces.append(text[last:])
            origins.extend(origin[last:])
            text, origin = "".join(pieces), origins
    return text, origin, removed


def _hex(text: str) -> bool:
    return len(text) == 2 and all(ch in "0123456789abcdefABCDEF" for ch in text)


def _decode_once(
    text: str, spans: list[tuple[int, int]]
) -> tuple[str, list[tuple[int, int]]]:
    """One round of percent and JSON-escape decoding, keeping for every output
    character the span of original characters it came from."""
    out: list[str] = []
    out_spans: list[tuple[int, int]] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "%" and _hex(text[index + 1 : index + 3]):
            # A run of %XX bytes decodes as UTF-8 together, like unquote.
            end = index
            data = bytearray()
            while text[end : end + 1] == "%" and _hex(text[end + 1 : end + 3]):
                data.append(int(text[end + 1 : end + 3], 16))
                end += 3
            span = (spans[index][0], spans[end - 1][1])
            for decoded in data.decode("utf-8", errors="replace"):
                out.append(decoded)
                out_spans.append(span)
            index = end
            continue
        if char == "\\":
            match = _JSON_ESCAPE.match(text, index)
            if match is not None:
                out.append(
                    chr(int(match.group(1), 16)) if match.group(1) else match.group(2)
                )
                out_spans.append((spans[index][0], spans[match.end() - 1][1]))
                index = match.end()
                continue
        out.append(char)
        out_spans.append(spans[index])
        index += 1
    return "".join(out), out_spans


def _decode_map(text: str) -> tuple[str, list[tuple[int, int]], bool]:
    """Undo percent-encoding and JSON string escapes until nothing changes.

    Repeated, so double-encoded text (%253A) is seen too. Returns the
    decoded text, for each decoded character the [start, end) span of
    `text` it came from, and whether decoding settled within DECODE_ROUNDS.
    Control characters an escape produced (%0A is kept as a line break,
    \u0000 is dropped) are removed, as in `_strip_control`.
    """
    spans = [(index, index + 1) for index in range(len(text))]
    current = text
    settled = False
    for _ in range(DECODE_ROUNDS):
        decoded, decoded_spans = _decode_once(current, spans)
        if decoded == current:
            settled = True
            break
        current, spans = decoded, decoded_spans
    kept = [
        (char, span)
        for char, span in zip(current, spans)
        if char == "\n" or not _CONTROL.match(char)
    ]
    current = "".join(" " if char == "\t" else char for char, _ in kept)
    return current, [span for _, span in kept], settled


def _token_end(text: str, index: int) -> int:
    while index < len(text) and not text[index].isspace():
        index += 1
    return index


def _widen(text: str, decoded: str, spans: list[tuple[int, int]]) -> set[int]:
    """Indices of `text` to hide because its decoded form holds a secret.

    The rules run again over the decoded text. Every character they remove
    maps back to the original characters it came from, and the hidden part
    runs on from there to the end of that original whitespace-separated
    token. A separator that exists only once decoded (%20, %3B, %22, \")
    can therefore end a match early but never shows what follows it.
    """
    if decoded == text:
        return set()
    _, _, removed = _apply_rules(decoded)
    hidden: set[int] = set()
    run_start = previous = None
    for index in sorted(removed) + [None]:
        if index is not None and previous is not None and index == previous + 1:
            previous = index
            continue
        if run_start is not None and previous is not None:
            start = spans[run_start][0]
            end = _token_end(text, spans[previous][1])
            hidden.update(range(start, end))
        run_start = previous = index
    return hidden


def _drop(text: str, origin: list[int], hidden: set[int]) -> str:
    """Replace each run of characters whose origin is hidden with a marker
    (none when the run touches a marker already there)."""
    pieces: list[str] = []
    index = 0
    while index < len(text):
        if origin[index] not in hidden or origin[index] == MARKER:
            pieces.append(text[index])
            index += 1
            continue
        end = index
        while end < len(text) and origin[end] != MARKER and origin[end] in hidden:
            end += 1
        touches = (index > 0 and origin[index - 1] == MARKER) or (
            end < len(text) and origin[end] == MARKER
        )
        if not touches:
            pieces.append("[redacted]")
        index = end
    return "".join(pieces)


def _redact_pass(text: str) -> str:
    decoded, spans, settled = _decode_map(text)
    if not settled:
        # Still changing after DECODE_ROUNDS rounds: too deep to inspect.
        return ENCODED_MARK
    redacted, origin, _ = _apply_rules(text)
    hidden = _widen(text, decoded, spans)
    return _drop(redacted, origin, hidden) if hidden else redacted


def redact(text: str) -> str:
    """Remove anything shaped like a credential, key, URL or email address.

    The rules run on the text as written, then on its decoded form (percent
    and JSON escapes undone); anything the decoded pass finds is cut out of
    the original text together with the rest of its token (see `_widen`).
    Decoding can only widen what is hidden. The result is the original text
    with secrets replaced by markers, never a decoded form, and is checked
    again until a further pass changes nothing.
    """
    text = _strip_control(text)
    for _ in range(REDACT_PASSES):
        redacted = _redact_pass(text)
        if redacted == text:
            return redacted
        text = redacted
    return ENCODED_MARK if _redact_pass(text) != text else text


def _starts_inside_key_block(decoded_lines: Sequence[str]) -> bool:
    for line in decoded_lines:
        begin = _KEY_BEGIN.search(line)
        end = _KEY_END.search(line)
        if end is not None and (begin is None or end.start() < begin.start()):
            return True
        if begin is not None:
            return False
    return False


def redact_key_blocks(lines: Sequence[str]) -> list[str]:
    """Replace every log line inside a private-key block with a marker.

    The tail is read line by line, so a block spans entries: the BEGIN line,
    the base64 body (whose short last line no other rule would catch) and
    the END line. The markers are looked for in each line's decoded form,
    so percent- or JSON-escaped markers count too; the text kept around a
    marker is the original. When the tail starts inside a block (an END
    comes before any BEGIN, because the BEGIN line scrolled out), the lines
    up to that END are treated as the block's body. A block with no END
    hides the rest of the tail.
    """
    decoded_lines = [_decode_map(line) for line in lines]
    inside = _starts_inside_key_block([decoded for decoded, _, _ in decoded_lines])
    result: list[str] = []
    for line, (decoded, spans, _) in zip(lines, decoded_lines):
        pieces: list[str] = []
        raw_at = 0
        decoded_at = 0
        if inside:
            end = _KEY_END.search(decoded)
            if end is None:
                result.append(KEY_BLOCK_MARK)
                continue
            pieces.append(KEY_BLOCK_MARK)
            raw_at = spans[end.end() - 1][1]
            decoded_at = end.end()
            inside = False
        while True:
            block = _KEY_BLOCK.search(decoded, decoded_at)
            if block is None:
                break
            pieces.append(line[raw_at : max(raw_at, spans[block.start()][0])])
            pieces.append(KEY_BLOCK_MARK)
            if _KEY_END.search(block.group(0)) is None:
                inside = True
                raw_at = len(line)
                break
            raw_at = spans[block.end() - 1][1]
            decoded_at = block.end()
        pieces.append(line[raw_at:])
        result.append("".join(pieces))
    return result


def _strip_control(text: str) -> str:
    return _CONTROL.sub("", text.replace("\t", " "))


def _clean(value: object, limit: int = LINE_LIMIT) -> str:
    """Free text: strip control characters, redact, then cap the length."""
    text = redact(_strip_control(str(value)))
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _enum(value: object, allowed: Iterable[str]) -> str:
    return str(value) if str(value) in tuple(allowed) else "other"


FEEDER_WAITING = ("waiting-blocked", "waiting-capacity")
_WAIT_REPO = r"[\w.-]{1,39}/[\w.-]{1,100}"
_WAIT_LABEL = rf"(?:(?:{_WAIT_REPO})?#[1-9][0-9]{{0,9}}|[A-Z][A-Z0-9]{{0,9}}-[1-9][0-9]{{0,9}})"
_WAIT_NOTE = r" \([^()]*\)"
_WAIT_PART = rf"{_WAIT_LABEL}(?:{_WAIT_NOTE})?"
_WAIT_REASON = re.compile(
    "waiting on (?:"
    rf"a blocker cycle: (?P<cycle>{_WAIT_LABEL}(?: -> {_WAIT_LABEL})+)"
    rf"|file (?P<path>\S{{1,{LINE_LIMIT}}}) held by (?P<holders>{_WAIT_LABEL}(?:, {_WAIT_LABEL})*)"
    rf"|(?P<labels>{_WAIT_PART}(?:, {_WAIT_PART})*)"
    ")",
    re.ASCII,
)


def _blocked_by(reason: object, repo: object) -> list[dict]:
    if not (isinstance(reason, str) and isinstance(repo, str) and re.fullmatch(_WAIT_REPO, repo, re.ASCII)):
        return []
    match = _WAIT_REASON.fullmatch(reason)
    if match is None:
        return []
    path = match["path"]
    if match["cycle"]:
        loop = match["cycle"].split(" -> ")
        labels = [label for label in loop[1:-1] if label != loop[0]] if loop[0] == loop[-1] else []
    elif path:
        labels = match["holders"].split(", ")
    else:
        labels = re.sub(_WAIT_NOTE, "", match["labels"]).split(", ")
    return [
        {
            "kind": "file" if path else "ticket",
            "ref": _clean(repo + label if label.startswith("#") else label, 160),
            "path": _clean(path) if path else None,
        }
        for label in labels
    ]


def _scrub(value: object) -> object:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


# --- reading ------------------------------------------------------------------


def open_readonly(path: Path) -> sqlite3.Connection:
    """Open an existing database with a SQLite mode=ro URI and query_only.

    Symlinks are resolved first, so the existence check, the URI and the
    -wal/-shm files SQLite uses all belong to the real file. It never uses
    immutable=1: the poller's writers come and go (the -wal is removed when
    the last one closes), so SQLite's normal shared locks and WAL reads are
    what keep a read consistent while a writer commits or checkpoints. Like
    any reader of a WAL database, SQLite may create the -wal and -shm files
    beside it. When it cannot (a read-only directory), the open fails with
    that explanation rather than falling back to an unlocked read.
    """
    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise DashboardError(f"database does not exist: {path}") from None
    if not real.is_file():
        raise DashboardError(f"database does not exist: {path}")
    uri = real.as_uri() + "?mode=ro"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        # Opening is lazy; read the schema now so a failure surfaces here.
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return connection
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        message = f"cannot read database {real}: {exc}"
        if not os.access(real.parent, os.W_OK):
            message += (
                f" ({real.parent} is not writable, so SQLite cannot create the"
                " -wal/-shm files a WAL database is read through; the dashboard"
                " does not fall back to an unlocked immutable=1 read)"
            )
        raise DashboardError(message) from exc


def read_log_tail(path: Path, *, lines: int = LOG_TAIL_LINES) -> list[str]:
    """Return the last `lines` lines of a regular file, control characters removed.

    Only the final LOG_TAIL_BYTES are read. A line longer than RAW_LINE_LIMIT
    is replaced by a marker rather than cut, so a cut can never leave half a
    secret that the redactor no longer recognizes.
    """
    if not path.is_file():
        return []
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        start = max(0, size - LOG_TAIL_BYTES)
        handle.seek(start)
        data = handle.read(size - start)
    raw = data.split(b"\n")
    if start > 0 and raw:
        raw = raw[1:]
    if raw and raw[-1] == b"":
        raw = raw[:-1]
    result: list[str] = []
    for item in raw[-lines:]:
        text = item.decode("utf-8", errors="replace").rstrip("\r")
        if len(text) > RAW_LINE_LIMIT:
            result.append("[long line omitted]")
        else:
            result.append(_strip_control(text))
    return result


def _age(now: float, then: float | str | None) -> int | None:
    if then is None:
        return None
    return int(max(0.0, now - epoch.read(then)))


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


_VERDICT_TIMES = (
    ("jobs", "MIN", "created_at", "source_comment_id"),
    ("jobs", "MAX", "finished_at", "handoff_comment_id"),
    ("ticket_state", "MAX", "approved_at", "approve_comment_id"),
    ("ship_holds", "MAX", "held_at", "approve_comment_id"),
    ("ticket_retries", "MAX", "recorded_at", "comment_id"),
    ("followup_filings", "MAX", "created_at", "comment_id"),
    ("auto_extra_comments", "MAX", "created_at", "comment_id"),
    ("verdict_heads", "MAX", "posted_at", "comment_id"),
    ("handled_times", "MAX", "handled_at", "comment_id"),
    ("verdict_acceptances", "MAX", "accepted_at", "comment_id"),
    ("qa_runs", "MAX", "finished_at", "comment_id"),
)


def collect_snapshot(
    db_path: Path,
    log_path: Path,
    *,
    now: float,
    machines_path: Path | None = None,
    refresh_seconds: int = REFRESH_DEFAULT,
    last_cycle_at: float | None = None,
    chart_path: Path | None = None,
    job_states: JobStateConfig | None = None,
    alive: Alive | None = pid_alive,
    poll_seconds: int = POLL_SECONDS,
    orch: OrchConfig | None = None,
    publish_seconds: float | None = None,
    uptime: component_health.Uptime | None = None,
) -> dict:
    """Read everything once. `last_cycle_at` is when the poller's last cycle
    ended (the live push passes it); without it poll health is "unknown".
    `poll_seconds` is the poller's orch.yml poll_seconds (30 or 60): poll
    health's `cycle_seconds` and its ok and late thresholds follow it.
    `chart_path` is the statechart chart the job page draws (default
    model/chart.mmd); only its file name is copied out. `job_states` is
    orch.yml's job_states (the stale threshold); `alive` checks a recorded
    runner pid on this host (lib/job_state.py), never over SSH. `orch` is
    the loaded orch.yml, which says whether the Linear listener and the
    dashboard writer are expected to heartbeat (lib/health.py).
    `publish_seconds` is how often a pushing writer publishes the snapshot
    (None for a one-off snapshot): the page keeps the component states it
    observed until a newer snapshot is overdue (lib/dashboard_page.py).
    `uptime` is the boot and monotonic clock at `now`, so a heartbeat's age
    leaves out time the box was suspended (lib/health.py)."""
    if (
        isinstance(refresh_seconds, bool)
        or not isinstance(refresh_seconds, int)
        or not REFRESH_MIN <= refresh_seconds <= REFRESH_MAX
    ):
        raise DashboardError("refresh_seconds must be 15 to 30")
    if last_cycle_at is not None and (
        isinstance(last_cycle_at, bool)
        or not isinstance(last_cycle_at, (int, float))
        or not math.isfinite(last_cycle_at)
    ):
        raise DashboardError("last_cycle_at must be a Unix time")
    if (
        isinstance(poll_seconds, bool)
        or not isinstance(poll_seconds, int)
        or poll_seconds not in POLL_SECONDS_ALLOWED
    ):
        raise DashboardError("poll_seconds must be 30 or 60")
    chart = load_chart(chart_path)
    machines = (
        component_health.load_machines(machines_path) if machines_path is not None else None
    )
    machines_state, machine_items, host = _machines_yaml(machines_path)
    connection = open_readonly(db_path)
    try:
        connection.execute("BEGIN")
        db = _read_db(
            connection,
            now,
            chart,
            job_states if job_states is not None else JobStateConfig(),
            alive,
            host,
        )
        telemetry = read_telemetry(connection, now)  # issue #93
        rules = read_project_rules(connection)  # issue #152
        usage = read_usage(connection, now, machines)  # issue #151
        accounts = read_accounts(connection, now, machines)
        live = live_settings.peek(connection)
        connection.execute("COMMIT")
    except sqlite3.Error as exc:
        raise DashboardError(f"cannot read database: {exc}") from exc
    finally:
        connection.close()

    log_present = log_path.is_file()
    log_mtime = log_path.stat().st_mtime if log_present else None
    tail = (
        [_clean(line) for line in redact_key_blocks(read_log_tail(log_path))]
        if log_present
        else []
    )
    activity = [
        value
        for value in (log_mtime, db["newest_error_at"], db["newest_deferral_at"])
        if value is not None
    ]
    # One slot count per device (#90): a running harness job with no
    # placement runs on the orch host, so it counts in that host's held slots,
    # and slots.box.running is every running job on the host, placed or not.
    devices, box_max = _machines(machine_items, host, db["held"], unplaced=db["unplaced_running"])
    box_running = db["unplaced_running"] + db["running_on"].get(host, 0)
    attach_telemetry([*db["running"], *db["queued"]], telemetry, now)
    report = component_health.load_report(
        db_path,
        now,
        queued=[(item["job"], item["recipient"]) for item in db["queued"]],
        machines=machines,
        orch=orch,
        uptime=uptime,
    )
    for item in db["queued"]:
        flag = report.no_waiter_for(item["recipient"])
        if flag is not None:
            item["no_waiter"] = flag.harness
            item["no_waiter_link"] = f"health.html#{flag.anchor}"
    records = {
        db["record_keys"][key]: {
            **record,
            "jobs": [
                {name: value for name, value in item.items() if name not in ("flow", "record")}
                for item in db["ticket_jobs"].get(key, [])
            ],
        }
        for key, record in db["ticket_records"].items()
    }

    snapshot = {
        "schema": SCHEMA,
        "generated_at": datetime.fromtimestamp(now, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "generated_at_epoch": now,
        "refresh_seconds": refresh_seconds,
        "queue": db["queue"],
        "running": db["running"],
        "queued": db["queued"],
        "slots": {
            "machines_yml": machines_state,
            "box": {"name": _clean(host, 40), "running": box_running, "slot_max": box_max},
            "devices": devices,
        },
        "settings": {"results": _settings_results(db_path.parent / SETTINGS_AUDIT, live)},
        "stuck": {
            "tickets": db["stuck_tickets"],
            "overdue_jobs": db["overdue_jobs"],
        },
        "paused": db["paused"],
        "tickets": db["tickets"],
        "poller": {
            "log": {
                "present": log_present,
                "age_seconds": _age(now, log_mtime),
                "tail": tail,
            },
            "last_activity_age_seconds": _age(now, max(activity)) if activity else None,
            "errors": db["errors"],
            "deferrals": db["deferrals"],
            # Only when a Linear webhook listener or webhook-mode poller has
            # run on this database.
            **(
                {"linear_webhook": db["linear_webhook"]}
                if db["linear_webhook"] is not None
                else {}
            ),
            "health": _poll_health(
                _age(now, last_cycle_at),
                _age(now, max(activity)) if activity else None,
                poll_seconds,
            ),
            "linear_budget": db["linear_budget"],
            "verdicts": db["verdicts"],
        },
        "handoff_savings": db["handoff_savings"],
        "relay": db["relay"],
        "ci_runs": db["ci_runs"],
        "ci_pool": db["ci_pool"],
        "ci_attention": db["ci_attention"],
        "qa": {**db["qa"], "cap": orch.qa.max_cloud_lanes if orch is not None else None},
        "attention": _attention(
            db["stuck_tickets"], db["overdue_jobs"], db["paused"], db["attention_flags"],
            report, db["tickets"],
        ),
        "health": {**_without_checkout_paths(component_health.report_json(report, now)),
                   "publish_seconds": publish_seconds},
        # The backlog feeder at its last poll (issue #85); None when none has run.
        "feeder": db["feeder"],
        "statechart": chart,
        "telemetry": telemetry["rollup"],
        "projects": _projects(orch, rules, now),
        "rules": rules_and_limits(chart),
        "usage": usage,
        "accounts": accounts,
        "routing": role_matrix(),
        RECORDS: records,
    }
    snapshot["ci_lane"] = ci_lane(snapshot)
    return _scrub(snapshot)  # type: ignore[return-value]



def _without_checkout_paths(health: dict) -> dict:
    return {**health, "devices": [{key: value for key, value in device.items() if key != "checkout"}
                                  for device in health["devices"]]}


def _ship_rule(labels: list[str]) -> str:
    parts = [label.removeprefix("ship = ").partition(", ") for label in labels]
    text = "{}, {}, or {}".format(*(way for way, _, _ in parts))
    conditions = {condition for _, _, condition in parts}
    shared = conditions.pop() if len(conditions) == 1 else ""
    return f"{text}, when {shared}" if shared else text


_CHART_RULES = (
    ("stuck", (("building", "stuck"),), lambda labels: labels[0]),
    ("plan-check", (("plan-check", "planning"), ("plan-check", "stuck")),
     lambda labels: f"sends work back when {labels[0]}; {labels[1]} → stuck"),
    ("rounds", (("review", "fixing"), ("review", "capped"), ("capped", "fixing")),
     lambda labels: f"{labels[0]} → fixing; {labels[1]} → capped ({labels[2]})"),
    ("ship", (("ship", "merged"), ("ship", "handed-off"), ("ship", "waiting-external")),
     _ship_rule),
    ("approver", (("waiting-external", "stuck"),),
     lambda labels: f"{labels[0]} from an external approver → stuck"),
)


def rules_and_limits(chart: dict | None) -> list[dict]:
    rows = []
    edges = (
        {} if chart is None
        else {(edge["from"], edge["to"]): edge["label"] for edge in chart["edges"]}
    )
    for key, wanted, join in _CHART_RULES:
        labels = [edges.get(pair) for pair in wanted]
        if chart is None:
            text = "chart unavailable"
        elif None in labels:
            text = "not in the chart"
        else:
            text = join(labels)
        rows.append({"key": key, "text": text})
    return rows


def _ci_waits(connection: sqlite3.Connection, now: float) -> dict[tuple[str, str], dict]:
    found = {}
    columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(ci_waits)")}
    next_round = "next_round" if "next_round" in columns else "NULL"
    for row in connection.execute(
        f"SELECT repo, ticket_id, reason, checks, since, {next_round} AS next_round FROM ci_waits"
    ):
        try:
            names = json.loads(row["checks"])
        except ValueError:
            names = []
        found[(str(row["repo"]).casefold(), str(row["ticket_id"]))] = {
            "reason": _enum(row["reason"], CI_WAIT_REASONS),
            "checks": [_clean(name, 80) for name in names] if isinstance(names, list) else [],
            "waiting_seconds": _age(now, row["since"]),
            **({"round": int(row["next_round"])} if row["next_round"] is not None else {}),
        }
    return found


def _ci_attention(connection: sqlite3.Connection, tables: set[str]) -> dict[str, int]:
    open_ticket = (
        """
        NOT EXISTS (
            SELECT 1 FROM ticket_state AS state
            WHERE state.repo = row.repo AND state.ticket_id = row.ticket_id
              AND COALESCE(state.workflow_state, '') IN ('merged', 'done')
        )
        """
        if "ticket_state" in tables
        else "1"
    )
    held = [
        f"SELECT repo, ticket_id FROM {table} AS row WHERE {column} = ? AND {open_ticket}"
        for table, column in (("ci_waits", "reason"), ("ci_stale", "state"))
        if table in tables
    ]
    base_red = (
        connection.execute(
            f"SELECT COUNT(*) FROM ({' UNION '.join(held)})", (CI_BASE_RED,) * len(held)
        ).fetchone()[0]
        if held
        else 0
    )
    stale = (
        connection.execute(
            f"SELECT COUNT(*) FROM needs_attention AS row WHERE reason = ? AND {open_ticket}",
            (CI_STALE_REASON,),
        ).fetchone()[0]
        if "needs_attention" in tables
        else 0
    )
    return {"base_red": int(base_red), "stale": int(stale)}


def _no_ci_bases(connection: sqlite3.Connection, tables: set[str]) -> dict[tuple[str, str], dict]:
    epoch.register(connection)
    bases = {
        (str(row["repo"]).casefold(), str(row["ticket_id"])): {"base": _clean(row["base"], 80)}
        for row in connection.execute("SELECT repo, ticket_id, base FROM no_ci_bases")
    }
    if "job_events" in tables:
        for row in connection.execute(
            f"""
            SELECT event.repo, event.ticket_id, event.detail FROM job_events AS event
            JOIN no_ci_bases AS base ON base.repo = event.repo AND base.ticket_id = event.ticket_id
            WHERE event.event = ? ORDER BY {epoch.sql('event.recorded_at')}, event.id
            """,
            (LOCAL_GATE_PASSED,),
        ):
            key = (str(row["repo"]).casefold(), str(row["ticket_id"]))
            if key in bases and (head := passed_head(row["detail"])) is not None:
                bases[key]["gate_head"] = head[:12]
    return bases


CI_JOB_STATUSES = ("queued", "in_progress", "completed")
CI_CONCLUSIONS = (
    "success", "failure", "neutral", "cancelled", "skipped", "timed_out", "action_required",
    "startup_failure", "stale",
)
CI_RUNNER_KINDS = ("self-hosted", "github-hosted", "none")
CI_RUNNER_STATUSES = ("online", "offline")
CI_POOL_STALE_SECONDS = 300
CI_FAILURE_WINDOW = 86400
RECENT_CI_FAILURES = 10
CI_TESTS_SHOWN = 5
_CI_ID = re.compile(r"[0-9]{1,20}")
_CI_HEAD = re.compile(r"[0-9a-f]{7,64}")
_CI_JOB_LOG = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/actions/runs/([0-9]{1,20})/job/([0-9]{1,20})")


def _ci_run_status(jobs: list[dict]) -> str:
    statuses = {job["status"] for job in jobs}
    if statuses == {"completed"}:
        return "failed" if any(job["conclusion"] in CHECK_FAILED for job in jobs) else "passed"
    if "in_progress" in statuses or "completed" in statuses:
        return "in_progress"
    return "queued"


def _ci_log_ids(row: sqlite3.Row) -> dict:
    run_id, job_id = (str(row[name] or "") for name in ("run_id", "job_id"))
    if not (_CI_ID.fullmatch(run_id) and _CI_ID.fullmatch(job_id)):
        found = _CI_JOB_LOG.fullmatch(str(row["log_url"] or ""))
        run_id, job_id = found.groups() if found else (None, None)
    return {"run_id": run_id, "job_id": job_id}


def _ci_tests(text: object) -> tuple[list[str], int]:
    try:
        names = json.loads(str(text)) if text is not None else []
    except ValueError:
        names = []
    names = [_clean(name, 200) for name in names if isinstance(name, str)] if isinstance(names, list) else []
    return names[:CI_TESTS_SHOWN], max(0, len(names) - CI_TESTS_SHOWN)


def _ci_labels(text: object) -> list[str]:
    try:
        labels = json.loads(str(text))
    except ValueError:
        labels = []
    return [_clean(label, 64) for label in labels if isinstance(label, str)] if isinstance(labels, list) else []


def _ci_pool(connection: sqlite3.Connection, now: float) -> dict:
    rows = connection.execute("SELECT * FROM ci_runners ORDER BY repo, name").fetchall()
    if not rows:
        return {"state": "unread", "age_seconds": None, "runners": []}
    age = _age(now, min(epoch.read(row["seen_at"]) for row in rows))
    return {
        "state": "live" if age is not None and age <= CI_POOL_STALE_SECONDS else "stale",
        "age_seconds": age,
        "runners": [
            {
                "repo": _clean(row["repo"], 100),
                "name": _clean(row["name"], 64),
                "os": _clean(row["os"], 32),
                "status": _enum(row["status"], CI_RUNNER_STATUSES),
                "busy": bool(row["busy"]),
                "labels": _ci_labels(row["labels"]),
            }
            for row in rows
        ],
    }


def _ci_runs(
    connection: sqlite3.Connection,
    now: float,
    sources: dict[tuple[str, str], tuple[str | None, str | None]],
    trackers: dict[tuple[str, str], str | None],
) -> dict:
    epoch.register(connection)
    grouped: dict[tuple[str, str, str], list[dict]] = {}
    refs: dict[tuple[str, str, str], dict] = {}
    failures = []
    for row in connection.execute(
        f"SELECT * FROM ci_jobs ORDER BY {epoch.sql('COALESCE(created_at, seen_at)')}, check_name"
    ):
        key = (str(row["repo"]).casefold(), str(row["ticket_id"]))
        head = str(row["head"])
        ref = _ticket_ref(sources, row["repo"], row["ticket_id"])
        pr = str(row["pr"] or "")
        if _CI_ID.fullmatch(pr):
            ref["pr"] = pr
        ref.setdefault("pr", None)
        ref.setdefault("source", None)
        ref["tracker"] = trackers.get(key)
        ref["head_short"] = head[:7] if _CI_HEAD.fullmatch(head) else None
        status = _enum(row["status"], CI_JOB_STATUSES)
        conclusion = _enum(row["conclusion"], CI_CONCLUSIONS) if row["conclusion"] is not None else None
        created, started, completed = (
            epoch.cell(row[name]) for name in ("created_at", "started_at", "completed_at")
        )
        queue_end = started if started is not None else (None if status == "completed" else now)
        job = {
            "name": _clean(row["check_name"], 120),
            "status": status,
            "conclusion": conclusion,
            **_ci_log_ids(row),
            "runner_kind": _enum(row["runner_kind"], CI_RUNNER_KINDS) if row["runner_kind"] is not None else None,
            "runner_name": _clean(row["runner_name"], 64) if row["runner_name"] is not None else None,
            "created_at": created,
            "started_at": started,
            "completed_at": completed,
            "elapsed_seconds": (
                _age(completed if completed is not None and status == "completed" else now, started)
                if started is not None else None
            ),
            "queue_seconds": _age(queue_end, created) if created is not None and queue_end is not None else None,
        }
        grouped.setdefault((*key, head), []).append(job)
        refs.setdefault((*key, head), ref)
        if (
            status == "completed" and conclusion in CHECK_FAILED
            and completed is not None and completed >= now - CI_FAILURE_WINDOW
        ):
            tests, more = _ci_tests(row["failing_tests"])
            failures.append({
                **ref,
                "name": job["name"],
                "conclusion": conclusion,
                "run_id": job["run_id"],
                "job_id": job["job_id"],
                "tests": tests,
                "more_tests": more,
                "at": completed,
                "run": (*key, head),
            })
    runs = []
    for run_key, jobs in grouped.items():
        status = _ci_run_status(jobs)
        if status not in ("queued", "in_progress"):
            continue
        began = min((job["created_at"] for job in jobs if job["created_at"] is not None), default=None)
        runs.append({**refs[run_key], "status": status, "age_seconds": _age(now, began), "jobs": jobs})
    runs.sort(key=lambda run: (run["age_seconds"] is None, run["age_seconds"] or 0))
    failures.sort(key=lambda item: -item["at"])
    return {
        "runs": runs,
        "failures": [
            {**{k: v for k, v in item.items() if k not in ("at", "run")}, "age_seconds": _age(now, item["at"]),
             "run_checks": [{"status": job["status"], "conclusion": job["conclusion"]} for job in grouped[item["run"]]]}
            for item in failures[:RECENT_CI_FAILURES]
        ],
    }


QA_RUN_STATES = ("queued", "running", "reported")
QA_SCOPES = ("full", "targeted", "smoke")
QA_ARMS = ("primary", "shootout")
QA_RESULTS = ("PASS", "FAIL", "BLOCKED")
_QA_RUN_ID = re.compile(r"[0-9a-f]{32}")


def _json(text: object) -> object:
    try:
        return json.loads(str(text)) if text is not None else None
    except ValueError:
        return None


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


def _qa_scenarios(lanes: list[sqlite3.Row]) -> tuple[int, int]:
    newest: dict[int, sqlite3.Row] = {}
    for lane in sorted(lanes, key=lambda row: (row["slice"], row["attempt"])):
        newest[lane["slice"]] = lane
    passed = total = 0
    for lane in newest.values():
        asked = [item for item in _list(_json(lane["scenarios"])) if isinstance(item, str)]
        result = _json(lane["result"])
        verified = {
            qa_id(item["id"]) for item in _list(result.get("scenarios") if isinstance(result, dict) else None)
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item.get("result") == "verified"
        }
        total += len(asked)
        passed += sum(item in verified for item in asked)
    return passed, total


def _qa(
    connection: sqlite3.Connection, now: float, tables: set[str], tracked: dict[tuple[str, str], TicketInfo]
) -> dict:
    newest: dict[tuple[str, str], object] = {}
    heads: dict[tuple[str, str], list[sqlite3.Row]] = {}
    if "qa_runs" in tables:
        for row in connection.execute("SELECT rowid, * FROM qa_runs ORDER BY rowid DESC"):
            key = (str(row["repo"]).casefold(), str(row["ticket_id"]))
            if key not in tracked or newest.setdefault(key, row["head"]) != row["head"]:
                continue
            if row["scope"] != "none" and row["state"] not in ("cancelled", "stale"):
                heads.setdefault(key, []).append(row)
    lanes = "qa_lanes" in tables
    runs = []
    for key in sorted(heads):
        for row in sorted(heads[key], key=lambda row: (row["arm"] != "primary", row["rowid"])):
            found = connection.execute(
                "SELECT slice, kind, attempt, scenarios, result FROM qa_lanes "
                "WHERE run_id = ? AND kind != 'base-check'", (row["run_id"],)
            ).fetchall() if lanes else []
            state = _enum(row["state"], QA_RUN_STATES)
            passed, total = (
                _qa_scenarios([lane for lane in found if lane["kind"] == "head"]) if state == "reported" else (None, None)
            )
            run_id = str(row["run_id"])
            reported = state == "reported" and row["finished_at"] is not None
            runs.append({
                "repo": _clean(row["repo"], 100),
                "ticket_id": _clean(row["ticket_id"], 64),
                "runner": _enum(row["runner"], QA_RUNNERS),
                "arm": _enum(row["arm"], QA_ARMS),
                "state": state,
                "scope": _enum(row["scope"], QA_SCOPES),
                "result": _enum(row["result"], QA_RESULTS) if row["result"] is not None else None,
                "lanes": len({lane["slice"] for lane in found}),
                "passed": passed,
                "total": total,
                "elapsed_seconds": _age(epoch.read(row["finished_at"]) if reported else now, row["started_at"]),
                "run": [run_id[:16], run_id[16:]] if _QA_RUN_ID.fullmatch(run_id) else None,
            })
    open_lanes = dict(connection.execute(
        "SELECT runner, COUNT(*) FROM qa_lanes WHERE state IN ('launching', 'running') GROUP BY runner"
    ).fetchall()) if lanes else {}
    runners = [{"runner": name, "open": int(open_lanes.get(name, 0))} for name in QA_RUNNERS]
    queued = [
        {"repo": _clean(repo, 100), "count": int(count)}
        for repo, count in connection.execute(
            "SELECT repo, COUNT(*) FROM jobs WHERE action = 'qa' AND state = 'queued' GROUP BY repo ORDER BY repo"
        )
    ] if "jobs" in tables else []
    return {"runs": runs, "runners": runners, "queued": queued}


def _qa_run_parts(run_id: str) -> list[str] | None:
    if _QA_RUN_ID.fullmatch(run_id):
        return [run_id[:16], run_id[16:]]
    if re.fullmatch(r"[A-Za-z0-9._-]{1,31}", run_id):
        return [run_id]
    return None


def _chunk_qa_path(encoded_rel: str) -> list[str] | None:
    if not encoded_rel or ".." in encoded_rel or "//" in encoded_rel:
        return None
    for form in (encoded_rel, unquote(encoded_rel)):
        cleaned = _strip_control(form)
        decoded, _spans, settled = _decode_map(cleaned)
        if not settled:
            return None
        for candidate in (cleaned, decoded):
            if _apply_rules(candidate, _NON_OPAQUE_RULES)[2]:
                return None
            if any(redact(seg) != seg for seg in candidate.split("/")):
                return None
            segments = candidate.replace("/", " ")
            if redact(segments) != segments:
                return None
    return [encoded_rel[i : i + 16] for i in range(0, len(encoded_rel), 16)]


def _held_qa_run(
    connection: sqlite3.Connection, key: tuple[str, str], head: str
) -> sqlite3.Row | None:
    select = """
        SELECT run_id, scope, result, reason, evidence_dir FROM qa_runs
        WHERE (
            state = 'reported'
            OR (state = 'cancelled' AND (reason = 'rerun-qa' OR reason LIKE 'rerun-qa:carried:%'))
        ) AND
    """
    row = connection.execute(
        select + "lower(repo) = ? AND ticket_id = ? AND head = ? AND arm = 'primary' "
        "ORDER BY rowid DESC LIMIT 1",
        (*key, head),
    ).fetchone()
    seen: set[str] = set()
    while row is not None and row["scope"] == "none":
        raw_reason = str(row["reason"] or "")
        carried = raw_reason.removeprefix("rerun-qa:")
        source = carried.removeprefix("carried:")
        if source == carried or not source or source in seen:
            return None
        seen.add(source)
        row = connection.execute(select + "run_id = ?", (source,)).fetchone()
    return row if row is not None and row["evidence_dir"] is not None else None


def _visual_qa_for_holds(
    connection: sqlite3.Connection,
    tables: set[str],
    ticket_rows: list[tuple[dict, tuple[str, str]]],
    wait_facts: dict[tuple[str, str], dict[str, str | None]],
) -> None:
    """Attach redact-proof inline visual QA media metadata onto `visual-qa-review` held tickets."""
    if "qa_runs" not in tables:
        return
    cols = {str(row["name"]) for row in connection.execute("PRAGMA table_info(qa_runs)")}
    if "evidence_dir" not in cols:
        return
    holds = (
        {
            (str(row["repo"]).casefold(), str(row["ticket_id"])): str(row["head"])
            for row in connection.execute("SELECT repo, ticket_id, head FROM ship_holds")
        }
        if "ship_holds" in tables
        else {}
    )
    for item, key in ticket_rows:
        if item.get("state") != "awaiting-ok" or key not in holds:
            continue
        hold_reasons = (wait_facts.get(key) or {}).get("hold_reasons") or ""
        if "visual-qa-review" not in {p.strip() for p in hold_reasons.split(",") if p.strip()}:
            continue
        row = _held_qa_run(connection, key, holds[key])
        if row is None:
            continue
        run_id = str(row["run_id"])
        run_parts = _qa_run_parts(run_id)
        if run_parts is None:
            continue
        media_out: list[dict[str, object]] = []
        try:
            raw_media = qa_evidence.visual_media(Path(str(row["evidence_dir"])), run_id)
        except OSError:
            raw_media = []
        for entry in raw_media:
            url = str(entry.get("url") or "")
            lane = entry.get("lane")
            marker = f"/lane-{lane}/"
            if not isinstance(lane, int) or lane < 1 or marker not in url:
                continue
            encoded_rel = url.split(marker, 1)[1]
            path_parts = _chunk_qa_path(encoded_rel)
            if path_parts is None:
                continue
            media_out.append({
                "kind": "video" if entry.get("kind") == "video" else "image",
                "lane": lane,
                "path_parts": path_parts,
                "scenario": _clean(entry["scenario"], 64) if entry.get("scenario") else None,
                "result": _clean(entry["result"], 32) if entry.get("result") else None,
            })
        item["visual_qa"] = {
            "run": run_parts,
            "result": _enum(row["result"], QA_RESULTS) if row["result"] is not None else None,
            "media": media_out,
        }


def _open_questions_for_tickets(
    connection: sqlite3.Connection,
    tables: set[str],
    ticket_rows: list[tuple[dict, tuple[str, str]]],
) -> None:
    """Attach redacted open product questions onto `waiting-max` tickets (E4-1)."""
    if "ticket_state" not in tables:
        return
    cols = {str(row["name"]) for row in connection.execute("PRAGMA table_info(ticket_state)")}
    if "paused_question_text" not in cols:
        return
    texts: dict[tuple[str, str], str] = {}
    for row in connection.execute(
        """
        SELECT repo, ticket_id, paused_question_text FROM ticket_state
        WHERE workflow_state = 'waiting-max' AND paused_question_text IS NOT NULL
        """
    ):
        texts[(str(row["repo"]).casefold(), str(row["ticket_id"]))] = str(row["paused_question_text"])
    for item, key in ticket_rows:
        if item.get("state") != "waiting-max":
            continue
        raw = texts.get(key)
        if not raw:
            continue
        parsed = extract_open_questions(raw)
        if not parsed:
            continue
        cleaned = [
            {
                "text": _clean(entry["text"], 240),
                "options": [_clean(opt, 160) for opt in (entry.get("options") or ())[:8]],
            }
            for entry in parsed
            if entry.get("text")
        ]
        if cleaned:
            item["questions"] = cleaned


def _ticket_ref(
    sources: dict[tuple[str, str], tuple[str | None, str | None]],
    repo: object,
    ticket_id: object,
    repo_limit: int = 100,
) -> dict:
    """Issue #115: a row's shown repo and ticket id, with its source issue and PR
    found by the stored identifiers before `_clean` can cut them short."""
    ref = {"repo": _clean(repo, repo_limit), "ticket_id": _clean(ticket_id, 64)}
    found = sources.get((str(repo).casefold(), str(ticket_id)))
    if found is not None:
        ref["source"] = found[0] if found[0] is not None and SOURCE.fullmatch(found[0]) else None
        ref["pr"] = _clean(found[1], 16) if found[1] is not None else None
    return ref


def _feeder_titles(connection: sqlite3.Connection, tables: set[str]) -> dict[tuple[str, str], str]:
    if FEEDER_TABLE not in tables:
        return {}
    return {
        (str(row["repo"]).casefold(), str(row["issue"])): _clean(row["title"], 120)
        for row in connection.execute(f"SELECT repo, issue, title FROM {FEEDER_TABLE}")
    }


def _acting(row: dict) -> dict:
    return {key: row[key] for key in ("state", "wait", "actions") if key in row}


_COMMENT_ID = re.compile(r"[0-9]{1,20}")


def _comment_id(value: str | None) -> str | None:
    return value if value is not None and _COMMENT_ID.fullmatch(value) else None


def _same_ref(row: dict) -> dict:
    return {key: row[key] for key in ("repo", "ticket_id", "source", "pr") if key in row}


def _projects(
    orch: OrchConfig | None, rules: dict[str, sqlite3.Row], now: float
) -> list[dict]:
    """Each orch.yml project's tracker, ship mode and round caps, in orch.yml
    order, as the poller last read its factory.md (issue #152). A project
    whose factory.md was unreadable, or not read yet, is `unknown` with the
    reason and no values."""
    if orch is None:
        return []
    projects: list[dict] = []
    for project in orch.projects:
        row = rules.get(project.repo.lower())
        if row is None or row["reason"] is not None:
            projects.append({
                "repo": project.repo,
                "status": "unknown",
                "tracker": "unknown",
                "ship": "unknown",
                "approver": None,
                "rounds": None,
                "reason": _clean(row["reason"]) if row else "not read by the poller yet",
                "read_age_seconds": _age(now, row["read_at"]) if row else None,
            })
            continue
        ship, _, approver = str(row["ship"]).partition(":")
        projects.append({
            "repo": project.repo,
            "status": "ok",
            "tracker": _enum(row["tickets"], TICKETS),
            "ship": _enum(ship, ("merge", "handoff", "after")),
            "approver": _clean(approver) if approver else None,
            "rounds": {"low": int(row["rounds_low"]), "high": int(row["rounds_high"])},
            "read_age_seconds": _age(now, row["read_at"]),
        })
    return projects


def _attention(
    stuck: list[dict],
    overdue: list[dict],
    paused: list[dict],
    flags: Sequence[dict] = (),
    health: component_health.HealthReport | None = None,
    tickets: Sequence[dict] = (),
) -> dict:
    """Everything that needs Max, each ticket or job once: stuck tickets,
    overdue jobs, tickets flagged needs-attention (a ship that failed and
    will not be retried, a job dedupe dropped: #83, #84), paused tickets
    that are not already listed, then (#92) down components, an unreadable
    orch.health.db and harnesses whose queued jobs have no live waiter, each
    linked to the Health tab. `count` heads the attention strip on every page."""
    acting = {(row["repo"], row["ticket_id"]): _acting(row) for row in tickets}

    items: list[dict] = []
    for row in stuck:
        detail = row["state"] + (f": {row['reason']}" if row["reason"] else "")
        items.append({"kind": "stuck", **_same_ref(row), "detail": detail,
                      **acting.get((row["repo"], row["ticket_id"]), {})})
    for row in overdue:
        job_state = row.get("job_state")
        if job_state == DEAD:
            kind, what = "dead", "runner gone"
        else:
            kind = "overdue" if row.get("timed_out", True) else "stale"
            what = "runner gone, worker alive" if row.get("stale_cause") == RUNNER_GONE else row["state"]
        items.append({
            "kind": kind,
            **_same_ref(row),
            "detail": f"{row['recipient']} {row['action']} {what}, "
                      f"no heartbeat for {_duration(row['age_seconds'])}",
        })
    for row in flags:
        detail = row["reason"] + (f": {row['detail']}" if row["detail"] else "")
        items.append({"kind": "needs-attention", **_same_ref(row), "detail": detail,
                      **acting.get((row["repo"], row["ticket_id"]), {})})
    listed = {(row["repo"], row["ticket_id"]) for row in stuck}
    listed |= {(row["repo"], row["ticket_id"]) for row in flags}
    for row in paused:
        if (row["repo"], row["ticket_id"]) in listed:
            continue
        detail = row["state"] + (f" ({row['paused_action']})" if row["paused_action"] else "")
        if row.get("waiting_on"):
            detail += f" (waiting on {row['waiting_on']})"
        items.append({"kind": "paused", **_same_ref(row), "detail": detail,
                      **acting.get((row["repo"], row["ticket_id"]), {})})
    for component in health.down if health is not None else ():
        reason = f" ({component.reason})" if component.reason else ""
        items.append({"kind": "down", "repo": "", "ticket_id": "",
                      "component": component.label,
                      "link": f"health.html#{component.anchor}",
                      "detail": f"down{reason}"})
    if health is not None and health.error is not None:
        items.append({"kind": "health", "repo": "", "ticket_id": "",
                      "component": "health storage", "link": "health.html#components",
                      "detail": f"unknown: {health.error}"})
    for flag in health.no_waiter if health is not None else ():
        count = len(flag.jobs)
        items.append({"kind": "no-waiter", "repo": "", "ticket_id": "",
                      "component": f"waiter {flag.bot}",
                      "link": f"health.html#{flag.anchor}",
                      "detail": f"no waiter for {flag.harness} "
                                f"({count} queued job{'' if count == 1 else 's'})"})
    return {"count": len(items), "items": items}


def _poll_health(
    cycle_age: int | None, activity_age: int | None, poll_seconds: int
) -> dict:
    ok_seconds, late_seconds = health_thresholds(poll_seconds)
    if cycle_age is None:
        state = "unknown"
    elif cycle_age <= ok_seconds:
        state = "ok"
    elif cycle_age <= late_seconds:
        state = "late"
    else:
        state = "stale"
    return {
        "state": state,
        "cycle_seconds": poll_seconds,
        "last_cycle_age_seconds": cycle_age,
        "last_activity_age_seconds": activity_age,
    }


def _verdict_kind(outcome: str) -> str:
    """Group a handled comment's outcome for the feed."""
    if outcome.startswith(("queued-", "retried-")):
        return "routed"
    if outcome.startswith("stale-"):
        return "stale"
    if outcome.startswith(("rejected-", "refused-")):
        return "rejected"
    if outcome.startswith(("stuck-", "capped-")):
        return "stuck"
    if outcome in ("merged", "merge-confirmed", "handed-off"):
        return "shipped"
    if outcome.startswith("ship-") or outcome == "approve-ci-red":
        return "blocked"
    if outcome.startswith(("waiting-", "already-waiting")) or outcome == "no-questions":
        return "waiting"
    return "other"


def _handoff_kind(recipient: str, state: str, has_handoff: bool) -> str:
    if state == "failed":
        return "failed"
    if recipient == "grok":
        return "bot_to_bot"
    if has_handoff and recipient in WAIT_RECIPIENTS:
        return "scripted"
    return "done"


_VERDICT_WORDS = {
    "queued-review": ("routed", "sent to review"),
    "queued-plan": ("routed", "sent to plan"),
    "queued-plan-build": ("routed", "plan accepted, sent to build"),
    "merge-queue": ("routed", "approved, queued to merge"),
    "resumed-fix": ("routed", "resumed the fix"),
    "handed-off": ("shipped", "handed off"),
    "merged": ("shipped", "merged"),
    "waiting-max-repeat": ("waiting", "still waiting on Max"),
    "approve-ci-red": ("blocked", "approved, CI red"),
}
_UNSTUCK_PREFIX = "authorized-unstick-"
_UNSTUCK_WORDS = ("unstuck", "Max unstuck it")


def _verdict_words(outcome: str) -> tuple[str | None, str | None]:
    if outcome.startswith(_UNSTUCK_PREFIX):
        return _UNSTUCK_WORDS
    return _VERDICT_WORDS.get(outcome, (None, None))


_HANDOFF_COLUMNS = (
    "id, repo, ticket_id, action, recipient, state, finished_at, "
    "handoff_comment_id IS NOT NULL AS has_handoff"
)
_HANDOFF_DONE = "state IN ('done', 'failed') AND finished_at IS NOT NULL"


def _handoff_row(sources: dict, row: sqlite3.Row, now: float) -> dict:
    recipient = _enum(row["recipient"], RECIPIENTS)
    state = _enum(row["state"], STATES)
    return {
        "job": _clean(str(row["id"])[:8], 8),
        **_ticket_ref(sources, row["repo"], row["ticket_id"]),
        "action": _enum(row["action"], ACTIONS),
        "recipient": recipient,
        "state": state,
        "kind": _handoff_kind(recipient, state, bool(row["has_handoff"])),
        "age_seconds": _age(now, row["finished_at"]),
    }


def _handled(connection: sqlite3.Connection, tables: set[str]) -> list[sqlite3.Row]:
    """Every handled comment, newest first, with the first time recorded for it
    (_VERDICT_TIMES) or NULL. Joins on grouped tables, not a subquery per
    comment: lower(repo) leaves the subqueries no index (seconds on a live
    database)."""
    epoch.register(connection)
    joins, times = [], []
    for index, (table, when, column, comment) in enumerate(_VERDICT_TIMES):
        if table in tables and {column, comment} <= {
            str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")
        }:
            joins.append(
                f"LEFT JOIN (SELECT lower(repo) AS repo, ticket_id, {comment} AS comment, {when}({epoch.sql(column)}) AS at "
                f"FROM {table} GROUP BY lower(repo), ticket_id, {comment}) AS t{index} "
                f"ON t{index}.repo = lower(h.repo) AND t{index}.ticket_id = h.ticket_id "
                f"AND t{index}.comment = h.comment_id"
            )
            times.append(f"t{index}.at")
    recorded = f"COALESCE({', '.join(times)}, NULL)" if times else "NULL"
    return connection.execute(
        f"SELECT h.rowid AS seq, h.repo, h.ticket_id, h.outcome, {recorded} AS recorded_at "
        f"FROM handled_comments AS h {' '.join(joins)} ORDER BY h.rowid DESC"
    ).fetchall()


def _verdict_row(sources: dict, row: sqlite3.Row, now: float) -> dict:
    outcome = str(row["outcome"])
    if _OUTCOME.fullmatch(outcome) is None:
        outcome = "other"
    return {
        **_ticket_ref(sources, row["repo"], row["ticket_id"]),
        "outcome": _clean(outcome, 48),
        "kind": _verdict_kind(outcome),
        "seq": int(row["seq"]),
        "age_seconds": _age(now, row["recorded_at"]),
    }


def _resting(record: dict | None, waiting_on: str | None, now: float) -> dict | None:
    if record is None:
        return None
    if record["stopped"]:
        stopped = record["stopped"]
        return {"kind": "stopped", "state": stopped["state"], "stopped_in": stopped["stopped_in"],
                "age_seconds": _age(now, stopped["at"])}
    if record["shipped"]:
        shipped = record["shipped"]
        return {"kind": "shipped", "how": shipped["how"], "age_seconds": _age(now, shipped["at"])}
    return {"kind": "idle", "state": record["state"], "age_seconds": record["state_age_seconds"],
            "waiting_on": waiting_on}


def _relay(
    connection: sqlite3.Connection,
    tables: set[str],
    sources: dict,
    handled: list[sqlite3.Row],
    records: dict[tuple[str, str], dict],
    waiting_on: dict[tuple[str, str], str],
    tracked: int,
    now: float,
) -> dict:
    epoch.register(connection)
    seen: list[Iterable] = []
    if "tracked_tickets" in tables:
        seen.append(connection.execute("SELECT repo, ticket_id, NULL AS at FROM tracked_tickets"))
    if "jobs" in tables:
        seen.append(connection.execute(
            f"SELECT repo, ticket_id, MAX({epoch.sql('COALESCE(finished_at, last_activity_at, created_at)')}) AS at "
            "FROM jobs GROUP BY repo, ticket_id"))
    if "ticket_transitions" in tables:
        seen.append(connection.execute(
            f"SELECT repo, ticket_id, MAX({epoch.sql('at')}) FROM ticket_transitions GROUP BY repo, ticket_id"))
    seen.append((row["repo"], row["ticket_id"], row["recorded_at"]) for row in handled)
    stored: dict[tuple[str, str], tuple[str, str]] = {}
    moved: dict[tuple[str, str], float] = {}
    for rows in seen:
        for repo, ticket_id, at in rows:
            key = (str(repo).casefold(), str(ticket_id))
            stored.setdefault(key, (str(repo), str(ticket_id)))
            if at is not None:
                at = epoch.read(at)
                moved[key] = max(moved.get(key, at), at)

    def newest(key: tuple[str, str]) -> tuple:
        return key not in moved, -moved.get(key, 0.0), key

    def stopped_at(key: tuple[str, str]) -> tuple:
        at = records[key]["stopped"]["at"]
        return at is None, -(at or 0.0), key

    stopped = sorted((key for key in stored if key in records and records[key]["stopped"]), key=stopped_at)
    stopped = stopped[:RELAY_TICKETS]
    movers = sorted(stored.keys() - set(stopped), key=newest)
    chosen = sorted([*stopped, *movers[: RELAY_TICKETS - len(stopped)]], key=newest)
    if not chosen:
        return {"tracked": tracked, "threads": []}
    threads = {
        key: {**_ticket_ref(sources, *stored[key]), "handoffs": [], "verdicts": [],
              "now": _resting(records.get(key), waiting_on.get(key), now)}
        for key in chosen
    }
    if "jobs" in tables:
        wanted = ", ".join("(lower(?), ?)" for _ in chosen)
        for row in connection.execute(
            f"SELECT * FROM (SELECT {_HANDOFF_COLUMNS}, ROW_NUMBER() OVER ("
            f"PARTITION BY lower(repo), ticket_id ORDER BY {epoch.sql('finished_at')} DESC, id DESC) AS n "
            f"FROM jobs WHERE {_HANDOFF_DONE} AND (lower(repo), ticket_id) IN (VALUES {wanted})) "
            f"WHERE n <= ? ORDER BY {epoch.sql('finished_at')} DESC, id DESC",
            (*(name for key in chosen for name in stored[key]), RELAY_STEPS),
        ):
            threads[(str(row["repo"]).casefold(), str(row["ticket_id"]))]["handoffs"].append(
                _handoff_row(sources, row, now))
    for row in handled:
        thread = threads.get((str(row["repo"]).casefold(), str(row["ticket_id"])))
        if thread is not None and len(thread["verdicts"]) < RELAY_STEPS:
            verdict = _verdict_row(sources, row, now)
            chip, words = _verdict_words(verdict["outcome"])
            thread["verdicts"].append({**verdict, "chip": chip, "words": words})
    return {"tracked": tracked, "threads": list(threads.values())}


_DIGEST = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _digest(value: str) -> list[str]:
    if not _DIGEST.fullmatch(value):
        return [_clean(value)]
    return [value[i : i + 16] for i in range(0, len(value), 16)]


def _qa_rounds(connection: sqlite3.Connection, tables: set[str]) -> dict[str, int]:
    if "qa_runs" not in tables:
        return {}
    return {
        str(job_id): int(number)
        for job_id, number in connection.execute("SELECT job_id, round FROM qa_runs WHERE job_id IS NOT NULL")
    }


def _review_round(found: str | None, qa_round: int | None) -> str:
    if found is not None:
        return found.split("/")[0]
    return exposure.UNKNOWN if qa_round is None else str(qa_round)


def _exposure(found: exposure.Exposure | None, review_round: str) -> dict:
    if found is None:
        return {"round": review_round} | dict.fromkeys(
            ("model", "effort", "harness", "cli_version", "brief_sha256", "skill_paths", "host", "factory_revision"),
            exposure.UNKNOWN,
        )
    return {
        "round": review_round,
        "model": _clean(found.model),
        "effort": _clean(found.effort),
        "harness": _clean(found.harness),
        "cli_version": _clean(found.cli_version),
        "brief_sha256": _digest(found.brief_sha256),
        "skill_paths": exposure.UNKNOWN if found.skill_paths is None else [_clean(p) for p in found.skill_paths],
        "host": _clean(found.host),
        "factory_revision": _digest(found.factory_revision),
    }


def _round(value: object) -> str | None:
    return value if isinstance(value, str) and _ROUND.fullmatch(value) else None


def _read_db(
    connection: sqlite3.Connection,
    now: float,
    chart: dict | None = None,
    job_states: JobStateConfig = JobStateConfig(),
    alive: Alive | None = pid_alive,
    host: str = DEFAULT_ORCH_HOST,
) -> dict:
    epoch.register(connection)
    tables = _tables(connection)
    jobs = "jobs" in tables
    exposures = exposure.read_all(connection)
    qa_rounds = _qa_rounds(connection, tables)
    sources = read_sources(connection)
    if "tracked_tickets" in tables:
        for repo, ticket_id in connection.execute(
            "SELECT repo, ticket_id FROM tracked_tickets WHERE tracker = 'github'"
        ):
            sources.setdefault((str(repo).casefold(), str(ticket_id)), (None, str(ticket_id)))

    totals = {state: 0 for state in STATES}
    groups: dict[tuple[str, str], dict] = {}
    if jobs:
        for row in connection.execute(
            "SELECT recipient, action, state, count(*) AS n FROM jobs "
            "GROUP BY recipient, action, state"
        ):
            recipient = _enum(row["recipient"], RECIPIENTS)
            action = _enum(row["action"], ACTIONS)
            state = _enum(row["state"], STATES)
            if state == "other":
                continue
            totals[state] += int(row["n"])
            entry = groups.setdefault(
                (recipient, action),
                {"recipient": recipient, "action": action, **{s: 0 for s in STATES}},
            )
            entry[state] += int(row["n"])

    held: dict[str, int] = {}
    placed: set[str] = set()
    placements: dict[str, tuple[str, float | None]] = {}
    if "device_slots" in tables:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(device_slots)")
        }
        placed_at = "placed_at" if "placed_at" in columns else "NULL"
        for row in connection.execute(
            f"SELECT job_id, device, {placed_at} AS placed_at FROM device_slots"
        ):
            placed.add(str(row["job_id"]))
            held[str(row["device"])] = held.get(str(row["device"]), 0) + 1
            placements[str(row["job_id"])] = (
                str(row["device"]),
                epoch.read(row["placed_at"]) if row["placed_at"] is not None else None,
            )

    slots = read_placements(connection) if "device_slots" in tables else {}
    running: list[dict] = []
    queued: list[dict] = []
    overdue: list[dict] = []
    # Each running or queued job's ticket as stored, to find its history.
    active: list[tuple[dict, str, str]] = []
    unplaced_running = 0
    marks = priority_marks(connection) if jobs else {}
    running_on: dict[str, int] = {}  # running jobs per device they are placed on
    if jobs:
        for row in connection.execute(
            f"""
            SELECT id, repo, ticket_id, action, recipient, state,
                   timeout_seconds, created_at, last_activity_at, {_DETAIL_ROUND} AS round
            FROM jobs WHERE state IN ('queued', 'running')
            ORDER BY {epoch.sql('created_at')}, id
            """
        ):
            base = {
                "job": _clean(str(row["id"])[:8], 8),
                **_ticket_ref(sources, row["repo"], row["ticket_id"]),
                "action": _enum(row["action"], ACTIONS),
                "recipient": _enum(row["recipient"], RECIPIENTS),
            }
            timeout = int(row["timeout_seconds"])
            job_state: str | None = None
            cause: str | None = None
            if row["state"] == "running":
                age = _age(now, row["last_activity_at"]) or 0
                # The lane: a device-pool placement, else a box slot (grok
                # wakes hold none). jobs has no claim time, so an unplaced
                # job's elapsed time counts from when it was queued.
                placement = placements.get(str(row["id"]))
                if placement is not None:
                    device: str | None = _clean(placement[0], 40)
                    started = placement[1]
                    elapsed_from = "placed"
                else:
                    device = None if row["recipient"] == "grok" else _clean(host, 40)
                    started = None
                    elapsed_from = "queued"
                if started is None:
                    started = row["created_at"]
                    elapsed_from = "queued"
                slot = slots.get(str(row["id"]))
                facts = liveness(slot, alive)
                job_state = _enum(
                    derive(
                        last_activity_at=epoch.read(row["last_activity_at"]),
                        now=now,
                        placement=slot,
                        stale_seconds=job_states.stale_seconds,
                        liveness=facts,
                    ),
                    JOB_STATES,
                )
                cause = stale_cause(job_state, facts)
                item = {
                    **base,
                    "job_state": job_state,
                    "stale_cause": cause,
                    "round": _round(row["round"]),
                    "heartbeat_age_seconds": age,
                    "heartbeat_at": epoch.read(row["last_activity_at"]),
                    "timeout_seconds": timeout,
                    "overdue": age > timeout,
                    "device": device,
                    "elapsed_seconds": _age(now, started) or 0,
                    "elapsed_from": elapsed_from,
                    "exposure": _exposure(
                        exposures.get(str(row["id"])), _review_round(_round(row["round"]), qa_rounds.get(str(row["id"])))
                    ),
                }
                running.append(item)
                active.append((item, str(row["repo"]), str(row["ticket_id"])))
                if row["recipient"] != "grok" and str(row["id"]) not in placed:
                    unplaced_running += 1
                elif placement is not None:
                    running_on[placement[0]] = running_on.get(placement[0], 0) + 1
            else:
                # The poller's queue-wait clock: since it was queued, or
                # since a claim that never started was released (#77 F1.2).
                age = _age(now, row["last_activity_at"]) or 0
                mark = marks.get((str(row["repo"]).lower(), str(row["ticket_id"])), PriorityMark())
                item = {**base, "round": _round(row["round"]), "age_seconds": age,
                        "tier": mark.tier, "own_tier": mark.own_tier}
                queued.append(item)
                active.append((item, str(row["repo"]), str(row["ticket_id"])))
            # Stuck / overdue: past the timeout, or (#75) a running job whose
            # heartbeat went stale or whose runner is dead.
            if age > timeout or job_state in (STALE, DEAD):
                overdue.append({
                    **base,
                    "state": _enum(row["state"], STATES),
                    "job_state": job_state,
                    "stale_cause": cause,
                    "timed_out": age > timeout,
                    "age_seconds": age,
                })
        running.sort(key=lambda item: -item["heartbeat_age_seconds"])

    tickets: list[dict] = []
    ticket_rows: list[tuple[dict, tuple[str, str]]] = []
    stuck_tickets: list[dict] = []
    paused: list[dict] = []
    ticket_info: dict[tuple[str, str], TicketInfo] = {}
    waiting_on: dict[tuple[str, str], str] = {}
    if "tracked_tickets" in tables and "ticket_state" in tables:
        approvers = _hold_approvers(connection)
        wait_facts = max_actions.wait_facts(connection)
        ci_waits = _ci_waits(connection, now) if "ci_waits" in tables else {}
        no_ci_bases = _no_ci_bases(connection, tables) if "no_ci_bases" in tables else {}
        state_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(ticket_state)")}
        stuck_at = ", ".join([
            *(["state.stuck_since"] if "stuck_since" in state_columns else []),
            *(["wake.created_at"] if jobs else []),
            "NULL",
        ])
        external_at = "state.external_at" if "external_at" in state_columns else "NULL"
        closed_at = "state.closed_at" if "closed_at" in state_columns else "NULL"
        linear = "linear_states" in tables
        titles = _feeder_titles(connection, tables)
        for row in connection.execute(
            f"""
            SELECT tracked.repo, tracked.ticket_id, tracked.tracker,
                   COALESCE(state.workflow_state, 'intake') AS workflow_state,
                   COALESCE(state.review_round, 0) AS review_round,
                   COALESCE(state.gate_count, 0) AS gate_count,
                   COALESCE(state.stuck, 0) AS stuck,
                   state.stuck_reason, state.paused_action,
                   COALESCE({stuck_at}, NULL) AS stuck_at,
                   {external_at} AS external_at,
                   {closed_at} AS closed_at,
                   {"linear.state_name" if linear else "NULL"} AS linear_state
            FROM tracked_tickets AS tracked
            LEFT JOIN ticket_state AS state
              ON state.repo = tracked.repo AND state.ticket_id = tracked.ticket_id
            {"LEFT JOIN jobs AS wake ON wake.id = state.stuck_wake_job_id" if jobs else ""}
            {'''LEFT JOIN linear_states AS linear
              ON linear.repo = lower(tracked.repo) AND linear.ticket_id = tracked.ticket_id'''
             if linear else ""}
            """
        ):
            ref = _ticket_ref(sources, row["repo"], row["ticket_id"])
            state = _enum(row["workflow_state"], WORKFLOW_STATES)
            paused_action = (
                _enum(row["paused_action"], ACTIONS)
                if row["paused_action"] is not None
                else None
            )
            stuck = bool(row["stuck"])
            # Issue #86: who a held PR waits on, only while awaiting-ok.
            approver = (
                approvers.get((str(row["repo"]), str(row["ticket_id"])))
                if str(row["workflow_state"]) == "awaiting-ok"
                else None
            )
            waiting = {"waiting_on": _clean(approver, 39)} if approver else {}
            key = (str(row["repo"]).casefold(), str(row["ticket_id"]))
            if approver:
                waiting_on[key] = waiting["waiting_on"]
            ci_wait = ci_waits.get(key)
            no_ci = no_ci_bases.get(key)
            reason = (
                _clean(row["stuck_reason"], 200) if row["stuck_reason"] is not None else None
            )
            facts = wait_facts.get(key, {})
            wait = max_actions.wait_of(
                str(row["workflow_state"]), stuck,
                stuck_reason=reason,
                stuck_comment_id=_comment_id(facts.get("stuck_comment_id")),
                question_id=_comment_id(facts.get("question_id")),
                parked_outcome=facts.get("parked"),
                approve_comment_id=_comment_id(facts.get("approve_comment_id")),
                hold_reasons=facts.get("hold_reasons"),
                stop_comment_id=_comment_id(facts.get("stop_comment_id")),
            )
            acting = {
                "wait": wait.as_dict() if wait else None,
                "actions": max_actions.applicable(wait, hand_off=max_actions.may_hand_off(
                    str(row["workflow_state"]), stuck, row["paused_action"],
                    facts.get("running") is not None,
                )),
            }
            ticket_info[key] = TicketInfo(
                _enum(row["tracker"], TRACKERS), stuck, state,
                ref["repo"], ref["ticket_id"], int(row["review_round"]), reason,
                f"{redact(str(row['repo']))}#{redact(str(row['ticket_id']))}",
                None if row["stuck_at"] is None else epoch.read(row["stuck_at"]),
                epoch.cell(row["closed_at"]),
            )
            issue = ref["source"].removeprefix("#") if ref.get("source") else str(row["ticket_id"])
            tickets.append(
                {
                    **ref,
                    "title": titles.get((str(row["repo"]).casefold(), issue)),
                    "tracker": _enum(row["tracker"], TRACKERS),
                    "state": state,
                    "round": int(row["review_round"]),
                    "gate_count": int(row["gate_count"]),
                    "stuck": stuck,
                    "paused_action": paused_action,
                    **waiting,
                    **({"ci_wait": ci_wait} if ci_wait else {}),
                    **({"no_ci": no_ci} if no_ci else {}),
                    **({"external_at": epoch.read(row["external_at"])} if row["external_at"] is not None else {}),
                    **({"linear_state": _clean(row["linear_state"], 64)} if row["linear_state"] else {}),
                    **acting,
                }
            )
            ticket_rows.append((tickets[-1], key))
            if stuck:
                stuck_tickets.append(
                    {
                        **ref,
                        "state": state,
                        "reason": reason,
                    }
                )
            if stuck or str(row["workflow_state"]) in PAUSED_STATES:
                paused.append(
                    {
                        **ref,
                        "state": state,
                        "paused_action": paused_action,
                        **waiting,
                    }
                )
        _visual_qa_for_holds(connection, tables, ticket_rows, wait_facts)
        _open_questions_for_tickets(connection, tables, ticket_rows)
    flows, retries, tracked_at = _ticket_flows(
        connection,
        {*ticket_info, *((repo.casefold(), ticket_id) for _, repo, ticket_id in active)},
        {key: info.state for key, info in ticket_info.items()},
        chart,
        now,
        tables,
        exposures,
        qa_rounds,
    )
    for key, info in ticket_info.items():
        flows[key] = anchor_close(flows[key], info.closed_at, now)
    for row, key in ticket_rows:
        row["state_age_seconds"] = flows[key]["state_age_seconds"]
        shipped = shipped_facts(chart, flows[key]) or {"at": None, "how": None}
        if row["state"] == HANDED_OFF_EXTERNALLY:
            shipped = {"at": row.get("external_at"), "how": "handed-off"}
        row["shipped_at"], row["shipped_how"] = shipped["at"], shipped["how"]
    for item, repo, ticket_id in active:
        key = (repo.casefold(), ticket_id)
        info = ticket_info.get(key, TicketInfo(None, False, None))
        item["tracker"] = info.tracker
        item["stuck"] = info.stuck
        item["flow"] = flows[key]
        if key in retries:
            item["retries"] = retries[key]
    spent = ticket_totals(connection)
    ticket_records = {
        key: _ticket_record(info, flows[key], tracked_at.get(key), spent.get(key), chart)
        for key, info in ticket_info.items()
    }
    record_keys = _record_keys(ticket_info)
    for item, key in ticket_rows:
        item["record"] = record_keys[key]
    ci_runs = (
        _ci_runs(connection, now, sources, {key: info.tracker for key, info in ticket_info.items()})
        if "ci_jobs" in tables else {"runs": [], "failures": []}
    )
    ci_pool = (
        _ci_pool(connection, now)
        if "ci_runners" in tables else {"state": "unread", "age_seconds": None, "runners": []}
    )
    ticket_of = {id(item): (repo.casefold(), ticket_id) for item, repo, ticket_id in active}
    ticket_jobs: dict[tuple[str, str], list[dict]] = {}
    for item in [*sorted(running, key=lambda item: item["elapsed_seconds"]), *queued]:
        key = ticket_of[id(item)]
        if key in record_keys:
            item["record"] = record_keys[key]
        ticket_jobs.setdefault(key, []).append(item)

    order = lambda item: (item["repo"].casefold(), item["ticket_id"])  # noqa: E731
    tickets.sort(key=order)
    stuck_tickets.sort(key=order)
    paused.sort(key=order)

    errors = {"last_hour": 0, "last_day": 0, "recent": []}
    newest_error_at = None
    if "poll_errors" in tables:
        row = connection.execute(
            f"""
            SELECT
              COALESCE(SUM({epoch.sql('recorded_at')} >= ?), 0) AS last_hour,
              COALESCE(SUM({epoch.sql('recorded_at')} >= ?), 0) AS last_day,
              MAX({epoch.sql('recorded_at')}) AS newest
            FROM poll_errors
            """,
            (now - 3600, now - 86400),
        ).fetchone()
        errors["last_hour"] = int(row["last_hour"])
        errors["last_day"] = int(row["last_day"])
        newest_error_at = row["newest"]
        errors["recent"] = [
            {
                **_ticket_ref(sources, item["repo"], item["ticket_id"]),
                "error": _clean(item["error"]),
                "age_seconds": _age(now, item["recorded_at"]),
            }
            for item in connection.execute(
                "SELECT repo, ticket_id, error, recorded_at FROM poll_errors "
                f"ORDER BY {epoch.sql('recorded_at')} DESC, id DESC LIMIT ?",
                (RECENT_ERRORS,),
            )
        ]

    deferrals = None
    newest_deferral_at = None
    if "poll_deferrals" in tables:
        rows = connection.execute(
            "SELECT detail, recorded_at FROM poll_deferrals "
            f"ORDER BY {epoch.sql('recorded_at')} DESC, id DESC LIMIT ?",
            (RECENT_DEFERRALS,),
        ).fetchall()
        deferrals = {
            "recent": [
                {
                    "detail": _clean(item["detail"]),
                    "age_seconds": _age(now, item["recorded_at"]),
                }
                for item in rows
            ]
        }
        newest_deferral_at = epoch.read(rows[0]["recorded_at"]) if rows else None

    feeder = None
    if last_feeder_poll(connection) is not None:
        items = feeder_items(connection)
        feeder = {
            **feeder_counts(connection),
            "items": [
                {
                    "repo": _clean(item["repo"], 120),
                    "issue": _clean(item["issue"], 64),
                    "title": _clean(item["title"], 120),
                    "state": _enum(item["state"], FEEDER_STATES),
                    "pr": _clean(item["pr_number"], 16) if item["pr_number"] is not None else None,
                    "reason": _clean(item["waiting_reason"], 200) if item["waiting_reason"] else None,
                    **(
                        {"blocked_by": _blocked_by(item["waiting_reason"], item["repo"])}
                        if item["state"] in FEEDER_WAITING
                        else {}
                    ),
                    "fed_age_seconds": _age(now, item["fed_at"]),
                    "tier": None if item["tier"] is None else int(item["tier"]),
                    "own_tier": None if item["own_tier"] is None else int(item["own_tier"]),
                }
                for item in items
            ],
        }
    attention_flags: list[dict] = []
    if "needs_attention" in tables:
        finished = (
            """
            LEFT JOIN ticket_state AS state
              ON state.repo = flag.repo AND state.ticket_id = flag.ticket_id
            WHERE COALESCE(state.workflow_state, '') NOT IN ('merged', 'done')
            """
            if "ticket_state" in tables
            else ""
        )
        for row in connection.execute(
            f"""
            SELECT flag.repo, flag.ticket_id, flag.reason, flag.detail
            FROM needs_attention AS flag
            {finished}
            ORDER BY {epoch.sql('flag.raised_at')}, flag.repo, flag.ticket_id, flag.reason
            """
        ):
            attention_flags.append(
                {
                    **_ticket_ref(sources, row["repo"], row["ticket_id"], 120),
                    "reason": _enum(row["reason"], ATTENTION_REASONS),
                    "detail": _clean(row["detail"]) if row["detail"] is not None else "",
                }
            )

    linear_webhook = None
    health = read_health(connection)
    if health is not None:
        linear_webhook = {
            "last_received_age_seconds": _age(now, health.last_received_at),
            "last_event": (
                _clean(health.last_event, 64) if health.last_event is not None else None
            ),
            "listener_age_seconds": _age(now, health.started_at),
            "dirty": health.dirty,
            "warning": health.warning(now),
            "note": health.quiet_note(now),
        }

    recent_handoffs: list[dict] = []
    if jobs:
        recent_handoffs = [
            _handoff_row(sources, row, now)
            for row in connection.execute(
                f"SELECT {_HANDOFF_COLUMNS} FROM jobs WHERE {_HANDOFF_DONE} "
                f"ORDER BY {epoch.sql('finished_at')} DESC, id DESC LIMIT ?",
                (RECENT_HANDOFFS,),
            )
        ]

    verdicts = None
    handled: list[sqlite3.Row] = []
    if "handled_comments" in tables:
        handled = _handled(connection, tables)
        verdicts = [_verdict_row(sources, row, now) for row in handled[:RECENT_VERDICTS]]
        oks: dict[tuple[str, str], list[str]] = {}
        for row in handled:
            if row["outcome"] in OK_OUTCOMES:
                oks.setdefault((str(row["repo"]).casefold(), str(row["ticket_id"])), []).append(row["outcome"])
        for item, key in ticket_rows:
            if key in oks:
                item["ok_outcomes"] = oks[key]

    budget = {"deferrals": 0, "exhausted": 0, "rate_limited": 0}
    if "poll_deferrals" in tables:
        row = connection.execute(
            "SELECT count(*) AS n FROM poll_deferrals "
            f"WHERE {epoch.sql('recorded_at')} >= ? AND detail LIKE '%Linear poll budget%'",
            (now - 3600,),
        ).fetchone()
        budget["deferrals"] = int(row["n"])
    if "poll_errors" in tables:
        row = connection.execute(
            f"""
            SELECT
              COALESCE(SUM(error LIKE '%Linear poll budget is exhausted%'), 0)
                AS exhausted,
              COALESCE(SUM(error LIKE '%Linear returned HTTP 429%'
                           OR error LIKE '%Linear returned RATELIMITED%'
                           OR error LIKE '%Linear rate-limit backoff%'), 0)
                AS rate_limited
            FROM poll_errors WHERE {epoch.sql('recorded_at')} >= ?
            """,
            (now - 3600,),
        ).fetchone()
        budget["exhausted"] = int(row["exhausted"])
        budget["rate_limited"] = int(row["rate_limited"])
    if budget["rate_limited"]:
        budget_state = "rate_limited"
    elif budget["exhausted"]:
        budget_state = "exhausted"
    elif budget["deferrals"]:
        budget_state = "deferring"
    else:
        budget_state = "ok"

    scripted = in_flight = bot_to_bot = 0
    if jobs:
        marks = ",".join("?" for _ in WAIT_RECIPIENTS)
        row = connection.execute(
            f"""
            SELECT
              COALESCE(SUM(recipient IN ({marks}) AND state = 'done'
                           AND handoff_comment_id IS NOT NULL), 0) AS scripted,
              COALESCE(SUM(recipient IN ({marks}) AND state = 'running'), 0)
                AS in_flight,
              COALESCE(SUM(recipient = 'grok'), 0) AS bot_to_bot
            FROM jobs
            """,
            (*WAIT_RECIPIENTS, *WAIT_RECIPIENTS),
        ).fetchone()
        scripted = int(row["scripted"])
        in_flight = int(row["in_flight"])
        bot_to_bot = int(row["bot_to_bot"])
    denominator = scripted + bot_to_bot
    engine_handled = engine_escalated = 0
    if "engine_decisions" in tables:
        row = connection.execute(
            """
            SELECT
              COALESCE(SUM(outcome IN ('granted', 'landed')), 0) AS handled,
              COALESCE(SUM(outcome = 'escalated'), 0) AS escalated
            FROM engine_decisions
            """
        ).fetchone()
        engine_handled = int(row["handled"])
        engine_escalated = int(row["escalated"])

    return {
        "queue": {
            "totals": totals,
            "by_recipient_action": [groups[key] for key in sorted(groups)],
        },
        "running": running,
        "queued": queued,
        "ticket_records": ticket_records,
        "record_keys": record_keys,
        "ticket_jobs": ticket_jobs,
        "unplaced_running": unplaced_running,
        "running_on": running_on,
        "overdue_jobs": overdue,
        "tickets": tickets,
        "stuck_tickets": stuck_tickets,
        "paused": paused,
        "errors": errors,
        "newest_error_at": newest_error_at,
        "deferrals": deferrals,
        "newest_deferral_at": newest_deferral_at,
        "linear_webhook": linear_webhook,
        "feeder": feeder,
        "attention_flags": attention_flags,
        "ci_attention": _ci_attention(connection, tables),
        "held": held,
        "verdicts": verdicts,
        "relay": _relay(connection, tables, sources, handled, ticket_records, waiting_on, len(tickets), now),
        "ci_runs": ci_runs,
        "ci_pool": ci_pool,
        "qa": _qa(connection, now, tables, ticket_info),
        "linear_budget": {
            "state": budget_state,
            "deferrals_last_hour": budget["deferrals"],
            "exhausted_last_hour": budget["exhausted"],
            "rate_limited_last_hour": budget["rate_limited"],
            "requests_per_hour": LINEAR_BUDGET_REQUESTS_PER_HOUR,
            "complexity_per_hour": LINEAR_BUDGET_COMPLEXITY_PER_HOUR,
        },
        "handoff_savings": {
            "scripted": scripted,
            "scripted_in_flight": in_flight,
            "bot_to_bot": bot_to_bot,
            "engine_handled": engine_handled,
            "engine_escalated": engine_escalated,
            "scripted_share": (
                round(scripted / denominator, 3) if denominator else None
            ),
            "definition": HANDOFF_SAVINGS_DEFINITION,
            "scripted_share_definition": SCRIPTED_SHARE_DEFINITION,
            "recent": recent_handoffs,
        },
    }


class TicketInfo(NamedTuple):
    tracker: str | None
    stuck: bool
    state: str | None
    repo: str = ""
    ticket_id: str = ""
    round: int = 0
    reason: str | None = None
    key: str = ""
    stuck_at: float | None = None
    closed_at: float | None = None


def _record_keys(ticket_info: dict[tuple[str, str], TicketInfo]) -> dict[tuple[str, str], str]:
    """Each tracked ticket's tickets.json key, from its stored repo and ticket
    id redacted but never cut short, so tickets whose shown identifiers cut to
    the same text keep separate records. Tickets that redaction alone makes
    equal take a `~<n>` suffix, in stored-identifier order."""
    keys: dict[tuple[str, str], str] = {}
    taken: set[str] = set()
    for ticket in sorted(ticket_info):
        key = base = ticket_info[ticket].key
        count = 1
        while key in taken:
            count += 1
            key = f"{base}~{count}"
        taken.add(key)
        keys[ticket] = key
    return keys


def _ticket_record(
    info: TicketInfo,
    flow: dict,
    tracked_at: float | None,
    totals: dict | None,
    chart: dict | None,
) -> dict:
    """A tracked ticket's tickets.json record, less its live jobs. Shaped so
    the ticket page's `_detail_model({"flow": record}, chart)` reads it as it
    reads a job row."""
    visits = flow["visits"]
    shipped = shipped_facts(chart, flow)
    if shipped is not None:
        began = tracked_at if tracked_at is not None else (
            visits[0]["started_at"] if visits else None
        )
        shipped.update(
            intake_to_ship_seconds=(
                None if shipped["at"] is None or began is None
                else int(max(0.0, shipped["at"] - began))
            ),
            revise_rounds=info.round,
            **(totals or {"jobs": 0, "tokens_in": None, "tokens_out": None, "cost_usd": None}),
        )
    stopped = stop_facts(chart, flow, str(info.state), info.stuck)
    if stopped is not None:
        stopped["reason"] = info.reason
        if info.stuck and stopped["at"] is None:
            stopped["at"] = info.stuck_at
    return {
        "repo": info.repo,
        "ticket_id": info.ticket_id,
        "state": flow["state"],
        "state_age_seconds": flow["state_age_seconds"],
        "round": info.round,
        "hops": max(0, len(visits) - 1),
        "off_chart_count": sum(1 for visit in visits if visit["off_chart"]),
        "terminal": flow["terminal"],
        "hops_to_shipped": flow["hops_to_shipped"],
        "all_durations_known": flow["all_durations_known"],
        "visits": visits,
        "shipped": shipped,
        "stopped": stopped,
    }


def _ticket_flows(
    connection: sqlite3.Connection,
    keys: set[tuple[str, str]],
    states: dict[tuple[str, str], str | None],
    chart: dict | None,
    now: float,
    tables: set[str],
    exposures: dict[str, exposure.Exposure],
    qa_rounds: dict[str, int],
) -> tuple[dict[tuple[str, str], dict], dict[tuple[str, str], int], dict[tuple[str, str], float]]:
    """(flow, accepted retries, tracked_at) per ticket key (repo casefolded,
    ticket id). Each table is read whole and kept for `keys` only: no
    `IN (...)` list, so the number of tickets never meets SQLite's variable cap."""
    epoch.register(connection)
    histories: dict[tuple[str, str], list[JobStep]] = {key: [] for key in keys}
    if "jobs" in tables:
        columns = (
            {str(row["name"]) for row in connection.execute("PRAGMA table_info(job_telemetry)")}
            if "job_telemetry" in tables
            else set()
        )
        joined = "job_id" in columns
        times = ", ".join(
            f"t.{name} AS {name}" if joined and name in columns else f"NULL AS {name}"
            for name in ("started_at", "finished_at", "suite_wait_seconds")
        )
        kept = []
        for row in connection.execute(
            "SELECT jobs.id, jobs.repo, jobs.ticket_id, jobs.action, jobs.recipient, jobs.created_at, "
            f"jobs.attempt, {times} "
            "FROM jobs "
            + ("LEFT JOIN job_telemetry t ON t.job_id = jobs.id " if joined else "")
            + f"ORDER BY {epoch.sql('jobs.created_at')}, jobs.rowid"
        ):
            key = (str(row["repo"]).casefold(), str(row["ticket_id"]))
            if key in histories:
                kept.append((key, row))
        rounds = dict(connection.execute(
            f"SELECT jobs.id, {_DETAIL_ROUND} AS round FROM json_each(?) AS ids JOIN jobs ON jobs.id = ids.value",
            (json.dumps([row["id"] for _, row in kept]),),
        ).fetchall())
        for key, row in kept:
            histories[key].append(JobStep(
                _enum(row["action"], ACTIONS),
                _enum(row["recipient"], RECIPIENTS),
                epoch.read(row["created_at"]),
                epoch.cell(row["started_at"]),
                epoch.cell(row["finished_at"]),
                int(row["attempt"]),
                _clean(str(row["id"])[:8], 8),
                _exposure(
                    exposures.get(str(row["id"])),
                    _review_round(_round(rounds.get(row["id"])), qa_rounds.get(str(row["id"]))),
                ),
                _maybe_count(row["suite_wait_seconds"]),
            ))
    recorded: dict[tuple[str, str], list[Transition]] = {key: [] for key in keys}
    if "ticket_transitions" in tables:
        for row in connection.execute(
            "SELECT repo, ticket_id, from_state, to_state, at, source FROM ticket_transitions "
            f"ORDER BY {epoch.sql('at')}, id"
        ):
            key = (str(row["repo"]).casefold(), str(row["ticket_id"]))
            at = epoch.cell(row["at"])
            if key in recorded and at is not None:
                recorded[key].append(Transition(
                    None if row["from_state"] is None
                    else _enum(row["from_state"], WORKFLOW_STATES),
                    _enum(row["to_state"], WORKFLOW_STATES),
                    at,
                    str(row["source"]),
                ))
    tracked_at: dict[tuple[str, str], float] = {}
    if "tracked_tickets" in tables:
        for row in connection.execute("SELECT repo, ticket_id, tracked_at FROM tracked_tickets"):
            key = (str(row["repo"]).casefold(), str(row["ticket_id"]))
            at = epoch.cell(row["tracked_at"])
            if key in keys and at is not None:
                tracked_at[key] = at
    retries: dict[tuple[str, str], int] = {}
    if "ticket_retries" in tables:
        for row in connection.execute(
            "SELECT repo, ticket_id, count(*) AS n FROM ticket_retries "
            "WHERE outcome NOT LIKE 'rejected-%' GROUP BY repo, ticket_id"
        ):
            key = (str(row["repo"]).casefold(), str(row["ticket_id"]))
            if key in keys:
                retries[key] = retries.get(key, 0) + int(row["n"])
    flows = {
        key: job_flow(chart, histories[key], states.get(key), now, recorded[key], tracked_at.get(key))
        for key in keys
    }
    return flows, retries, tracked_at


def _has_placeholder(value: object) -> bool:
    if isinstance(value, str):
        return "REPLACE_WITH_" in value
    if isinstance(value, dict):
        return any(_has_placeholder(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_placeholder(item) for item in value)
    return False


def _machines_yaml(path: Path | None) -> tuple[str, list[dict], str]:
    """Optional F2-01 inventory: its state, device items and orch host.

    Absent or invalid degrades to no items. The host is the raw orch_host,
    else the `local: true` device, else DEFAULT_ORCH_HOST.
    """
    if path is None or not path.is_file():
        return "absent", [], DEFAULT_ORCH_HOST
    try:
        if path.stat().st_size > MACHINES_MAX_BYTES:
            return "invalid", [], DEFAULT_ORCH_HOST
        data = load_yaml(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return "invalid", [], DEFAULT_ORCH_HOST
    if isinstance(data, dict):
        data = apply_overlay(path, data)[0]
    if not isinstance(data, dict) or not isinstance(data.get("devices"), list):
        return "invalid", [], DEFAULT_ORCH_HOST
    items = data["devices"]
    if any(not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in items):
        return "invalid", [], DEFAULT_ORCH_HOST
    host = data.get("orch_host")
    if not isinstance(host, str):
        host = next(
            (item["name"] for item in items if item.get("local") is True), DEFAULT_ORCH_HOST
        )
    return "present", items, host


def _machines(
    items: list[dict],
    host: str,
    held: dict[str, int],
    *,
    unplaced: int = 0,
) -> tuple[list[dict], int | None]:
    """Device rows and the orch host's slot_max; the `unplaced` running jobs
    are counted in the host's held slots (#90)."""
    devices: list[dict] = []
    box_max = None
    for item in items:
        slot_max = item.get("slot_max")
        slot_max = (
            slot_max
            if isinstance(slot_max, int) and not isinstance(slot_max, bool)
            else None
        )
        harnesses = item.get("harnesses")
        names = harnesses.keys() if isinstance(harnesses, dict) else (
            harnesses if isinstance(harnesses, list) else ()
        )
        enabled = item.get("enabled", True)
        devices.append(
            {
                "name": _clean(item["name"], 40),
                "slot_max": slot_max,
                "held": held.get(item["name"], 0)
                + (unplaced if item["name"] == host else 0),
                "enabled": enabled is True,
                "drain": item.get("drain") is True,
                "local": item["name"] == host,
                "placeholder": _has_placeholder(item),
                "harnesses": sorted(
                    {str(name) for name in names if str(name) in HARNESSES}
                ),
            }
        )
        if item["name"] == host:
            box_max = slot_max if box_max is None else box_max
    return devices, box_max


def _audit_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, list):
        return [_clean(item, 60) for item in value[:20]]
    return _clean(value, 200)


_WAITER_HARNESS = {f"waiter-{bot}": harness for bot, harness in OWNING_BOTS.items()}


def _every_service_uses(pending: object, version: int, live: live_settings.Live | None) -> bool:
    if live is None or not live.is_applied(version) or not isinstance(pending, list):
        return False
    return all(
        name == POLLER_SERVICE
        or (name in _WAITER_HARNESS and live.seen.get(_WAITER_HARNESS[name], 0) >= version)
        for name in pending
    )


def _adopted_new(row: dict, live: live_settings.Live) -> bool:
    name = row["field"]
    if row["target"] == "account":
        account = row["account"]
        return (isinstance(account, str) and isinstance(name, str)
                and live.adopted_account_holds(account, name, row["new"]))
    device = row["device"]
    return (isinstance(device, str) and isinstance(name, str)
            and live.adopted_holds(device, name, row["new"]))


def _change_key(row: dict) -> tuple[str | None, str, str] | None:
    subject = row["account"] if row["target"] == "account" else row["device"]
    field = row["field"]
    return (row["target"], subject, field) if isinstance(subject, str) and isinstance(field, str) else None


def _supersedes(later: dict, row: dict) -> bool:
    if later["version"] <= row["version"]:
        return False
    key = _change_key(row)
    if key is not None and key == _change_key(later):
        return True
    return (row["target"] == later["target"] == "account" and later["field"] in ("add", "remove")
            and isinstance(row["account"], str) and later["account"] == row["account"])


def _settings_results(path: Path, live: live_settings.Live | None) -> list[dict]:
    """The last SETTINGS_RESULTS entries of the settings audit log (#777),
    oldest first, each cut to the AuditEntry keys with its strings clipped.
    A landed change that a later landed change replaced reads `superseded`
    (#990 f-2), so it never waits on a restart that can no longer finish it."""
    rows = []
    for line in read_log_tail(path, lines=SETTINGS_RESULTS):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            row = {key: _audit_value(entry.get(key)) for key in AUDIT_KEYS}
            row["result"] = _enum(entry.get("result"), AUDIT_RESULTS)
            rows.append(row)
    landed = [r for r in rows if r["result"] in ("applied", "pending") and type(r["version"]) is int]
    for i, row in enumerate(landed):
        later = next((s for s in landed[i + 1:] if _supersedes(s, row)), None)
        if later is not None:
            row["result"], row["superseded_by"] = "superseded", later["id"]
    for row in rows:
        version = row["version"]
        if row["result"] == "applied" and type(version) is int:
            row["waiting"] = (live is None or not live.is_applied(version)
                              or not _adopted_new(row, live))
            if row["waiting"]:
                row["result"] = "pending"
        elif row["result"] == "pending" and type(version) is int:
            if _every_service_uses(row["pending"], version, live) and _adopted_new(row, live):
                row["result"] = "applied"
    return rows


# --- writing --------------------------------------------------------------------


def write_snapshot(out_dir: Path, snapshot: dict) -> tuple[Path, Path]:
    """Write status.json and every page (PAGES) from one snapshot, as a set.

    The same write as `write_snapshot_pages`; returns just
    (status.json, index.html). Callers that need every written path (the
    push, the CLI) use `write_snapshot_pages`.
    """
    paths = write_snapshot_pages(out_dir, snapshot)
    return paths[0], out_dir / "index.html"


def write_snapshot_pages(out_dir: Path, snapshot: dict) -> tuple[Path, ...]:
    """Write status.json, the static assets (STATIC_ASSETS), every page (PAGES),
    tickets.json and evals.json from one snapshot, as a set.

    The ticket records (RECORDS) go to tickets.json only and the weekly eval
    bundle (EVALS, lib/dashboard_evals.py) to evals.json only, a "no dir"
    bundle when the snapshot has none, with the snapshot's generated_at_epoch
    so the page can tell an evals.json older than status.json; status.json and
    the static assets are written from the rest of the snapshot, and the pages from the rest plus the
    eval bundle, so evals.html draws the weekly table. Each
    file is written to a temp file in the same directory and fsynced, then
    renamed into place, status.json first. If a later rename fails, every file
    already renamed is put back as it was (or removed if there was none), so the
    directory never mixes files from this snapshot with files from an earlier
    one. Returns every path: status.json, STATIC_ASSETS, the pages in PAGES
    order, then tickets.json and evals.json.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    paged = {key: value for key, value in snapshot.items() if key != RECORDS}
    status = {key: value for key, value in paged.items() if key != EVALS}
    texts = {"status.json": json.dumps(status, indent=2, sort_keys=True, allow_nan=False) + "\n"}
    texts.update(render_static_assets(status))
    texts.update(render_pages(paged))
    texts["tickets.json"] = tickets_json(snapshot)
    texts["evals.json"] = evals_json(snapshot.get(EVALS), snapshot.get("generated_at_epoch"))
    targets = [out_dir / name for name in texts]
    temps: list[str] = []
    try:
        staged = [_write_temp(path, texts[path.name], temps) for path in targets]
        published: list[tuple[Path, str | None]] = []
        try:
            for path, temp in zip(targets, staged):
                previous = None
                if path.is_file():
                    previous = _temp_name(path, temps, suffix=".prev")
                    shutil.copyfile(path, previous)
                os.replace(temp, path)
                published.append((path, previous))
        except BaseException:
            for path, previous in reversed(published):
                if previous is not None:
                    os.replace(previous, path)
                else:
                    path.unlink(missing_ok=True)
            raise
    finally:
        for temp in temps:
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass
    return tuple(targets)


def tickets_json(snapshot: dict) -> str:
    """The ticket records as compact JSON, with <, > and & written as JSON
    escapes so no markup survives in the file even outside a JSON parser."""
    text = json.dumps(
        {
            "schema": TICKETS_SCHEMA,
            "generated_at_epoch": snapshot.get("generated_at_epoch"),
            "tickets": snapshot.get(RECORDS, {}),
        },
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026") + "\n"


def _temp_name(path: Path, temps: list[str], *, suffix: str = ".tmp") -> str:
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=suffix)
    os.close(fd)
    temps.append(temp)
    return temp


def _write_temp(path: Path, text: str, temps: list[str]) -> str:
    temp = _temp_name(path, temps)
    with open(temp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp, 0o644)
    return temp


# --- rendering ----------------------------------------------------------------


def render_html(snapshot: dict) -> str:
    """The main page (index.html). Every value is escaped and already redacted."""
    return render_pages(snapshot)["index.html"]


__all__ = [
    "PAGES",
    "STATIC_ASSETS",
    "DashboardError",
    "collect_snapshot",
    "redact",
    "render_html",
    "render_pages",
    "render_static_assets",
    "write_snapshot",
    "write_snapshot_pages",
]
