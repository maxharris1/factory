"""Factory command-line entry point."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import functools
import getpass
import json
import os
import re
import secrets
import shutil
import signal
import sqlite3
import stat
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from typing import Any

from lib import (
    account_doctor,
    account_home,
    codex_rollout,
    codex_sandbox,
    containers,
    doctor_live,
    exposure,
    health,
    job_detail,
    job_run,
    job_token,
    linear_webhook,
    oom,
    placed_run,
    skill_preflight,
    skill_probe,
    suite_slot,
    telemetry,
    trace,
    usage_probe,
    usage_readings,
    usage_record,
    waiter,
    worktrees,
)
from lib.dashboard_cli import main as dashboard_main
from lib.dashboard_push import LivePusher, PollPusher, poll_pusher_for, push_timer_tick
from lib.dispatch.constants import HARNESS_WORK
from lib.dispatch.cycle_clock import CycleClock
from lib.dispatch.helpers import _retry_of
from lib.dispatch.poll import sleep_on_either_clock
from lib.dispatcher import Dispatcher, PollCycleFailed, PollLoop, ReviewShipper, ShipError
from lib.eval_report import (
    EvalError,
    append_message,
    build_metrics,
    build_report,
    render,
    render_json,
    render_metrics,
    render_metrics_json,
    render_qa,
)
from lib import qa_runners, qa_smoke
from lib.factory_md import EXAMPLE_PATHS, QA_RUNNERS, FactoryConfig, load_factory_md
from lib.github_actions import GitHubAdapterError, _status as gh_status
from lib.grok_delivery import GrokDeliveryError, wake_delivery
from lib.handoff import format_header
from lib.harness_env import CPU_CAP, account_environ, harness_env
from lib.harnesses import all_drivers
from lib.hold import MergeUpdate
from lib.hook_t7 import T7Hook
from lib.hook_t8 import T8Hook
from lib.load import Finding
from lib.scratch import remove as remove_scratch
from lib.device_monitor import DeviceMonitor, read_memory, retry_stranded
from lib.feeder import Feeder
from lib.intake_live import GitHubIntakeClient, LinearIntakeClient
from lib.job_state import record_job_event
from lib.device_pool import DevicePool
from lib.machines_yml import MachinesConfig, load_machines_yml
from lib.orch_yml import (
    OrchConfig,
    ProjectPins,
    approvals,
    bot_approver_findings,
    hold_approver_findings,
    project_device_findings,
    qa_storage_findings,
    refused_harness_findings,
    linear_approver_findings,
    load_approvals,
    load_orch_yml,
    parse_bind,
)
from lib.poll_source import (
    FedPulls,
    GhCacheExpiries,
    IntakeRisks,
    LivePollSource,
    PollSourceError,
    RequiredGlobs,
)
from lib.linear_upload import HttpLinear
from lib.qa_cloud import QaWorker
from lib.qa_storage import S3Storage
from lib.pstack_models import rules_text
from lib.pstack_pin import Mode, doctor, install_skills, remove_claude_extras, unstable_checkout
from lib.handoff_check import (
    Expected,
    Read,
    ReadError,
    expected_for,
    gh_reader,
    problems as handoff_problems,
    ready_at,
)
from lib.pre_ready import (
    ContextUnavailable,
    HeadCheck,
    PreReadyResult,
    check_environ,
    checks_for,
    committed_checkout,
    first_failure,
)
from lib.queue import (
    BUSY_TIMEOUT_SECONDS,
    MAX_INVALID_HANDOFFS,
    SLOTS_TABLE,
    SUBMITTED_INVALID,
    ClaimProofError,
    Proof,
    QueueError,
    SQLiteJobQueue,
    job_from_row,
    require_latest_claim,
    require_proof,
)
from lib.remote import SshTransport
from lib.role_router import RoutingError, load_routing_yml, routing_argv_for_detail
from lib.state_backup import (
    BackupError,
    CycleBackup,
    backup,
    backup_every,
    check_manifest,
    db_facts,
    default_backup_path,
    labelled_backup,
    merge_job_detail,
    restore,
    write_manifest,
)
from lib.status import main as status_main
from lib.update_check import (
    HeadDiff,
    MovedHead,
    RebaseScope,
    RetargetDelta,
    UpdateClass,
    classify as classify_update,
    head_diff,
    merge_base,
    merge_update,
    moved_head_probe,
    rebase_scope,
    retarget_delta,
    stack_bounds,
)
from lib.wait import (
    OWNING_BOTS,
    WaitError,
    open_queue,
    owned_job,
    recipient_for,
    retry_locked,
    wait_for_job,
)

DB_ENV = "ORCH_DB"
PIDFILE_ENV = "ORCH_POLL_PIDFILE"
# `orch job run` could not start a remote job (preflight or checkout failed,
# #94) and requeued it: EX_TEMPFAIL, nothing ran, try again.
REQUEUED_EXIT = 75
MAX_BRIEF_BYTES = 1024 * 1024

ROOT = Path(__file__).resolve().parent.parent

USAGE = """\
usage: orch <command> [options]

commands:
  check-config     validate orch.yml, machines.yml and factory.md
  install-skills   install the pinned pstack and router skills
  doctor           canary the pinned poteto-mode skill
  doctor-runtime   check PyYAML and the shared dispatcher database
  eval-report      weekly eval report from a local log directory
  log-message      log one manual bot-to-bot message to dispatcher.log
  poll             poll configured trackers and dispatch work
  qa-worker        claim qa jobs and run their cloud QA lanes
  qa               qa smoke: health-check a QA runner and evidence root, no GitHub comment
  init-db          create and initialize the dispatcher database
  backup-db        back up the dispatcher database (orch poll does each cycle)
  db-backup        labelled backup to take before a manual write to orch.db
  restore-db       restore a missing dispatcher database from its backup
  move-db          check a dispatcher database moving to another host
  db-spill-detail  move bulky job detail out of the database (#653)
  wait             block until the dispatcher queues a job for a bot
  job              run, heartbeat, finish or cancel a job
  gc               remove stale job worktrees with no changes
  cleanup          delete finished job checkouts on every device, or one bot's with --bot (#928)
  dashboard        write (and optionally push) the read-only status snapshot
  linear-webhook   serve the Linear webhook listener that marks issues dirty
  health           component heartbeats: report or forget (issue #92)
  heal             restart down or stale-code components (issue #107)
  settings         apply device settings asked for in Mission Control (#777)
  status           read-only snapshot of the queue, devices and paused tickets
  retry            ask the dispatcher to restart a stuck ticket (kind=retry)
  trace            read-only: every job attempt on a ticket and its handoff verdict
  usage            per-account usage readings: migrate orch.db, record an external reading (#772)
  accounts         prepare account folders, check sign-ins, run the dashboard sign-in broker

check-config [--orch PATH] [--machines PATH] [--factory [owner/name=]PATH ...]
             [--allow-placeholders] [--digest]
  With no paths, checks orch.yml, machines.yml and the example factory.md
  files. When --orch is given, machines.yml is checked only with --machines.
  --allow-placeholders permits and reports REPLACE_WITH_ install values in
  orch.yml only; a machines.yml device with one is always skipped with a warning.
  Warns about uncommitted edits to a tracked orch.yml or machines.yml.
  --digest prints the sha256 of each effective config (base plus overlay).
  owner/name=PATH names the project a factory.md belongs to; a file with
  qa enabled needs it, and that project needs a qa_storage entry.
  A disabled or placeholder device is named on stderr and never scheduled.
  An unknown model name warns on stderr and still exits 0.

install-skills [--home PATH] [--project PATH] [--cloud] [--allow-unstable-checkout]
               [--remove-claude-extras]
  Symlink each pinned skill into ~/.claude/skills, ~/.agents/skills,
  and ~/.cursor/skills. --cloud copies them into the project at
  .claude/skills, .agents/skills, and .cursor/skills. A local install also
  links pstack's agents/ and docs/ into ~/.claude and writes
  ~/.claude/rules/pstack-models.md. --remove-claude-extras removes those
  three and leaves the skills.

doctor [--home PATH] [--project PATH] [--cloud]
       [--responses DIR] [--print-canary]
doctor --live [--home PATH] [--machines PATH] [--orch PATH]
       [--harness NAME]... [--timeout SECONDS] [--work DIR]
  Print a canary that asks the harness to name poteto-mode and quote
  a token from the pinned skill. Score the replies the owning bots
  write. Exit 1 if any harness did not load that version.
  --print-canary prints the prompt and does not read replies.
  --live starts every enabled harness (or each --harness) on each
  machines.yml device with its bot's waiter command, scores its
  reply, and names each failing harness@device. A harness refused in
  orch.yml is skipped. A harness past --timeout (default 600) is
  stopped. Run it as each bot: it reads only this user's waiter config.

eval-report --input PATH [--since TIME] [--until TIME] [--json]
eval-report --db PATH --baseline PATH
  Print the weekly eval report for one directory of fixtures or
  collected logs. It does not contact GitHub or Linear. --json prints
  the --input report as JSON.
  --db prints the #209 metrics of a backed-up orch.db, opened read-only,
  beside those of the --baseline snapshot (O4's), then the --db
  snapshot's QA section, split by runner.

log-message --repo OWNER/NAME [--pr N] --from BOT --to BOT [--kind KIND]
            [--log PATH]
  Append one line to dispatcher.log for a bot-to-bot message sent by hand
  instead of a scripted handoff. eval-report counts these lines. --log
  defaults to dispatcher.log beside $ORCH_DB; --kind defaults to handoff.
  Exits 2 and writes nothing on a bad repo, pr, bot or kind.

qa-worker [--db PATH] [--orch PATH] [--once] [--tick-seconds N]
  Claim queued qa jobs and coordinate their cloud lanes (docs/SPEC.md 4.2.2).
  Holds no build slot and makes no model call. --once runs one tick.
  Only one worker may hold the database's qa-worker lock. With no QA runner
  registered it claims nothing.

qa smoke --repo OWNER/NAME --runner NAME [--orch PATH]
  Check a host is ready for Live App QA before any real run: the project
  has a qa_storage entry, the runner answers a ping, the evidence root
  exists, and a synthetic result.json can be written and read back where
  the dashboard reads lane evidence. Removes everything it wrote, never
  creates the evidence root, posts no GitHub comment, signs no link and
  launches no lane. Prints one ok/fail line per check; exits 1 if any fail.

poll [--db PATH] [--orch PATH] [--machines PATH] [--once] [--pidfile PATH]
  Poll GitHub and Linear, apply dispatcher transitions, and deliver work.
  --once runs one complete poll cycle. --db defaults to $ORCH_DB.
  --once exits 1, naming the source, when a GitHub, Linear or ticket
  read fails; the cycle still runs to its end first.
  Each poll also releases device slots whose worker is confirmed gone and,
  for a remote device holding a slot, runs one SSH check; a device that
  fails every check for 60 minutes marks its running jobs stuck (F2-02).
  --machines defaults to the checkout's machines.yml; without that file
  no device is checked.
  Only one poller may hold the database's lock. The holder writes its pid
  to --pidfile, else $ORCH_POLL_PIDFILE, else poller.pid beside the
  database, and removes it on exit, including on SIGTERM. When orch.yml
  sets dashboard.push, each cycle (or every dashboard.push.every_minutes)
  starts `orch dashboard push --once` in the background; a push failure
  is one log line and never stops polling.

init-db [--db PATH] [--orch PATH]
  Create and initialize the database without polling or delivering work.

backup-db [--db PATH] [--backup PATH]
restore-db [--db PATH] [--backup PATH]
  `orch poll` backs the database up after every cycle (#66) with SQLite's
  online backup API, swapped into place atomically. --backup defaults to
  $ORCH_DB_BACKUP, else ~/.local/share/factory/backup/orch.db; a path in the
  database's own directory is refused. A failed cycle backup is one
  poll_errors row and polling goes on. $ORCH_DB_BACKUP_EVERY (seconds,
  default 0: every cycle) spaces the rolling backups out; a failed one is
  retried the next cycle, and orch poll exits 1 on a value that is not a
  whole number. backup-db takes one by hand.
  restore-db restores a missing database from the backup after an
  integrity check: exit 0 restored, 3 no backup, 1 when the database
  already exists or the backup is bad. deploy/poller-service.sh start and
  ensure run it when the database is missing.

move-db drained --db PATH
move-db manifest --db PATH --out PATH --source-host HOST --to HOST
                 --retired LINE
move-db check --db PATH --manifest PATH
move-db unlocked --db PATH
move-db merge-detail --from DIR --to DIR
  deploy/orch-move.sh runs these on copies taken with backup-db, each
  opened read-only. drained exits 0 when the copy has no running job and
  no held device slot, 1 (printing both counts) when it has, and 2 when
  it is missing or unreadable. manifest writes --out as JSON (the copy's
  sha256, per-table row counts, integrity, the hosts, the --retired line,
  the time and the sha256 of each file in the job-detail/ beside the copy)
  and exits 1, writing nothing, unless the copy passes integrity_check and
  every spilled job row has its job-detail file. check prints each table's
  row count and exits 0 when the copy has the manifest's sha256, passes
  integrity_check, the manifest has a retired line, each job-detail file
  the manifest names is beside the copy with its sha256, and every spilled
  job row has its file; otherwise it exits 1 saying which failed.
  unlocked reads the live database's locks, never the database: it exits
  0 when no poller holds PATH.lock and no `orch wait` holds a
  waiter-<bot>.lock beside PATH, 1 (naming each held lock) when one does.
  merge-detail copies each job-detail file in --from into --to and exits
  1, copying nothing, when a file already in --to differs from its --from
  copy by more than keys the --from copy adds.

db-backup <label> [--db PATH] [--dir PATH]
  Run this before any manual write to orch.db (#654). Copies the database
  with the online backup API to --dir/orch-<UTC stamp>-<label>.db; --dir
  defaults to manual/ beside the backup-db path. It then keeps the newest 5
  labelled backups plus any under 48 hours old and deletes the rest with
  their -wal and -shm files; `orch poll` prunes the same way once a day.
  label: letters, digits, '.', '_' and '-', at most 64. Exit 1 on failure.

db-spill-detail [--db PATH] [--dir PATH] [--dry-run | --vacuum]
  Move bulky job detail out of the database (#653, #1777). --dry-run opens
  the database read-only, takes no lock and prints the survey: rows and
  inline bytes for live, replayable and cold jobs, the bytes the cold spill
  would move, the rows whose detail is not a JSON object, and the ten
  largest inline keys. Without it the command holds the poller's lock (it
  refuses while `orch poll` runs), writes --dir/orch-<UTC stamp>-pre-spill.db
  with db-backup's labelled backup (--dir defaults to manual/ beside the
  backup-db path) and exits 1, changing no row, unless that copy passes
  PRAGMA integrity_check. It then moves the fixed keys (lib/job_detail.py
  SPILLED) of every job and each key over 1 KB of every cold job to
  job-detail/<id>.json beside the database, and prints the rows and inline
  bytes moved and the backup path. A rerun moves nothing new. --vacuum then
  runs VACUUM and wal_checkpoint(TRUNCATE) and prints the database size
  before and after; it exits 1 before the backup while any `orch wait`
  holds a waiter-<bot>.lock beside the database. Sequence: run --dry-run
  against the live database, stop the poller and the waiters, run with
  --vacuum, start them again. To undo, stop them, move orch.db and its -wal
  and -shm aside, and run `orch restore-db --db PATH --backup <printed
  backup path>`; leave job-detail/ in place. Afterwards merged readers (the
  dispatcher, the dashboard, briefs) see each job's full detail as before;
  a raw SQL read of jobs.detail sees the slim row, whose "spilled" key
  names the keys now in job-detail/<id>.json.

wait <bot> [--db PATH] [--machines PATH] [--orch PATH]
  <bot> is claude-bot, codex-bot, cursor-bot, or gemini-bot. Blocks until the
  dispatcher database has a queued job for that bot and a device in
  machines.yml (--machines, default the checkout's) has a free slot for
  its harness, claims both, prints one JSON line (with "device" and
  "remote"; a remote job also has "workdir"), and exits 0. Without
  --autostart it never starts a harness. --db defaults to $ORCH_DB.
wait <bot> --autostart [--db PATH] [--machines PATH] [--harness-config PATH]
                       [--orch PATH] [--state-dir DIR]
  Run as the bot's own user. Loops: claims the bot's next job on a box
  slot, writes its standard brief (with the repo's factory.md prose, read
  from orch.yml's factory_ref with gh) to DIR/jobs/<job-id>/brief.md, runs
  `orch job run <bot> <job-id> --worktree -- COMMAND` in its own session
  with the brief on stdin and in ORCH_JOB_BRIEF, and waits again. COMMAND
  is the argv in --harness-config (default
  ~/.config/factory/waiter-<bot>.yml: `bot` and `command`, owned by this
  user, writable by no one else); nothing from a job is added to it.
  --orch defaults to the checkout's orch.yml; --state-dir to
  ~/.local/state/factory/waiter-<bot>. A job with no standard brief, or
  whose start fails, is failed for T6. SIGTERM exits 0 and leaves running
  jobs alone. Run it under deploy/waiter-service.sh or
  deploy/orch-waiter@.service so it restarts by itself.

job run <bot> <job-id> [--db PATH] [--machines PATH] [--interval SECONDS]
        [--worktree] [--work-root DIR] [--git-url-template URL]
        [--status-file PATH] [--follow-grace SECONDS] -- COMMAND [ARG ...]
  Run COMMAND (the harness) with an orch-owned heartbeat: one heartbeat
  before it starts (a job that is not running is refused and nothing
  runs), then one every --interval seconds (default 15, or a third of the
  job's timeout_seconds if shorter; an override is capped at 30 seconds
  and at a third of the timeout, so a live job never shows as stale). The heartbeat stops when COMMAND exits, when the job
  is no longer running, or when this process dies. SIGTERM and SIGHUP are
  forwarded to COMMAND; a Ctrl-C reaches COMMAND through the process group.
  A signal before COMMAND starts means it never runs. Exits with COMMAND's
  status; never finishes the job. COMMAND gets ORCH_JOB_ID, ORCH_JOB_BOT,
  ORCH_TEST_SUITE_MAX (the placed device's test_suite_max) and ORCH_DB.
  Telemetry (issue #93): `claude -p` gets --output-format stream-json
  --verbose and `codex exec` gets --json; COMMAND's stdout is logged to
  events.jsonl in $ORCH_JOB_STATE_DIR (else telemetry-<bot>/<job-id>
  beside the database), capped at 20 MB and rotated,
  and a scrubbed summary goes to the job_telemetry table with each
  heartbeat and at exit, with the agent's plan, current and last finished
  task and sub-agents (issue #147). ORCH_TELEMETRY=counters keeps the
  counters but stores no such text; ORCH_TELEMETRY=off runs COMMAND as given.
  The job must hold the device slot orch wait claimed for this bot. On a
  remote device COMMAND runs there as the bot's user over the device's
  ssh_alias (ssh -l <bot> -- <alias>), under its login shell (bash -lc),
  in <workdir_root>/jobs/<job-id>, with ORCH_JOB_ID, ORCH_JOB_BOT,
  ORCH_TEST_SUITE_MAX, ORCH_JOB_DEVICE and ORCH_JOB_WORKDIR. There it is
  launched detached (setsid) with its own files in
  <workdir_root>/jobs/<job-id>.run/<tag>/
  (stdin, pid, log, handoff, exit), and its stdin is the brief: the file
  $ORCH_JOB_BRIEF, else this process's stdin when that is a regular file.
  orch job run follows it with short ssh calls, relays its log to stdout,
  and rides out a dropped link, retrying with backoff; SIGTERM, SIGHUP,
  Ctrl-C or the job ending elsewhere stop the remote worker. It is
  recorded as run-died only when the device confirms the worker gone
  without an exit status, or gives no certain answer for --follow-grace
  seconds (default 1800). When COMMAND
  ends the slot is released once the worker is confirmed gone (a remote
  worker still running is sent SIGTERM first); otherwise it stays held for
  the poller to confirm.
  How COMMAND ended is recorded in job_events while the job still runs
  (#91). If its harness never started (no worker, or it was never
  exec'd), the job is requeued, no round charged, in the step that frees
  the slot.
  --worktree runs COMMAND in the job's checkout on the device it is placed
  on, at the job's head or the default branch, exported as
  ORCH_JOB_WORKTREE. On the box that is a git worktree of one shared clone
  per repo under --work-root (else $ORCH_WORK_ROOT, else ~/factory-work);
  when COMMAND exits it is removed and pruned, unless it has uncommitted
  changes or unpushed commits; then it is kept and logged. On a remote
  device orch first checks over ssh that git, gh and COMMAND's binary exist
  under bash -lc, then clones or fetches the checkout into
  <workdir_root>/jobs/<job-id> (on the job's branch when its tip is the
  head) and copies $ORCH_JOB_BRIEF, if set, to <job-id>.brief.md beside it
  (ORCH_JOB_BRIEF then names that copy); the checkout is kept. If that
  check or the checkout fails, nothing runs: the job is requeued and its
  slot freed (no round or attempt charged), with one line saying why, and
  the exit status is 75. The git remote is --git-url-template, else
  $ORCH_GIT_URL_TEMPLATE, else https://github.com/{repo}.git.
  --status-file PATH: when this exits, its exit status is written to PATH
  (mode 600); the autostart waiter uses it to finish a remote run after a
  waiter restart.
job cancel <job-id> [--db PATH] [--reason TEXT]
  Cancel a queued job (an operator command, not a bot's): it never runs,
  T6 never retries it, and its dedupe key stays, so the comment that
  queued it queues nothing again. Exits 1 for a running or finished job
  (a running one's worker must be stopped first) and 2 for an unknown one.
job heartbeat <bot> <job-id> [--db PATH]
job done <bot> <job-id> (--handoff COMMENT-ID | --failed) [--db PATH]
         [--orch PATH]
  heartbeat keeps a running job out of the T6 timeout. done finishes it
  with the handoff comment ID; --failed hands it to T6. Repeating the
  same done is a no-op. --handoff first reads the comment from GitHub
  and checks it against the job (#191): the repo, the PR, an author that
  orch.yml's trusted_logins lets speak for <bot>, and the header's kind,
  from, role, head and round. One that does not fit is submitted-invalid:
  exit 1, the job keeps running, and the ticket is flagged needs-attention
  once; the third fails the job for T6. A GitHub read that fails exits 1
  and changes nothing.

gc [--work-root DIR] [--older-than HOURS] [--dry-run]
  Report and remove job worktrees older than HOURS (default 24) whose
  owning process is gone and that have no uncommitted changes or unpushed
  commits; prune each shared clone. Kept worktrees are reported with the
  reason. Exits 1 if a record could not be read or removed.

dashboard snapshot [--db PATH] [--log PATH] [--out DIR] [--orch PATH]
                   [--machines PATH] [--refresh SECONDS]
  Open the database read-only (SQLite mode=ro), read the tail of
  poller.log, and atomically write a redacted status.json and a static
  index.html to --out. --db defaults to $ORCH_DB; --log to $ORCH_POLL_LOG
  or poller.log beside the database; --out to $ORCH_DASHBOARD_DIR or
  dashboard/ beside the database. --refresh (15 to 30) overrides
  dashboard.refresh_seconds. There is no live server.

dashboard push [--db PATH] [--log PATH] [--out DIR] [--orch PATH]
               [--machines PATH] [--once]
  Snapshot, then send it with orch.yml's dashboard.push transport every
  dashboard.push.interval_seconds (15 to 30); --once sends one (what
  `orch poll` runs each cycle). Files go to temp names, then one ssh
  renames both into place. With no transport configured it logs one
  line and exits 0 without reading or writing.

linear-webhook serve [--db PATH] [--orch PATH] [--bind HOST:PORT]
  Serve orch.yml's linear_webhook: POST on linear_webhook.path only, a
  Linear-Signature HMAC-SHA256 checked against the secret in
  linear_webhook.secret_file (mode 600, never printed), a webhookTimestamp
  within 60 seconds, a 1 MiB body cap and Linear's source IPs when visible.
  A signed Issue or Comment for a configured Linear project upserts one
  linear_dirty row in the existing database; nothing else is done. --bind
  overrides linear_webhook.bind. Exits 2 when no webhook is configured.

health [--db PATH] [--machines PATH] [--orch PATH] [--json]
  Print each component's heartbeat (poller, waiters, Linear listener,
  tunnel, dashboard writer, device probes) as up, stale or down, with
  last-seen in ET, pid and device, and each queued harness job with no
  live waiter on an eligible device ("no waiter for <harness>"). A
  component machines.yml or orch.yml expects that has never beaten is
  down. A poller, waiter or listener whose heartbeat names another commit
  than the checkout shows "stale code". Also measures disk and inode use
  of the box's filesystems and, over SSH, of each probed device's
  workdir_root, with an inode alert at 85%. Reads orch.health.db beside
  the database. Exits 3 when a component is down, a queued job has no
  waiter or inode use is at 85% or more, 1 when orch.health.db cannot be
  read, else 0.
health forget <component> [--db PATH] [--device NAME] [--bot BOT]
  Remove a retired component's rows.

heal --health-json PATH|- [--state DIR] [--deploy DIR] [--config DIR]
     [--machines PATH] [--orch PATH] [--device NAME] [--dry-run]
  Read `orch health --json` and restart, through its own deploy/ service
  script, each component on this device that this account owns
  (~/.config/factory autostart flags) and that is down, or runs stale code
  and is idle. Never overrides a *.stopped hold; at most 3 restarts per
  component in 30 minutes. Logs to self-heal.log in the state directory
  and escalates to the Factory webhook when a component cannot be fixed or
  flaps. Never calls an LLM and never signals a job. deploy/self-heal.sh
  runs it every 2 minutes. A stale-code waiter is drained first
  (waiter-<bot>.drain: it claims nothing and confirms in
  waiter-<bot>.drain.ack) and restarted only once it has confirmed and has
  no running job.
heal deploy [--state DIR] [--checkout DIR] [--machines PATH] [--orch PATH]
            [--device NAME] [--dry-run]
  Fast-forward the checkout to origin/main (issue #219), keeping local
  edits to machines.yml, orch.yml and deploy/install.sh. Only the
  checkout's owner pulls. A conflict, local commits or other local edits
  leave the checkout unchanged and escalate once. Runs
  `deploy/install.sh --update` when deploy/ or requirements.txt changed,
  and `orch install-skills` when skills/ changed. A box holding a job is
  left on its old HEAD and retried next pass (issue #297).
  deploy/self-heal.sh runs it before each heal pass.
heal devices [--state DIR] [--db PATH] [--checkout DIR] [--machines PATH]
             [--orch PATH] [--dry-run]
  Bring each enabled remote device's factory checkout (machines.yml
  factory_root) to this checkout's HEAD on main and run install-skills
  there (issue #297). A device with a job on it is skipped and retried next
  pass; local edits on a device are never discarded (refused, escalated
  once). --dry-run prints each target's state and writes nothing.
  deploy/self-heal.sh starts it detached after each heal pass.

settings apply [--state DIR] [--orch PATH] [--machines PATH] [--deploy DIR]
               [--config DIR]
  Apply each new request in orch.yml dashboard.settings.spool (enabled,
  slot_max or drain of one device, asked for by the approver on the
  Settings page): back up machines.yml, edit that one line, restart the
  poller and this account's waiters, and append to settings-audit.jsonl in
  the state dir. Without dashboard.settings it does nothing. Exits 1 when a
  restart failed (the backup is restored) or the spool cannot be read.
  deploy/self-heal.sh runs it before each health step.

status [--db PATH] [--machines PATH] [--orch PATH]
  Print the queue, each device's slots and running jobs, Grok wakes,
  paused and stuck tickets, the feeder, the webhook and component health,
  read-only. A database without the dispatcher tables shows the empty
  state. --db defaults to ORCH_DB. Exits 1 when the database or a given
  config file cannot be read, else 0.

retry <owner/repo>#<n> --reason TEXT [--force] [--db PATH] [--machines PATH]
  Post a grok-bot kind=retry comment ("Retry requested: TEXT") on a GitHub
  ticket (owner/repo#n or its PR or issue URL) through gh with the token
  variables unset; the dispatcher then resumes the step that got stuck for
  one more attempt (docs/SPEC.md 4.2 TR). The gh login must be a
  trusted_logins login for grok-bot. With --db or $ORCH_DB readable, it
  first refuses (exit 1, nothing posted) a ticket that is not stuck or has
  a queued or running harness job; --force skips that check, and the
  dispatcher still refuses either case. A running job on a remote device
  that is disabled in --machines (default machines.yml) or failed its last
  probe is not refused: the dispatcher settles it as lost and requeues it
  (#663). Linear tickets are out of scope.
  Prints the new comment id. Exits 2 on a bad ticket or empty reason, 1
  when refused or the post fails.

trace <owner/repo>#<n> [--db PATH] [--json]
  Print one line per job attempt on the ticket (owner/repo#n or its PR or
  issue URL): job id, action and recipient, attempt, state, host, exit, the
  handoff comment, the dispatcher's result and reason (issue #1016), and the
  exits of earlier executions a requeue resumed.
  Read-only: opens --db (default $ORCH_DB) with mode=ro, no GitHub call.
  --json prints a JSON list. Exits 2 on a bad ticket, 1 when the database
  cannot be read.

usage migrate [--db PATH]
  Key the usage readings on the account (#772). Takes the labelled backup
  usage-accounts (as db-backup does) first, then adds usage_readings.account
  filled with <harness>-default and the account-keyed tables, seeding
  usage_account_readings from usage_readings. `orch poll`
  runs it at start. Says whether it migrated; a second run changes nothing.
  --db defaults to ORCH_DB. Exit 1 when the backup or the migration fails.

usage record --account ID --window five_hour|seven_day|weekly --used FRACTION
             --resets-at EPOCH [--observed-at EPOCH] [--db PATH] [--machines PATH]
  Store one usage reading for an account machines.yml declares with
  harness: external (Grok) or harness: cursor, whose usage no factory
  process can read. Claude, Codex and Gemini accounts are read at each
  job's start and are refused. --used is the used fraction, 0 to 1;
  weekly is stored as seven_day. --resets-at is whole epoch seconds
  within 30 days of now.
  --observed-at defaults to now and may be at most 300 seconds ahead.
  A reading no newer than the stored one is not written: it prints
  "kept newer reading" and exits 0. --db defaults to $ORCH_DB, --machines
  to the checkout's machines.yml. Exits 2 and writes nothing on an
  unknown account or one with an automatic source, a bad value, an
  unknown option, or a usage_readings table without the account column
  (run orch usage migrate first); 1 when the database cannot be written.

accounts prepare --device NAME [--account ID] [--machines PATH]
  Run as each bot on its device. Makes ~/.factory-accounts/<id> mode 700
  for each account machines.yml signs in there; for claude, links skills,
  agents, docs, rules and settings.json to ~/.claude. Never replaces an
  existing file or link and never reads the folder. Exits 2 when refused
  (another user, unknown device or account, home mismatch), 1 on a conflict.

accounts doctor [--db PATH] [--machines PATH] [--json]
  Ask every device, over ssh as each bot, whether each account machines.yml
  signs in there is signed in: `claude auth status`, `codex login status`
  or `cursor-agent status` under the login shell, with token variables
  unset. Never reads a token or credential file and never signs in.
  Replaces the account_signins rows and prints one line per pair,
  <account>@<device> <status> <auth method or -> <detail>; --json prints
  the rows as a JSON list. --db defaults to $ORCH_DB, --machines to the
  checkout's machines.yml. Exits 0 after a pass whatever the statuses, 2
  on a config error, 1 when the database cannot be written.

accounts signin-broker [--state DIR] [--db PATH] [--machines PATH] [--orch PATH] [--once]
  Take the dashboard's sign-in requests from the settings spool through
  dashboard.settings.relay and run each admitted attempt to one audit
  line. Prints "dashboard sign-in is off" and exits 0 when orch.yml sets
  no relay. Beats as the signin-broker health component until SIGTERM or
  SIGINT, which cancel live attempts and wait for their audit lines; --once
  makes one pass, running admitted attempts to the end.
  --state defaults to $ORCH_STATE_DIR or ~/.local/state/factory, --db to
  $ORCH_DB or orch.db in the state dir. Exits 1 on an invalid orch.yml, a
  relay host ssh -G names no user for, or a --once pass that cannot read
  the spool; a serving broker logs spool errors and retries.

wait orchestrator [--db PATH] [--claimer ID] [--lease-seconds N] [--poll-seconds N] [--once]
  Block at zero token cost until a pull wake is available in driver_inbox,
  atomically claim it with a lease token, print one JSON object and exit 0.
  With --once, exits 0 without output when no wake is queued.
wait orchestrator --autostart [--harness-config PATH] [--db PATH] [--claimer ID]
                              [--lease-seconds N] [--poll-seconds N] [--once]
  Loops: claims and acks the next wake, then runs COMMAND, the argv in
  --harness-config (default ~/.config/factory/waiter-orchestrator.yml, same
  rules as a bot's), with no shell. The prompt on stdin names
  skills/factory/orchestrator/SKILL.md; the env carries ORCH_DB,
  ORCH_WAKE_JOB_ID, ORCH_WAKE_LEASE_TOKEN and ORCH_WAKE_FILE (the claimed
  wake's JSON). Renews the lease while COMMAND runs. A wake COMMAND did not
  finish is finished on exit 0 and released for retry otherwise. --once
  handles at most one wake. SIGTERM ends a running COMMAND's session,
  releases its wake for the next runner, and exits 0.

orchestrator (ack | heartbeat | finish) <job-id> --token TOKEN [--db PATH] ...
  Acknowledge, renew the lease on, release (--release), or finish a claimed
  pull orchestrator wake. finish accepts optional telemetry flags (--harness,
  --repo, --tokens-in, --tokens-cached, --tokens-out, --cost-usd,
  --elapsed-seconds, --turns, --exit-code) to record orchestrator spend in
  job_telemetry (bot='orchestrator').
"""


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE, end="")
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    if argv[0] == "check-config":
        return _call(_check_config, argv[1:])
    if argv[0] == "install-skills":
        return _call(_install_skills, argv[1:])
    if argv[0] == "doctor":
        return _call(_doctor, argv[1:])
    if argv[0] == "doctor-runtime":
        return _call(_doctor_runtime, argv[1:])
    if argv[0] == "eval-report":
        return _call(_eval_report, argv[1:])
    if argv[0] == "log-message":
        return _call(_log_message, argv[1:])
    if argv[0] == "poll":
        return _call(_poll, argv[1:])
    if argv[0] == "qa-worker":
        return _call(_qa_worker, argv[1:])
    if argv[0] == "qa":
        return _call(_qa, argv[1:])
    if argv[0] == "init-db":
        return _call(_init_db, argv[1:])
    if argv[0] == "backup-db":
        return _call(_backup_db, argv[1:])
    if argv[0] == "db-backup":
        return _call(_db_backup, argv[1:])
    if argv[0] == "restore-db":
        return _call(_restore_db, argv[1:])
    if argv[0] == "move-db":
        return _call(_move_db, argv[1:])
    if argv[0] == "db-spill-detail":
        return _call(_db_spill_detail, argv[1:])
    if argv[0] == "wait":
        return _call(_wait, argv[1:])
    if argv[0] == "job":
        return _call(_job, argv[1:])
    if argv[0] == "dashboard":
        return _call(dashboard_main, argv[1:])
    if argv[0] == "gc":
        return _call(_gc, argv[1:])
    if argv[0] == "linear-webhook":
        return _call(_linear_webhook, argv[1:])
    if argv[0] == "health":
        return health.main(argv[1:])
    if argv[0] == "heal":
        from lib import heal

        return heal.main(argv[1:])
    if argv[0] == "cleanup":
        from lib import job_cleanup

        return job_cleanup.main(argv[1:])
    if argv[0] == "settings":
        from lib import settings_apply

        return settings_apply.main(argv[1:])
    if argv[0] == "status":
        return _call(status_main, argv[1:])
    if argv[0] == "retry":
        return _call(_retry, argv[1:])
    if argv[0] == "trace":
        return _call(_trace, argv[1:])
    if argv[0] == "usage":
        return _call(_usage, argv[1:])
    if argv[0] == "accounts":
        return _call(_accounts, argv[1:])
    if argv[0] == "orchestrator":
        return _call(_orchestrator, argv[1:])
    print(f"unknown command: {argv[0]}", file=sys.stderr)
    print(USAGE, end="", file=sys.stderr)
    return 2


def _call(command: Callable[[list[str]], int], argv: list[str]) -> int:
    try:
        return command(argv)
    except ValueError:
        return 2
    except SystemExit as exc:
        return _exit_code(exc)


def _exit_code(exc: SystemExit) -> int:
    code = exc.code
    if code is None or code == 0:
        return 0
    if isinstance(code, int):
        return code
    return 2


def _check_config(argv: list[str]) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    orch_path = args.orch if args.orch is not None else ROOT / "orch.yml"
    if args.factory:
        factory_paths = args.factory
    else:
        factory_paths = [(None, ROOT / relative) for relative in EXAMPLE_PATHS]

    orch_config, orch_findings = load_orch_yml(
        orch_path,
        allow_placeholders=args.allow_placeholders,
    )
    if orch_config is not None:
        orch_findings = (
            orch_findings
            + linear_approver_findings(orch_config)
            + bot_approver_findings(orch_config)
        )
    failed = _report(orch_path, orch_findings)
    machines_path = args.machines
    if machines_path is None and args.orch is None:
        machines_path = ROOT / "machines.yml"
    machines_config = None
    if machines_path is not None:
        machines_config, machines_findings = load_machines_yml(machines_path)
        failed = _report(machines_path, machines_findings) or failed
    if orch_config is not None and machines_config is not None:
        failed = _report(orch_path, project_device_findings(orch_config, machines_config)) or failed
    routing_path = args.routing
    if routing_path is None and (ROOT / "config" / "routing.yml").is_file():
        routing_path = ROOT / "config" / "routing.yml"
    if routing_path is not None:
        _routing_cfg, routing_findings = load_routing_yml(routing_path)
        failed = _report(routing_path, routing_findings) or failed
    for repo, path in factory_paths:
        factory, findings = load_factory_md(path)
        if orch_config is not None and factory is not None:
            findings = (
                findings
                + hold_approver_findings(orch_config, factory.hold)
                + qa_storage_findings(orch_config, repo, factory.qa)
                + refused_harness_findings(orch_config, factory)
            )
        failed = _report(path, findings) or failed
    from lib.config_overlay import describe, digest, uncommitted_edits

    for path in (orch_path, machines_path):
        if path is None:
            continue
        warning = uncommitted_edits(path)
        if warning is not None:
            _report(path, [warning])
        line = describe(path)
        if line is not None:
            print(line)
        effective = digest(path) if args.digest else None
        if effective is not None:
            print(f"effective {path.name} sha256={effective}")
    if failed:
        return 1
    print("ok")
    return 0


def _report(path: Path, findings: list[Finding]) -> bool:
    failed = False
    for finding in findings:
        print(finding.format(str(path)), file=sys.stderr)
        if finding.severity == "error":
            failed = True
    return failed


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        print(f"{self.prog}: {message}", file=sys.stderr)
        raise ValueError(message)


def _eval_report(argv: list[str]) -> int:
    parser = _Parser(prog="orch eval-report", add_help=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path)
    source.add_argument("--db", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--baseline-since")
    parser.add_argument("--baseline-until")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.db is not None:
        if (args.baseline_since is not None or args.baseline_until is not None) and args.baseline is None:
            parser.error("--baseline-since/--baseline-until need --baseline")
        try:
            current = build_metrics(args.db, since=args.since, until=args.until)
            baseline = None
            if args.baseline is not None:
                if args.baseline_since is not None or args.baseline_until is not None:
                    b_since, b_until = args.baseline_since, args.baseline_until
                else:
                    b_since, b_until = args.since, args.until
                baseline = build_metrics(args.baseline, since=b_since, until=b_until)
        except EvalError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if args.json:
            print(render_metrics_json(current, baseline), end="")
        else:
            print(render_metrics(current, baseline), end="")
            print(render_qa(current), end="")
        return 0
    if args.baseline is not None or args.baseline_since is not None or args.baseline_until is not None:
        parser.error("--baseline needs --db")
    try:
        built = build_report(args.input, since=args.since, until=args.until)
    except EvalError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for warning in built.warnings:
        print(warning, file=sys.stderr)
    print((render_json if args.json else render)(built.report), end="")
    return 0


def _log_message(argv: list[str]) -> int:
    parser = _Parser(prog="orch log-message", add_help=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr")
    parser.add_argument("--from", dest="sender", required=True)
    parser.add_argument("--to", dest="recipient", required=True)
    parser.add_argument("--kind", default="handoff")
    parser.add_argument("--log", type=Path)
    args = parser.parse_args(argv)
    log = args.log
    if log is None:
        database = os.environ.get(DB_ENV)
        if not database:
            parser.error(f"pass --log or set {DB_ENV}")
        log = Path(database).with_name("dispatcher.log")
    try:
        append_message(
            log,
            repo=args.repo,
            pr=args.pr,
            sender=args.sender,
            recipient=args.recipient,
            kind=args.kind,
            at=datetime.now(timezone.utc),
        )
    except EvalError as exc:
        print(f"orch log-message: {exc}", file=sys.stderr)
        return 2
    return 0


def _wait(argv: list[str]) -> int:
    if argv and argv[0] == "orchestrator":
        return _wait_orchestrator(argv[1:])
    parser = _Parser(prog="orch wait", add_help=True)
    parser.add_argument("bot")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--machines", type=Path)
    parser.add_argument("--autostart", action="store_true")
    parser.add_argument("--harness-config", type=Path)
    parser.add_argument("--orch", type=Path)
    parser.add_argument("--state-dir", type=Path)
    args = parser.parse_args(argv)
    if not args.autostart and (
        args.harness_config is not None or args.state_dir is not None
    ):
        print("orch wait: --harness-config and --state-dir need --autostart", file=sys.stderr)
        return 2
    try:
        recipient = recipient_for(args.bot)
        harness = (
            waiter.load_harness_command(
                args.harness_config or waiter.default_harness_config(args.bot), args.bot
            )
            if args.autostart
            else None
        )
        queue = open_queue(_db_path(args.db))
        machines = _machines_config(args.machines, required=True)
        assert machines is not None
        pool = _device_pool(queue, machines, args.machines, orch=args.orch)
    except (WaitError, waiter.WaiterError, ValueError) as exc:
        print(f"orch wait: {exc}", file=sys.stderr)
        return 2
    database = _db_path(args.db)
    try:
        # Held for this process's life (the fd is never closed): one
        # autostart waiter per bot, and no plain claim beside it (#80).
        _lock = (
            waiter.hold_waiter_lock(database, args.bot)
            if harness is not None
            else waiter.share_waiter_lock(database, args.bot)
        )
    except (waiter.WaiterError, OSError) as exc:
        print(f"orch wait: {exc}", file=sys.stderr)
        return 2
    if (retired := _retired()) is not None:
        print(
            f"orch wait: this host is retired as the orch host ({retired}); not waiting",
            file=sys.stderr,
        )
        return 79
    # Heartbeats (#92): one waiter row per device this waiter claims on.
    # An autostart waiter marks its rows stopped on exit; a plain wait,
    # which exits by design once it claims, removes them.
    beats = health.Beater(
        database,
        health.WAITER,
        devices=health.waiter_devices(machines, args.bot),
        bot=args.bot,
        on_exit="stop" if harness is not None else "delete",
    )
    if harness is not None:
        with beats:
            return _wait_autostart(args, queue, pool, harness)
    signal.signal(signal.SIGTERM, _exit_on_term)
    try:
        with beats:
            wait_for_job(queue, recipient, sys.stdout, pool=pool, keep_ignored=True)
    except WaitError as exc:
        _quiet_stdout()
        print(f"orch wait: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


def _wait_orchestrator(argv: list[str]) -> int:
    from lib import orchestrator_pull

    parser = _Parser(prog="orch wait orchestrator", add_help=True)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--claimer", type=str)
    parser.add_argument(
        "--lease-seconds", type=float, default=orchestrator_pull.DEFAULT_LEASE_SECONDS
    )
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--autostart", action="store_true")
    parser.add_argument("--harness-config", type=Path)
    args = parser.parse_args(argv)
    if args.poll_seconds <= 0:
        print("orch wait orchestrator: --poll-seconds must be positive", file=sys.stderr)
        return 2
    if args.harness_config is not None and not args.autostart:
        print("orch wait orchestrator: --harness-config needs --autostart", file=sys.stderr)
        return 2
    try:
        harness = (
            waiter.load_harness_command(
                args.harness_config or waiter.default_harness_config("orchestrator"),
                "orchestrator",
            )
            if args.autostart
            else None
        )
    except waiter.WaiterError as exc:
        print(f"orch wait orchestrator: {exc}", file=sys.stderr)
        return 2
    if (retired := _retired()) is not None:
        print(
            f"orch wait orchestrator: this host is retired as the orch host ({retired}); not waiting",
            file=sys.stderr,
        )
        return 79
    database = _db_path(args.db)
    if harness is not None:
        return _run_orchestrator(args, database, harness)
    signal.signal(signal.SIGTERM, _exit_on_term)
    try:
        orchestrator_pull.wait_for_wake(
            database,
            sys.stdout,
            claimer=args.claimer,
            lease_seconds=args.lease_seconds,
            poll_seconds=args.poll_seconds,
            once=args.once,
            keep_ignored=True,
        )
    except ValueError as exc:
        print(f"orch wait orchestrator: {exc}", file=sys.stderr)
        return 2
    except WaitError as exc:
        _quiet_stdout()
        print(f"orch wait orchestrator: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


def _run_orchestrator(
    args: argparse.Namespace, database: Path, harness: waiter.HarnessCommand
) -> int:
    from lib import orchestrator_pull, orchestrator_runner

    def log(message: str) -> None:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        print(f"{stamp} orch wait orchestrator: {message}", file=sys.stderr, flush=True)

    stop = waiter.StopFlag()
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, stop.set)
    try:
        orchestrator_runner.run_wakes(
            database,
            harness.command,
            claimer=args.claimer or orchestrator_pull.default_claimer(),
            lease_seconds=args.lease_seconds,
            poll_seconds=args.poll_seconds,
            once=args.once,
            stop=stop,
            log=log,
        )
    except ValueError as exc:
        print(f"orch wait orchestrator: {exc}", file=sys.stderr)
        return 2
    except (orchestrator_runner.RunnerError, WaitError, sqlite3.Error) as exc:
        log(str(exc))
        return 1
    return 0


def _orchestrator(argv: list[str]) -> int:
    from lib import orchestrator_pull

    if not argv or argv[0] in ("-h", "--help"):
        print(
            "usage: orch orchestrator (ack | heartbeat | finish) <job-id> --token TOKEN [--db PATH] ...",
            file=sys.stderr if not argv else sys.stdout,
        )
        return 0 if argv else 2
    sub, rest = argv[0], argv[1:]
    if sub not in ("ack", "heartbeat", "finish"):
        print(f"orch orchestrator: unknown subcommand {sub!r}", file=sys.stderr)
        return 2
    parser = _Parser(prog=f"orch orchestrator {sub}", add_help=True)
    parser.add_argument("job_id")
    parser.add_argument("--token", required=True)
    parser.add_argument("--db", type=Path)
    if sub in ("ack", "heartbeat"):
        parser.add_argument(
            "--lease-seconds", type=float, default=orchestrator_pull.DEFAULT_LEASE_SECONDS
        )
    else:
        parser.add_argument("--result", type=str)
        parser.add_argument("--release", action="store_true")
        parser.add_argument("--harness", type=str)
        parser.add_argument("--repo", type=str, default="")
        parser.add_argument("--tokens-in", type=int)
        parser.add_argument("--tokens-cached", type=int, default=0)
        parser.add_argument("--tokens-out", type=int)
        parser.add_argument("--cost-usd", type=float)
        parser.add_argument("--elapsed-seconds", type=float, default=0.0)
        parser.add_argument("--turns", type=int, default=1)
        parser.add_argument("--exit-code", type=int, default=0)
        parser.add_argument("--exit-reason", type=str, default="done")
    args = parser.parse_args(rest)
    database = _db_path(args.db)
    try:
        if sub == "ack":
            ok = orchestrator_pull.ack_wake(
                database, args.job_id, lease_token=args.token, lease_seconds=args.lease_seconds
            )
        elif sub == "heartbeat":
            ok = orchestrator_pull.heartbeat_wake(
                database, args.job_id, lease_token=args.token, lease_seconds=args.lease_seconds
            )
        else:
            usage = None
            if (
                args.harness is not None
                or args.tokens_in is not None
                or args.tokens_out is not None
                or args.cost_usd is not None
            ):
                usage = orchestrator_pull.OrchestratorUsage(
                    harness=args.harness or "orchestrator",
                    repo=args.repo,
                    tokens_in=args.tokens_in,
                    tokens_cached=args.tokens_cached,
                    tokens_out=args.tokens_out,
                    cost_usd=args.cost_usd,
                    elapsed_seconds=args.elapsed_seconds,
                    turns=args.turns,
                    exit_code=args.exit_code,
                    exit_reason=args.exit_reason,
                )
            ok = orchestrator_pull.finish_wake(
                database,
                args.job_id,
                lease_token=args.token,
                result=args.result,
                release=args.release,
                usage=usage,
            )
    except ValueError as exc:
        print(f"orch orchestrator {sub}: {exc}", file=sys.stderr)
        return 2
    except WaitError as exc:
        print(f"orch orchestrator {sub}: {exc}", file=sys.stderr)
        return 1
    if not ok:
        print(
            f"orch orchestrator {sub}: lease not active or token mismatch for {args.job_id}",
            file=sys.stderr,
        )
        return 1
    return 0


def _wait_autostart(
    args: argparse.Namespace,
    queue: SQLiteJobQueue,
    pool: DevicePool,
    harness: waiter.HarnessCommand,
) -> int:
    """Claim, start the bot's harness under `orch job run`, wait again (#80)."""
    orch_path = args.orch if args.orch is not None else ROOT / "orch.yml"
    config, findings = load_orch_yml(orch_path, allow_placeholders=True)
    errors = [item.message for item in findings if item.severity == "error"]
    note = None
    if config is None or errors:
        config = None
        note = f"{orch_path}: {errors[0] if errors else 'not loaded'}"

    def log(message: str) -> None:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        print(f"{stamp} orch wait {args.bot}: {message}", file=sys.stderr, flush=True)

    if note is not None:
        log(f"factory.md context is off: {note}")
    if config is not None:
        policy = approvals(config)
    else:
        policy, approval_findings = load_approvals(orch_path)
        if policy is None:
            print(
                f"orch wait: {orch_path}: approval settings: {approval_findings[0].message}",
                file=sys.stderr,
            )
            return 2
    try:
        run = waiter.Waiter(
            bot=args.bot,
            queue=queue,
            pool=pool,
            harness=harness,
            context=waiter.GhContextSource(config, note=note),
            state_dir=args.state_dir or waiter.default_state_dir(args.bot),
            db=_db_path(args.db),
            machines=args.machines if args.machines is not None else ROOT / "machines.yml",
            log=log,
            approvals=policy,
            drain=waiter.drain_marker(args.bot),
            orch_yml=orch_path,
            home=Path.home(),
            sweep_cleanup=True,
        )
    except (WaitError, waiter.WaiterError) as exc:
        print(f"orch wait: {exc}", file=sys.stderr)
        return 2
    stop = waiter.StopFlag()
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, stop.set)
    try:
        return run.serve(stop)
    except WaitError as exc:
        log(str(exc))
        return 1


def _qa_worker(argv: list[str]) -> int:
    parser = _Parser(prog="orch qa-worker", add_help=True)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--orch", type=Path)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--tick-seconds", type=int, default=15)
    args = parser.parse_args(argv)
    try:
        path = _db_path(args.db)
        config = _runtime_config(args.orch)
        with open(path.with_name(f"{path.name}.qa-worker.lock"), "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("orch qa-worker: another qa worker holds this database", file=sys.stderr)
                return 1
            worker = _qa_runtime(path, config, args.orch)
            while True:
                worker.tick()
                if args.once:
                    return 0
                time.sleep(args.tick_seconds)
    except (QueueError, WaitError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"orch qa-worker: {' '.join(str(exc).split())}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def _qa(argv: list[str]) -> int:
    parser = _Parser(prog="orch qa", add_help=True, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    smoke = commands.add_parser("smoke", allow_abbrev=False)
    smoke.add_argument("--repo", required=True)
    smoke.add_argument("--runner", required=True)
    smoke.add_argument("--orch", type=Path)
    args = parser.parse_args(argv)
    try:
        config = _runtime_config(args.orch)
    except ValueError as exc:
        print(f"orch qa smoke: {' '.join(str(exc).split())}", file=sys.stderr)
        return 1
    checks = qa_smoke.run(config, args.repo, args.runner)
    for check in checks:
        print(check.line())
    return 0 if all(check.ok for check in checks) else 1


def _qa_runtime(path: Path, config: OrchConfig, orch_path: Path | None = None) -> QaWorker:
    source = LivePollSource(config)
    queue = open_queue(path)
    machines = _machines_config(None, required=False)
    pool = None if machines is None else _device_pool(queue, machines, None, orch=orch_path)
    projects = {project.repo.lower(): project for project in config.projects}
    actions = source.review_actions_by_repo()

    def factory(repo: str) -> FactoryConfig:
        project = projects[repo.lower()]
        if project.factory_ref is None:
            raise ValueError(f"{repo} has no projects[].factory_ref in orch.yml")
        return source._factory_at(project.repo, project.factory_ref)

    def post(repo: str, pr: int, body: str, marker: str) -> str:
        return actions[repo.lower()].post_issue_comment_once(repo, pr, body, marker)

    def storage(repo: str) -> S3Storage:
        entry = projects[repo.lower()].qa_storage
        if entry is None:
            raise ValueError(f"{repo} has no projects[].qa_storage in orch.yml")
        return S3Storage.from_entry(entry)

    linear_key = os.environ.get(config.linear.api_key_env) if config.linear else None
    return QaWorker(
        queue,
        runners={name: qa_runners.create(name) for name in QA_RUNNERS if name in qa_runners.registered()},
        factory=factory,
        base_commit=_qa_base_commit,
        post=post,
        storage=storage,
        head=lambda repo, pr: actions[repo.lower()].pull_head(repo, str(pr)),
        max_cloud_lanes=config.qa.max_cloud_lanes,
        shares_usage_with=config.qa.shares_usage_with,
        log=lambda line: print(f"orch qa-worker: {line}", file=sys.stderr, flush=True),
        evidence={p.repo.lower(): p.qa_storage for p in config.projects if p.qa_storage},
        linear=HttpLinear(linear_key) if linear_key else None,
        place=(lambda connection, job_id, repo: True) if pool is None else pool.place_qa,
    )


def _qa_base_commit(repo: str, base: str, head: str) -> str:
    clone = worktrees.fetched_clone(worktrees.work_root(None), repo, (head,), worktrees.url_template(None))
    return merge_base(clone, head, f"refs/remotes/origin/{base}")


def _poll(argv: list[str]) -> int:
    parser = _Parser(prog="orch poll", add_help=True)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--orch", type=Path)
    parser.add_argument("--machines", type=Path)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--pidfile", type=Path)
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(line_buffering=True)
    try:
        path = _db_path(args.db)
        signal.signal(signal.SIGTERM, _exit_on_term)
        with _poll_lock(path, _pidfile_path(args.pidfile, path)):
            if (retired := _retired()) is not None:
                print(
                    f"orch poll: this host is retired as the orch host ({retired}); not polling",
                    file=sys.stderr,
                )
                return 79
            dispatcher, source = _runtime(
                path,
                args.orch,
                create=False,
                machines_path=args.machines,
            )
            try:
                try:
                    if _migrate_usage(path):
                        print(f"orch poll: migrated usage readings in {path} (#772)", file=sys.stderr)
                except (BackupError, OSError, sqlite3.Error) as exc:
                    print(
                        f"orch poll: usage migration not done ({' '.join(str(exc).split())}); "
                        "readings keep the harness-wide shape",
                        file=sys.stderr,
                    )
                pusher, push_seconds = _dashboard_pusher(path, args.orch)
                after_poll = _AfterPoll(
                    CycleBackup(
                        path,
                        default_backup_path(),
                        lambda *error: dispatcher.record_poll_error(*error),
                        every=backup_every(),
                    ),
                    pusher,
                    _merge_kick(dispatcher, args.orch),
                )
                if args.once:
                    PollLoop(dispatcher, source).run(max_polls=1, after_poll=after_poll)
                else:
                    stopper = StopAtBoundary()
                    loop = PollLoop(dispatcher, source, sleep=stopper.sleep)
                    # dashboard.push.live: watch orch.db for changes between and
                    # during cycles; stopped on the way out, so a stop starts no push.
                    watcher = pusher if isinstance(pusher, LivePusher) else None
                    if watcher is not None:
                        watcher.start()
                    # Heartbeats (#92): the poller's own row and the per-device
                    # SSH reachability probe, each in its own daemon thread.
                    beats = health.poller_beats(path, args.machines, cycle=loop.cycle_state).start()
                    push_timer = (
                        health._Loop("dashboard-push-timer", push_timer_tick(push_seconds),
                                     lambda: pusher.on_timer(push_seconds), block_signals=False)
                        if pusher is not None else None
                    )
                    if push_timer is not None:
                        push_timer.start()
                    signal.signal(signal.SIGTERM, stopper.on_term)
                    try:
                        loop.run(
                            on_error=lambda exc: dispatcher.record_poll_exception(
                                "poll", "-", exc
                            ),
                            after_poll=after_poll,
                        )
                    finally:
                        if push_timer is not None:
                            push_timer.stop()
                        if watcher is not None:
                            watcher.stop()
                        beats.stop()
            finally:
                close = getattr(source, "close", None)
                if callable(close):
                    close()
    except (
        GrokDeliveryError,
        PollCycleFailed,
        PollSourceError,
        QueueError,
        ShipError,
        WaitError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        print(f"orch poll: {' '.join(str(exc).split())}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


class _AfterPoll:
    """The poll loop's after-cycle hook: the #66 backup, the dashboard push,
    then the #219 self-deploy kick after a merge of the factory's own repo."""

    def __init__(
        self,
        backup: Callable[[], None],
        pusher: PollPusher | None,
        kick: Callable[[], None] | None = None,
    ) -> None:
        self.backup = backup
        self.pusher = pusher
        self.kick = kick
        self.cycle_clock: CycleClock | None = None

    def __call__(self) -> None:
        with self._step("backup"):
            self.backup()
        if self.pusher is not None:
            with self._step("dashboard-push"):
                self.pusher()
        if self.kick is not None:
            with self._step("self-deploy"):
                self.kick()

    def _step(self, name: str) -> AbstractContextManager[None]:
        return nullcontext() if self.cycle_clock is None else self.cycle_clock.step(name)


def _merge_kick(dispatcher: Dispatcher, orch_path: Path | None) -> Callable[[], None]:
    from lib import heal, self_deploy

    return self_deploy.MergeKick(
        lambda: dispatcher.merged_this_poll,
        repo=self_deploy.own_repo(ROOT),
        on_error=lambda exc: dispatcher.record_poll_error(
            "self-deploy", "-", f"kick:{type(exc).__name__}"
        ),
        secret_names=heal._webhook(orch_path).secret_names,
    )


def _dashboard_pusher(db_path: Path, orch_path: Path | None) -> tuple[PollPusher | None, float]:
    """The poll loop's dashboard push hook (None when push is not set) and
    its push interval in seconds.

    `_runtime` has already validated the file strictly; this reads only its
    dashboard section. Placeholders elsewhere do not matter here.
    """
    config_path = orch_path if orch_path is not None else ROOT / "orch.yml"
    config, findings = load_orch_yml(config_path, allow_placeholders=True)
    if config is None or any(item.severity == "error" for item in findings):
        return None, 0.0
    push = config.dashboard.push
    # How often a push starts while all is well: the dashboard writer's interval.
    every = (push.every_minutes or 0) * 60
    interval = every if every and not push.live else config.poll_seconds

    def log(line: str) -> None:
        print(line, file=sys.stderr, flush=True)

    pusher = poll_pusher_for(
        push,
        db=db_path,
        orch=orch_path,
        environ=os.environ,
        log=log,
        poll_seconds=config.poll_seconds,
        record=lambda ok: health.push_result(db_path, ok, interval=interval, log=log),
    )
    return pusher, float(interval)


def _init_db(argv: list[str]) -> int:
    parser = _Parser(prog="orch init-db", add_help=True)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--orch", type=Path)
    args = parser.parse_args(argv)
    try:
        _runtime(args.db, args.orch, create=True)
    except (
        GrokDeliveryError,
        PollSourceError,
        QueueError,
        ShipError,
        WaitError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        print(f"orch init-db: {' '.join(str(exc).split())}", file=sys.stderr)
        return 1
    print("ok")
    return 0


def _backup_db(argv: list[str]) -> int:
    parser = _Parser(prog="orch backup-db", add_help=True)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args(argv)
    try:
        path = _db_path(args.db)
        dest = args.backup or default_backup_path()
        backup(path, dest)
    except (BackupError, WaitError, OSError, sqlite3.Error) as exc:
        print(f"orch backup-db: {' '.join(str(exc).split())}", file=sys.stderr)
        return 1
    print(f"backed up {path} to {dest}")
    return 0


def _db_backup(argv: list[str]) -> int:
    parser = _Parser(prog="orch db-backup", add_help=True)
    parser.add_argument("label")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--dir", type=Path)
    args = parser.parse_args(argv)
    try:
        path = _db_path(args.db)
        directory = args.dir or default_backup_path().parent / "manual"
        target, pruned = labelled_backup(path, directory, args.label, datetime.now(timezone.utc))
    except (BackupError, WaitError, OSError, sqlite3.Error) as exc:
        print(f"orch db-backup: {' '.join(str(exc).split())}", file=sys.stderr)
        return 1
    print(f"backed up {path} to {target}")
    for old in pruned:
        print(f"pruned {old}")
    return 0


def _usage_migrate(args: argparse.Namespace) -> int:
    try:
        path = _db_path(args.db)
        migrated = _migrate_usage(path)
    except (BackupError, WaitError, OSError, sqlite3.Error) as exc:
        print(f"orch usage migrate: {' '.join(str(exc).split())}", file=sys.stderr)
        return 1
    if migrated:
        print(f"migrated {path}")
    else:
        print(f"{path} already migrated")
    return 0


def _migrate_usage(path: Path) -> bool:
    if not path.exists():
        raise BackupError(f"database {path} does not exist")
    return usage_readings.migrate(path, lambda: sqlite3.connect(path, timeout=30))


def _restore_db(argv: list[str]) -> int:
    """Exit 0 restored, 3 no backup, 1 the database exists or the backup is bad."""
    parser = _Parser(prog="orch restore-db", add_help=True)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args(argv)
    try:
        path = _db_path(args.db)
        source = args.backup or default_backup_path()
        taken = restore(source, path)
    except FileNotFoundError as exc:
        print(f"orch restore-db: {exc}", file=sys.stderr)
        return 3
    except (BackupError, WaitError, OSError, sqlite3.Error) as exc:
        print(f"orch restore-db: {' '.join(str(exc).split())}", file=sys.stderr)
        return 1
    print(f"restored {path} from {source} (backup taken {taken:%Y-%m-%d %H:%M:%S} UTC)")
    return 0


def _db_spill_detail(argv: list[str]) -> int:
    parser = _Parser(prog="orch db-spill-detail", add_help=True)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--dir", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--vacuum", action="store_true")
    args = parser.parse_args(argv)
    try:
        path = _db_path(args.db)
        if args.dry_run:
            connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
            try:
                _print_survey(job_detail.survey(connection))
            finally:
                connection.close()
            return 0
        with _poll_lock(path):
            held = _held_waiter_locks(path) if args.vacuum else []
            for lock in held:
                print(f"orch db-spill-detail: a waiter holds {lock}; stop it before --vacuum", file=sys.stderr)
            if held:
                return 1
            directory = args.dir or default_backup_path().parent / "manual"
            backup, pruned = labelled_backup(path, directory, "pre-spill", datetime.now(timezone.utc))
            for old in pruned:
                print(f"pruned {old}")
            _check_integrity(backup)
            connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
            try:
                inline = _inline_bytes(connection)
                by_key = job_detail.spill_existing(connection)
                cold = job_detail.spill_cold(connection)
                print(f"moved {by_key} jobs by key, {cold} cold jobs")
                print(f"moved {inline - _inline_bytes(connection)} inline bytes")
                print(f"backup: {backup}")
                print(f"restore with: orch restore-db --db {path} --backup {backup}")
                if args.vacuum:
                    before = _file_bytes(path)
                    connection.execute("VACUUM")
                    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    print(f"vacuumed {path}: {before} -> {_file_bytes(path)} bytes")
            finally:
                connection.close()
    except (BackupError, ValueError, WaitError, OSError, sqlite3.Error) as exc:
        print(f"orch db-spill-detail: {' '.join(str(exc).split())}", file=sys.stderr)
        return 1
    return 0


def _print_survey(survey: job_detail.Survey) -> None:
    for name in job_detail.CLASSES:
        share = survey.classes[name]
        print(f"{name}: {share.rows} rows, {share.inline_bytes} inline bytes")
    print(f"would move: {survey.movable_bytes} bytes")
    print(f"non-object rows: {survey.non_object}")
    print("largest inline keys:")
    width = max((len(key) for key, _size in survey.largest_keys), default=0)
    for key, size in survey.largest_keys:
        print(f"  {key:<{width}}  {size}")


def _check_integrity(backup: Path) -> None:
    try:
        connection = sqlite3.connect(f"{backup.resolve().as_uri()}?mode=ro", uri=True)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise BackupError(f"backup {backup} failed integrity_check: {exc}") from exc
    if result != [("ok",)]:
        raise BackupError(f"backup {backup} failed integrity_check: {result[0][0]}")


def _inline_bytes(connection: sqlite3.Connection) -> int:
    return connection.execute(
        "SELECT coalesce(sum(length(CAST(detail AS BLOB))), 0) FROM jobs"
    ).fetchone()[0]


def _file_bytes(database: Path) -> int:
    wal = Path(f"{database.resolve()}-wal")
    return database.stat().st_size + (wal.stat().st_size if wal.exists() else 0)


def _runtime(
    db_path: Path | None,
    orch_path: Path | None,
    *,
    create: bool,
    machines_path: Path | None = None,
) -> tuple[Dispatcher, LivePollSource]:
    config = _runtime_config(orch_path)
    if not create and not config.trusted_logins:
        raise ValueError("trusted_logins is required for polling")
    if (
        not create
        and any(project.linear_project_ids for project in config.projects)
        and not config.trusted_linear_users
    ):
        raise ValueError("trusted_linear_users is required for Linear polling")
    if (
        config.driver == "grok"
        and config.grok is None
        and any(project.grok_webhook for project in config.projects)
    ):
        raise ValueError("grok webhook env names are required for polling")
    path = _db_path(db_path)
    queue = (
        SQLiteJobQueue(path, clock=time.time)
        if create
        else open_queue(path)
    )
    trusted: dict[str, dict[str, frozenset[str]]] = {
        "github": dict(config.trusted_logins),
        "linear": dict(config.trusted_linear_users),
    }
    if create and not any(trusted.values()):
        trusted["github"]["init-db"] = frozenset({"init-db"})
    delivery = wake_delivery(config)
    t8 = T8Hook()
    source = LivePollSource(
        config,
        risks=None if create else IntakeRisks(path),
        fed_pulls=None if create else FedPulls(path),
        gh_cache=None if create else GhCacheExpiries(path),
        required_globs=None if create else RequiredGlobs(path),
    )
    devices = None
    machines = None
    if not create:
        machines = _machines_config(machines_path, required=False)
        if machines is not None:
            from lib import self_deploy

            transport = SshTransport()
            devices = DeviceMonitor(
                _device_pool(queue, machines, machines_path, orch=orch_path),
                machines,
                transport,
                machines_path=_machines_path(machines_path),
                memory=functools.partial(read_memory, transport),
                sign_ins=functools.partial(
                    account_doctor.check, queue, SshTransport(), self_deploy.own_repo(ROOT)
                ),
            )
    feeder = None
    if not create and any(project.intake is not None for project in config.projects):
        feeder = _feeder(config, queue, machines, source)
    dispatcher = Dispatcher(
        path,
        queue,
        trusted_logins=trusted,
        review_actions=source.review_actions_by_repo(),
        grok_delivery=delivery,
        projects=config.projects,
        hooks=(T7Hook(), t8),
        devices=devices,
        job_states=config.job_states,
        log=lambda line: print(f"orch poll: {line}", file=sys.stderr, flush=True),
        feeder=feeder,
        update_classifier=_classify_update,
        rebase_classifier=_rebase_check,
        approvals=approvals(config),
        refused_harnesses=config.refused_harnesses,
        ci_logs=source.github.failed_run_logs,
        ci_rerun=source.github.rerun_failed_jobs,
        ci_full_rerun=source.github.rerun_workflow_runs,
        ci_base_checks=source.github.base_checks,
        ci_runs=source.github.run_jobs,
        ci_job_log=source.github.job_log,
        ci_pool=source.github.list_runners,
        hold_carry=_hold_carry,
        qa_diff=_qa_diff,
        moved_head=_moved_head_probe,
        stack_bounds=_stack_bounds_probe,
        retarget_delta=_retarget_delta_probe,
    )
    source.error_sink = dispatcher.record_poll_error
    source.warning_sink = lambda line: print(f"orch poll: {line}", file=sys.stderr, flush=True)
    source.github.warning_sink = source.warning_sink
    source.deferral_sink = dispatcher.record_poll_deferral
    source.terminal_sink = dispatcher.record_terminal
    if config.linear_webhook is not None:
        source.linear_dirty = linear_webhook.LinearDirtyQueue(path)
        source.active_tickets = dispatcher.active_job_tickets
        source.unpolled_tickets = dispatcher.unpolled_linear_tickets
    t8.shipper = ReviewShipper(dispatcher, source)
    return dispatcher, source


def _classify_update(
    repo: str, base: str, approved: str, head: str, shared_paths: tuple[str, ...] = ()
) -> UpdateClass:
    """#130: the trivial-update check, on the waiter's bare clone of repo."""
    clone = worktrees.fetched_clone(
        worktrees.work_root(None), repo, (approved, head), worktrees.url_template(None)
    )
    return classify_update(
        clone, approved, head, f"refs/remotes/origin/{base}", shared_paths
    )


def _rebase_check(repo: str, base: str, last: str, head: str) -> RebaseScope | None:
    clone = worktrees.fetched_clone(
        worktrees.work_root(None), repo, (last, head), worktrees.url_template(None)
    )
    return rebase_scope(clone, last, head, f"refs/remotes/origin/{base}")


def _qa_diff(repo: str, base: str, since: str | None, head: str) -> HeadDiff:
    heads = (head,) if since is None else (since, head)
    clone = worktrees.fetched_clone(
        worktrees.work_root(None), repo, heads, worktrees.url_template(None)
    )
    return head_diff(clone, since, head, f"refs/remotes/origin/{base}")


def _hold_carry(repo: str, base: str, approved: str, head: str) -> MergeUpdate:
    clone = worktrees.fetched_clone(
        worktrees.work_root(None), repo, (approved, head), worktrees.url_template(None)
    )
    return merge_update(clone, approved, head, f"refs/remotes/origin/{base}")


def _moved_head_probe(repo: str, base: str, ok: str, head: str) -> MovedHead:
    clone = worktrees.fetched_clone(
        worktrees.work_root(None), repo, (ok, head), worktrees.url_template(None)
    )
    return moved_head_probe(clone, f"refs/remotes/origin/{base}", ok, head)


def _stack_bounds_probe(repo: str, old_head: str, parent_heads: tuple[str, ...]) -> str:
    clone = worktrees.fetched_clone(
        worktrees.work_root(None), repo, (old_head, *parent_heads), worktrees.url_template(None)
    )
    return stack_bounds(clone, old_head, parent_heads)


def _retarget_delta_probe(
    repo: str, base: str, onto_from: str, from_head: str, parent_head: str, head: str
) -> RetargetDelta:
    clone = worktrees.fetched_clone(
        worktrees.work_root(None),
        repo,
        (onto_from, from_head, parent_head, head),
        worktrees.url_template(None),
    )
    return retarget_delta(
        clone, onto_from, from_head, parent_head, head, f"refs/remotes/origin/{base}"
    )


def _feeder(
    config: OrchConfig,
    queue: SQLiteJobQueue,
    machines: MachinesConfig | None,
    source: LivePollSource,
) -> Feeder:
    """The backlog feeder (issue #85) for projects with an orch.yml intake."""
    linear = None
    if config.linear is not None:
        linear = LinearIntakeClient(
            source,
            {
                project_id: project.repo
                for project in config.projects
                for project_id in project.linear_project_ids
            },
        )
    return Feeder(
        queue,
        config,
        machines,
        github=GitHubIntakeClient(
            runner=source._adapter_runner,
            cleared_variables=config.secret_env_names,
            list_pages=source._gh_list,
        ),
        linear=linear,
        factory_for=source.factory_context,
        head=lambda: health.checkout_sha(ROOT),
        log=lambda line: print(f"orch poll: {line}", file=sys.stderr, flush=True),
    )


def _machines_config(path: Path | None, *, required: bool) -> MachinesConfig | None:
    """machines.yml for scheduling: placeholders allowed (never scheduled).

    With no --machines the checkout's machines.yml is used. A missing file
    is an error only when required; an invalid one always is.
    """
    machines_path = _machines_path(path)
    if not machines_path.is_file() and not required and path is None:
        return None
    config, findings = load_machines_yml(machines_path)
    errors = [item for item in findings if item.severity == "error"]
    if config is None or errors:
        detail = errors[0].message if errors else "invalid machines.yml"
        raise ValueError(f"{machines_path}: {detail}")
    return config


def _machines_path(path: Path | None) -> Path:
    return path if path is not None else ROOT / "machines.yml"


def _device_pool(
    queue: SQLiteJobQueue,
    machines: MachinesConfig,
    path: Path | None,
    *,
    orch: Path | None = None,
) -> DevicePool:
    pins = ProjectPins(orch if orch is not None else ROOT / "orch.yml")
    try:
        return retry_locked(
            lambda: DevicePool(
                queue, machines, machines_path=_machines_path(path),
                head=lambda: health.checkout_sha(ROOT),
                pins=pins,
            )
        )
    except (QueueError, sqlite3.Error, OSError) as exc:
        raise WaitError(f"cannot open the device pool: {' '.join(str(exc).split())}") from exc


def _pidfile_path(explicit: Path | None, database: Path) -> Path:
    if explicit is not None:
        return explicit
    configured = os.environ.get(PIDFILE_ENV)
    if configured:
        return Path(configured)
    return database.with_name("poller.pid")


def _retired() -> str | None:
    path = Path.home() / ".config/factory/orch-retired"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    return f"{path}: {lines[0] if lines else ''}"


def _lock_path(database: Path) -> Path:
    return Path(f"{database.resolve()}.lock")


def _held_locks(database: Path) -> list[Path]:
    poller = [] if _lock_free(_lock_path(database)) else [_lock_path(database)]
    return [*poller, *_held_waiter_locks(database)]


def _held_waiter_locks(database: Path) -> list[Path]:
    waiters = [waiter.lock_path(database, bot) for bot in OWNING_BOTS]
    return [path for path in waiters if not _lock_free(path)]


def _lock_free(path: Path) -> bool:
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return True
    with handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return True


@contextmanager
def _poll_lock(path: Path, pidfile: Path | None = None):
    """Hold the single-poller lock; while held, own the pidfile.

    The pidfile is written only after the lock is taken, so a refused second
    poller never touches it, and a stale one left by a killed poller is
    replaced. On exit it is removed only if it still names this process.
    """
    if not path.is_file():
        raise ValueError(
            f"dispatcher database {path} does not exist; run orch init-db first"
        )
    lock_path = _lock_path(path)
    handle = lock_path.open("a+", encoding="utf-8")
    written = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(
                f"another orch poll owns {lock_path}"
            ) from None
        if pidfile is not None:
            _write_pidfile(pidfile)
            written = True
        yield
    finally:
        if written and pidfile is not None:
            _remove_own_pidfile(pidfile)
        handle.close()


def _write_pidfile(pidfile: Path) -> None:
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    temporary = pidfile.with_name(f".{pidfile.name}.{os.getpid()}.tmp")
    temporary.write_text(f"{os.getpid()}\n", encoding="utf-8")
    os.replace(temporary, pidfile)


def _remove_own_pidfile(pidfile: Path) -> None:
    try:
        if pidfile.read_text(encoding="utf-8").strip() == str(os.getpid()):
            pidfile.unlink()
    except OSError:
        pass


def _runtime_config(path: Path | None) -> OrchConfig:
    config_path = path if path is not None else ROOT / "orch.yml"
    config, findings = load_orch_yml(config_path)
    errors = [item for item in findings if item.severity == "error"]
    if config is None or errors:
        detail = errors[0].message if errors else "invalid configuration"
        raise ValueError(f"{config_path}: {detail}")
    return config


def _exit_on_term(_signum: int, _frame: FrameType | None) -> None:
    raise SystemExit(143)


class StopAtBoundary:
    """`orch poll`'s SIGTERM (#107): during a cycle it waits for the cycle to
    end, so no handoff or ship is cut in half; during the sleep between
    cycles, or a second SIGTERM, exits at once. Either way the exit is 143."""

    def __init__(self, sleep: Callable[[float], None] = sleep_on_either_clock):
        self._sleep = sleep
        self.cycling = True
        self.requested = False

    def on_term(self, _signum: int, _frame: FrameType | None) -> None:
        if self.cycling and not self.requested:
            self.requested = True
            return
        raise SystemExit(143)

    def sleep(self, seconds: float) -> None:
        self.cycling = False
        if self.requested:
            raise SystemExit(143)
        self._sleep(seconds)
        self.cycling = True


def _quiet_stdout() -> None:
    """Point a broken stdout at /dev/null so exit does not print a traceback."""
    try:
        sys.stdout.flush()
    except OSError:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)


def _job(argv: list[str]) -> int:
    if argv[:1] == ["run"]:
        return _job_run(argv[1:])
    if argv[:1] == ["cancel"]:
        return _job_cancel(argv[1:])
    parser = _Parser(prog="orch job", add_help=True)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    heartbeat = commands.add_parser("heartbeat")
    done = commands.add_parser("done")
    for sub in (heartbeat, done):
        sub.add_argument("bot")
        sub.add_argument("job_id")
        sub.add_argument("--db", type=Path)
    done.add_argument("--orch", type=Path)
    outcome = done.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--handoff", dest="handoff")
    outcome.add_argument("--failed", action="store_true")
    args = parser.parse_args(argv)
    prog = f"orch job {args.command}"
    try:
        queue = open_queue(_db_path(args.db))
        owned_job(queue, args.bot, args.job_id)
    except WaitError as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return 2
    try:
        refusal = _proof_refusal(queue, args.job_id)
    except (QueueError, sqlite3.Error, OSError) as exc:
        print(f"{prog}: {' '.join(str(exc).split()) or type(exc).__name__}", file=sys.stderr)
        return 1
    if refusal is not None:
        print(f"{prog}: {refusal}", file=sys.stderr)
        return 2
    proof = Proof(job_token.presented(os.environ))
    if args.command == "done" and args.handoff != "":
        try:
            settled = _done_settled(queue, args.job_id, args.handoff)
        except (QueueError, sqlite3.Error, OSError) as exc:
            print(f"{prog}: {' '.join(str(exc).split()) or type(exc).__name__}", file=sys.stderr)
            return 1
        if settled is not None:
            code, message = settled
            print(f"{prog}: {message}" if code else message, file=sys.stderr if code else sys.stdout)
            return code
        refused = _check_handoff(queue, args, prog, proof)
        if refused is not None:
            return refused
    try:
        if args.command == "heartbeat":
            retry_locked(
                lambda: queue.heartbeat(
                    args.job_id, owner=recipient_for(args.bot), proof=proof
                )
            )
            print(f"heartbeat {args.job_id}")
            return 0
        if args.handoff == "":
            print(f"{prog}: --handoff must not be empty", file=sys.stderr)
            return 2
        job = retry_locked(
            lambda: queue.finish(
                args.job_id,
                owner=recipient_for(args.bot),
                handoff_comment_id=args.handoff,
                proof=proof,
            )
        )
        if args.handoff is not None:
            queue.clear_invalid_handoff(job.id)
    except ClaimProofError as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return 2
    except (QueueError, sqlite3.Error, OSError) as exc:
        print(f"{prog}: {' '.join(str(exc).split()) or type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"{job.state} {job.id}")
    return 0


def _check_handoff(
    queue: SQLiteJobQueue, args: argparse.Namespace, prog: str, proof: Proof
) -> int | None:
    """#191: the handoff comment must fit the job before it is done.

    None when it does. A read that fails (GitHub, orch.yml) exits 1 and
    records nothing; a handoff that does not fit is submitted-invalid,
    exits 1, and the MAX_INVALID_HANDOFFS-th fails the job for T6. An
    unavailable local_gate is recorded and the job is done (#1464)."""
    if args.handoff is None:
        return None
    try:
        with queue.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (args.job_id,)).fetchone()
            if row is None or row["state"] != "running":
                return None
            job = job_from_row(row, connection)
        config_path = args.orch if args.orch is not None else ROOT / "orch.yml"
        config, findings = load_orch_yml(config_path, allow_placeholders=True)
        errors = [item.message for item in findings if item.severity == "error"]
        if config is None or errors:
            raise ValueError(f"{config_path}: {errors[0] if errors else 'invalid configuration'}")
        expected = expected_for(job, args.bot)
        read = gh_reader()
        found = handoff_problems(expected, args.handoff, read, config.trusted_logins)
        event, checked = SUBMITTED_INVALID, None
        if not found:
            failed = _head_check_failure(queue, args, proof, config, expected, read)
            if failed is None:
                return None
            check, checked = failed
            if checked.outcome == "unavailable":
                retry_locked(
                    lambda: queue.record_head_check(
                        args.job_id, event=check.unavailable_event, comment_id=args.handoff,
                        head=checked.head, reason=checked.reason, proof=proof,
                    )
                )
                print(
                    f"{prog}: {check.name} unavailable {args.handoff}: {checked.reason}; "
                    "the ticket waits for a device that can run it",
                    file=sys.stderr,
                )
                if checked.tail:
                    print(checked.tail, file=sys.stderr)
                return None
            found, event = [f"{check.name} failed: {checked.reason}"], check.failed_event
        count, job = retry_locked(
            lambda: queue.record_invalid_handoff(
                args.job_id,
                owner=recipient_for(args.bot),
                comment_id=args.handoff,
                detail="; ".join(found),
                proof=proof,
                event=event,
            )
        )
    except ClaimProofError as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return 2
    except (ReadError, ContextUnavailable, ValueError, sqlite3.Error, OSError) as exc:
        print(f"{prog}: {' '.join(str(exc).split()) or type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"{prog}: submitted-invalid {args.handoff}: {'; '.join(found)}", file=sys.stderr)
    if checked is not None and checked.tail:
        print(checked.tail, file=sys.stderr)
    if job.state == "failed":
        print(
            f"{prog}: {count} invalid handoffs; job {job.id} is failed for T6",
            file=sys.stderr,
        )
    else:
        print(
            f"{prog}: invalid handoff {count} of {MAX_INVALID_HANDOFFS}; the job is still "
            "running: post a handoff that fits it and run orch job done again",
            file=sys.stderr,
        )
    return 1


def _head_check_failure(
    queue: SQLiteJobQueue,
    args: argparse.Namespace,
    proof: Proof,
    config: OrchConfig,
    expected: Expected,
    read: Read,
) -> tuple[HeadCheck, PreReadyResult] | None:
    """#503, #1463: a fitting ready runs the base factory.md's head checks in
    a throwaway checkout of the ready's head made from the job's worktree,
    recording each pass. None when
    every check passed or there is nothing to run."""
    ready = ready_at(expected, args.handoff, read)
    if ready is None:
        return None
    checks = checks_for(waiter.GhContextSource(config), expected.repo, ready.base)
    if not checks:
        return None
    worktree = Path(os.environ.get("ORCH_JOB_WORKTREE") or os.getcwd())
    with committed_checkout(worktree, ready.head) as checkout:
        return first_failure(
            checks,
            checkout,
            ready.head,
            check_environ(os.environ, approvals(config)),
            lambda check: retry_locked(
                lambda: queue.record_head_check(
                    args.job_id, event=check.passed_event, comment_id=args.handoff,
                    head=ready.head, proof=proof,
                )
            ),
        )


def _job_cancel(argv: list[str]) -> int:
    """`orch job cancel`: an operator's cancel of a queued job (#66).

    Not bot-owned, so it does not go through owned_job.
    """
    parser = _Parser(prog="orch job cancel", add_help=True)
    parser.add_argument("job_id")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--reason", default="operator")
    args = parser.parse_args(argv)
    prog = "orch job cancel"
    try:
        queue = open_queue(_db_path(args.db))
    except WaitError as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return 2
    try:
        with queue.connect() as connection:
            row = connection.execute(
                "SELECT state FROM jobs WHERE id = ?", (args.job_id,)
            ).fetchone()
        if row is None:
            print(f"{prog}: unknown job {args.job_id}", file=sys.stderr)
            return 2
        if row["state"] == "running":
            print(f"{prog}: job is running; stop its worker first", file=sys.stderr)
            return 1
        job = retry_locked(lambda: queue.cancel(args.job_id, args.reason))
    except (QueueError, sqlite3.Error, OSError) as exc:
        print(f"{prog}: {' '.join(str(exc).split()) or type(exc).__name__}", file=sys.stderr)
        return 1
    request = job.request
    print(
        f"cancelled {job.id} ({request.repo}#{request.ticket_id} "
        f"{request.action} -> {request.recipient})"
    )
    return 0


RETRY_HEADER = {"kind": "retry", "from": "grok-bot", "role": "orchestrator"}
_REPO = r"(?P<repo>[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+)"
_TICKET_REF = re.compile(_REPO + r"#(?P<ticket>\S+)")
_TICKET_URL = re.compile(
    r"https://github\.com/" + _REPO + r"/(?:pull|issues)/(?P<ticket>[0-9]+)(?:[/?#]\S*)?"
)


def _retry(argv: list[str]) -> int:
    prog = "orch retry"
    parser = _Parser(prog=prog, add_help=True)
    parser.add_argument("ticket")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--machines", type=Path, default=ROOT / "machines.yml")
    args = parser.parse_args(argv)
    reason = args.reason.strip()
    if not reason:
        print(f"{prog}: --reason must say why (Max's words)", file=sys.stderr)
        return 2
    match = _TICKET_REF.fullmatch(args.ticket) or _TICKET_URL.fullmatch(args.ticket)
    if match is None or not match["ticket"].isdigit():
        linear = "linear.app/" in args.ticket or match is not None
        print(
            f"{prog}: {args.ticket!r} is not owner/repo#<n> or a GitHub PR or issue URL"
            + ("; Linear tickets are out of scope, Grok posts kind=retry there" if linear else ""),
            file=sys.stderr,
        )
        return 2
    repo, number = match["repo"], int(match["ticket"])
    if not args.force:
        refusal = _retry_refusal(
            args.db or os.environ.get(DB_ENV), repo, number, prog, args.machines
        )
        if refusal is not None:
            print(f"{prog}: {repo}#{number} {refusal}", file=sys.stderr)
            return 1
    body = f"{format_header(RETRY_HEADER)}\n\nRetry requested: {reason}\n"
    try:
        comment_id = GitHubIntakeClient().post_comment(repo, number, body)
    except GitHubAdapterError as exc:
        print(f"{prog}: {exc.safe_detail}", file=sys.stderr)
        if gh_status(exc) in (401, 403):
            print(
                f"{prog}: the gh login must be a trusted_logins login for grok-bot "
                "in orch.yml",
                file=sys.stderr,
            )
        return 1
    print(f"posted kind=retry comment {comment_id} on {repo}#{number}")
    return 0


def _trace(argv: list[str]) -> int:
    prog = "orch trace"
    parser = _Parser(prog=prog, add_help=True)
    parser.add_argument("ticket")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    match = _TICKET_REF.fullmatch(args.ticket) or _TICKET_URL.fullmatch(args.ticket)
    if match is None:
        print(
            f"{prog}: {args.ticket!r} is not owner/repo#<n> or a GitHub PR or issue URL",
            file=sys.stderr,
        )
        return 2
    database = args.db or os.environ.get(DB_ENV)
    if not database:
        print(f"{prog}: no dispatcher database (pass --db or set {DB_ENV})", file=sys.stderr)
        return 1
    ticket = f"{match['repo']}#{match['ticket']}"
    try:
        rows = trace.attempts(Path(database), match["repo"], match["ticket"])
    except (sqlite3.Error, OSError) as exc:
        why = " ".join(str(exc).split()) or type(exc).__name__
        print(f"{prog}: {database}: {why}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([asdict(row) for row in rows], indent=2))
        return 0
    if not rows:
        print(f"no job attempts recorded for {ticket}")
        return 0
    for row in rows:
        fields = (
            row.job_id,
            f"{row.action}->{row.recipient}",
            f"attempt {row.attempt}",
            row.state,
            row.host,
            row.exit,
            row.comment_url or row.comment_id,
            row.result,
            row.reason,
            "; ".join(row.earlier_exits),
        )
        print("\t".join(field or "-" for field in fields))
    return 0


def _retry_refusal(
    db: Path | str | None, repo: str, number: int, prog: str, machines: Path
) -> str | None:
    path = Path(db) if db else None
    if path is None or not path.is_file():
        print(
            f"{prog}: warning: no dispatcher database (pass --db or set {DB_ENV}); "
            "posting without the stuck check",
            file=sys.stderr,
        )
        return None
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            state = (
                connection.execute(
                    "SELECT stuck, COALESCE(workflow_state, 'intake') FROM ticket_state "
                    "WHERE repo = ? AND ticket_id = ?",
                    (repo.lower(), str(number)),
                ).fetchone()
                if "ticket_state" in tables
                else None
            )
            job = (
                connection.execute(
                    "SELECT id, action, recipient, state FROM jobs "
                    "WHERE lower(repo) = ? AND ticket_id = ? "
                    f"AND state IN ('queued', 'running') AND {HARNESS_WORK} "
                    "ORDER BY created_at LIMIT 1",
                    (repo.lower(), str(number)),
                ).fetchone()
                if "jobs" in tables
                else None
            )
            lost_plan = (
                connection.execute(
                    "SELECT 1 FROM jobs JOIN job_events ON job_events.job_id = jobs.id "
                    "WHERE jobs.id = (SELECT id FROM jobs "
                    f"WHERE lower(repo) = ? AND ticket_id = ? AND {HARNESS_WORK} "
                    "AND action IN ('plan', 'build', 'review', 'fix', 'update-from-main') "
                    "ORDER BY created_at DESC, rowid DESC LIMIT 1) "
                    "AND jobs.action = 'plan' AND jobs.state = 'done' "
                    "AND job_events.event = 'handoff-unverified' "
                    "AND job_events.detail LIKE '%is not on the ticket' LIMIT 1",
                    (repo.lower(), str(number)),
                ).fetchone()
                if {"jobs", "job_events"} <= tables
                else None
            )
            placed = (
                connection.execute(
                    "SELECT slots.device, health.failing_since FROM jobs "
                    "JOIN device_slots AS slots ON slots.job_id = jobs.id "
                    "LEFT JOIN device_health AS health ON health.device = slots.device "
                    "WHERE lower(jobs.repo) = ? AND jobs.ticket_id = ? "
                    f"AND jobs.{HARNESS_WORK} AND jobs.state = 'running' "
                    "AND jobs.action IN ('plan', 'build', 'review', 'fix', 'update-from-main') "
                    "ORDER BY jobs.created_at LIMIT 1",
                    (repo.lower(), str(number)),
                ).fetchone()
                if {"jobs", "device_slots", "device_health"} <= tables
                else None
            )
        finally:
            connection.close()
    except sqlite3.Error as exc:
        print(
            f"{prog}: warning: cannot read {path} ({exc}); posting without the stuck check",
            file=sys.stderr,
        )
        return None
    if placed is not None and _retry_stranded(placed[0], placed[1], machines, prog):
        return None
    replan = state == (0, "planning") and lost_plan is not None
    if not replan and (state is None or not state[0]):
        return f"not stuck (state {state[1] if state else 'untracked'}); nothing to retry"
    if job is not None:
        return f"has job {job[0]} {job[1]} -> {job[2]} {job[3]}; nothing to retry until it ends"
    return None


def _retry_stranded(
    device_name: str, failing_since: float | None, machines: Path, prog: str
) -> bool:
    try:
        config = _machines_config(machines, required=True)
    except (ValueError, OSError) as exc:
        print(f"{prog}: warning: {exc}; checking device health alone", file=sys.stderr)
        return failing_since is not None
    device = None if config is None else config.device(device_name)
    if device is None or device.local:
        return False
    return retry_stranded(device, failing_since, time.time()) is not None


def _done_settled(
    queue: SQLiteJobQueue, job_id: str, handoff: str | None
) -> tuple[int, str] | None:
    """`orch job done` on a job that may already be settled (#80, one handoff per job).

    (0, line) when the same outcome is already recorded: a repeat is a no-op.
    (1, error) when a different outcome is recorded: nothing changes.
    None when the job may be finished here.
    """
    target = "done" if handoff is not None else "failed"
    with queue.connect() as connection:
        row = connection.execute(
            "SELECT state, handoff_comment_id FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    if row is None:
        raise QueueError(f"unknown job {job_id!r}")
    state, recorded = str(row["state"]), row["handoff_comment_id"]
    if state == target and recorded == handoff:
        return 0, f"{state} {job_id} (already recorded; nothing changed)"
    if state in ("done", "failed"):
        already = f"{state} with handoff {recorded}" if recorded else state
        return 1, (
            f"job {job_id} is already {already}; refusing a different outcome, "
            "nothing changed"
        )
    return None


def _proof_refusal(queue: SQLiteJobQueue, job_id: str) -> str | None:
    """Why this process may not write to a running job, or None when it may (#127).

    A running job takes heartbeat, done and job run only from a process
    whose ORCH_JOB_PROOF (lib/job_token.py) matches the claim's token
    (jobs.claim_token_hash) or its autostart run's token (the slot's
    run_token_hash, #96). Only hashes are compared. There is no
    process-tree check: a sandbox in its own PID namespace (Codex) cannot
    see the run, but it inherits the token.
    """
    with queue.connect() as connection:
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        slot = (
            connection.execute(
                f"SELECT * FROM {SLOTS_TABLE} WHERE job_id = ?", (job_id,)
            ).fetchone()
            if _has_slots(connection)
            else None
        )
    if row is None:
        raise QueueError(f"unknown job {job_id!r}")
    if row["state"] != "running":
        return None
    # Codex PR #82 r2 F2.4: this path never opens a DevicePool or an
    # initializing queue, so a column may predate this code; it reads as NULL.
    claim_hash = _slot_value(row, "claim_token_hash")
    run_hash = _slot_value(slot, "run_token_hash")
    token = job_token.presented(os.environ)
    if job_token.matches(token, claim_hash) or job_token.matches(token, run_hash):
        return None
    autostart = _slot_value(slot, "autostart_pid")
    if autostart is not None:
        if run_hash is None:
            why = (
                "its claim has no run token (claimed before run tokens, #96, or its "
                "orch job run has not started), so no ORCH_JOB_PROOF can finish it"
            )
        elif token is None:
            why = "this process has no ORCH_JOB_PROOF from that run"
        else:
            why = "the ORCH_JOB_PROOF this process holds is not that run's"
        return (
            f"job {job_id} was claimed by its autostart waiter (pid {autostart}), "
            "whose harness run finishes it; only that run may call orch job done, so a "
            f"bot turn must not run or post for it; {why}. Nothing changed"
        )
    if claim_hash is None and run_hash is None:
        why = (
            "its claim has no token (claimed before claim tokens, #127), "
            "so no ORCH_JOB_PROOF can prove it; T6 ends it"
        )
    elif token is None:
        why = "this process has no ORCH_JOB_PROOF"
    else:
        why = "the ORCH_JOB_PROOF this process holds is not this claim's"
    return (
        f"job {job_id} is running under a claim this process cannot prove; run it "
        f"with ORCH_JOB_PROOF set to the proof orch wait printed; {why}. Nothing changed"
    )


def _slot_value(slot: sqlite3.Row | None, name: str) -> Any:
    """A slot column, or None when there is no slot or the column predates it."""
    if slot is None or name not in slot.keys():
        return None
    return slot[name]


def _has_slots(connection: sqlite3.Connection) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (SLOTS_TABLE,)
        ).fetchone()
        is not None
    )


def _job_run(argv: list[str]) -> int:
    """`orch job run`; with --status-file, its exit status is left there (#87 F1.4).

    The waiter starts a remote run in its own session, so the run outlives a
    waiter restart; the status file is how a restarted waiter learns how the
    run ended. It is written (atomically, mode 600) whenever this returns.
    """
    own = argv[: argv.index("--")] if "--" in argv else argv
    status_file: Path | None = None
    for index, arg in enumerate(own):
        if arg == "--status-file" and index + 1 < len(own):
            status_file = Path(own[index + 1])
        elif arg.startswith("--status-file="):
            status_file = Path(arg.partition("=")[2])
    code = 1
    try:
        code = _job_run_command(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
        raise
    finally:
        if status_file is not None:
            _write_status(status_file, code)
    return code


def _write_status(path: Path, code: int) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{code}\n")
        os.replace(temporary, path)
    except OSError as exc:
        print(f"orch job run: cannot write {path}: {exc}", file=sys.stderr)


def _job_run_command(argv: list[str]) -> int:
    prog = "orch job run"
    if "--" in argv:
        split = argv.index("--")
        own, command = argv[:split], argv[split + 1 :]
    else:
        own, command = argv, []
    parser = _Parser(prog=prog, add_help=True)
    parser.add_argument("bot")
    parser.add_argument("job_id")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--machines", type=Path)
    parser.add_argument("--interval", type=float)
    parser.add_argument("--worktree", action="store_true")
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--git-url-template")
    parser.add_argument("--status-file", type=Path)  # read and written by _job_run
    parser.add_argument("--follow-grace", type=float)
    args = parser.parse_args(own)
    if not command:
        print(f"{prog}: pass the command to run after --", file=sys.stderr)
        return 2
    if args.follow_grace is not None and not args.follow_grace > 0:
        print(f"{prog}: --follow-grace must be a number above 0", file=sys.stderr)
        return 2
    try:
        database = _db_path(args.db).absolute()
        queue = open_queue(database)
        owned_job(queue, args.bot, args.job_id)
        repo, head, timeout_seconds, detail = _job_row(queue, args.job_id)
    except WaitError as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return 2
    try:
        interval = job_run.heartbeat_interval(timeout_seconds, args.interval)
    except ValueError as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return 2
    owner = recipient_for(args.bot)

    def log(message: str) -> None:
        print(f"{prog}: {message}", file=sys.stderr, flush=True)

    if sys.platform.startswith("linux"):
        oom.lower_to_floor(log)
    if args.interval is not None and interval < args.interval:
        log(f"--interval {args.interval:g} capped to {interval:g} seconds")

    sessions: list[telemetry.Session] = []  # issue #93: the run's telemetry, once it starts

    def beat() -> object:
        result = retry_locked(
            lambda: queue.heartbeat(args.job_id, owner=owner, proof=claim_proof)
        )
        for session in sessions:
            session.flush()  # the summary moves with each heartbeat
        return result

    try:
        # Codex PR #82 r2 F2.2: an autostart claim is checked before the
        # first heartbeat, read only, so a refused run leaves the job claimed
        # (and the poller's claimed-job release requeues it).
        checked_autostart = placed_run.check_autostart(queue, job_id=args.job_id, bot=args.bot)
        # #96: an autostart run proves itself to `orch job done` with a
        # fresh secret token; only its hash goes on the slot (in prepare).
        run_token, token_hash = job_token.mint() if checked_autostart is not None else (None, None)
        refusal = _proof_refusal(queue, args.job_id)
    except placed_run.PlacementError as exc:
        print(f"{prog}: {' '.join(str(exc).split())}", file=sys.stderr)
        return 2
    except (QueueError, sqlite3.Error, OSError) as exc:
        print(f"{prog}: {' '.join(str(exc).split()) or type(exc).__name__}", file=sys.stderr)
        return 1
    if refusal is not None:
        print(f"{prog}: {refusal}", file=sys.stderr)
        return 2
    claim_proof = Proof(job_token.presented(os.environ))
    proof = run_token if checked_autostart is not None else claim_proof.token
    try:
        beat()  # a job that is not running is refused first (exit 1), as before F2-02
    except ClaimProofError as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return 2
    except (QueueError, sqlite3.Error, OSError) as exc:
        print(f"{prog}: {' '.join(str(exc).split()) or type(exc).__name__}", file=sys.stderr)
        return 1
    try:
        machines = _machines_config(args.machines, required=True)
        assert machines is not None
        placed = placed_run.prepare(
            _device_pool(queue, machines, args.machines),
            machines,
            job_id=args.job_id,
            bot=args.bot,
            owner=owner,
            worktree=args.worktree,
            log=log,
            transport=SshTransport(),
            checked_autostart=checked_autostart,
            token_hash=token_hash,
            proof=claim_proof,
        )
    except (placed_run.PlacementError, WaitError, ValueError) as exc:
        print(f"{prog}: {' '.join(str(exc).split())}", file=sys.stderr)
        return 2
    except (QueueError, sqlite3.Error, OSError) as exc:
        print(f"{prog}: {' '.join(str(exc).split()) or type(exc).__name__}", file=sys.stderr)
        return 1

    job_env = {
        "ORCH_JOB_ID": args.job_id,
        "ORCH_JOB_BOT": args.bot,
        suite_slot.LIMIT_ENV: str(placed.device.test_suite_max),
    }
    # Never pass on a token this process inherited (another job's run).
    inherited = {name: value for name, value in os.environ.items() if name != job_token.PROOF_ENV}
    inherited.update({**job_env, DB_ENV: str(database), **harness_env(owner)})
    environment = account_environ(inherited, owner, placed.account_home)
    if proof is not None and not placed.remote:
        # Not sent to a remote device: it would be on the ssh command line.
        environment[job_token.PROOF_ENV] = proof
    factory_root = skill_preflight.checkout_root(placed.device, owner, ROOT)
    try:
        command = routing_argv_for_detail(command, harness=owner, detail=detail)
    except RoutingError as exc:
        log(f"role router refused job detail flags: {exc}")
    # #100 r8: the summary's scrubber knows the harness's own environment,
    # the proof included.
    session = telemetry.start_session(
        connect=queue.connect,
        database=database,
        job_id=args.job_id,
        bot=args.bot,
        repo=repo,
        command=command,
        log=log,
        environ=environment,
        worker_env=placed.remote,
        device=placed.device.name,
        account=placed.account,
        own_login=placed.account_home is None,
        guard=lambda connection: require_latest_claim(connection, args.job_id, claim_proof),
        skills_root=None if factory_root is None else f"{factory_root}/skills",
    )
    usage_reads: list = []
    if session is None:
        log("usage probe: skipped (telemetry off)")
    else:
        sessions.append(session)
        command = session.flags.argv
        binary = usage_probe.claude_binary(session.flags)
        agy = usage_probe.gemini_binary(session.flags)
        if agy is not None:
            target = placed.target
            usage_reads = [
                usage_probe.start_gemini(
                    lambda readings: session.add_usage(
                        readings, placed.account, own_login=placed.account_home is None
                    ),
                    session.clock,
                    (lambda: usage_probe.gemini_remote(placed.transport, target))
                    if target is not None
                    else (lambda: usage_probe.gemini_local(agy, account_environ(inherited, session.harness, None))),
                    log,
                )
            ]
        elif binary is None:
            log(f"usage probe: skipped ({session.flags.reason})")
        else:
            target = placed.target
            usage_reads = usage_probe.start_pass(
                usage_probe.account_probes(
                    machines, placed.device.name, session.harness, (placed.account, placed.account_home), log
                ),
                lambda readings, account, home: session.add_usage(readings, account, own_login=home is None),
                session.clock,
                (lambda home: usage_probe.claude_remote(placed.transport, target, home=home))
                if target is not None
                else (lambda home: usage_probe.claude_local(binary, account_environ(inherited, session.harness, home))),
                log,
            )
    checkout: list[worktrees.JobCheckout] = []
    box_brief = os.environ.get(waiter.BRIEF_ENV) or None
    brief: list[bytes] = []
    if args.follow_grace is not None:
        placed.grace = args.follow_grace
    remote_env = {
        **job_env,
        "ORCH_JOB_DEVICE": placed.device.name,
        # #94: with --worktree, the checkout's and the brief's remote paths
        **placed.remote_env(worktree=args.worktree, brief=box_brief is not None),
        **harness_env(owner, placed.account_home),
    }

    scratch = telemetry.state_dir(args.job_id, args.bot, database, environment) / "tmp"
    created: list[Path] = []

    def setup() -> Path | None:
        if placed.remote:
            # #128: the brief is the detached harness's stdin on the device.
            brief.append(_read_brief(box_brief) or _stdin_brief())
        else:
            for path in (scratch.parent.parent, scratch.parent, scratch):
                if not path.exists():
                    path.mkdir(mode=0o700, parents=True)
                    created.append(path)
            environment.update(dict.fromkeys(("TMPDIR", "TMP", "TEMP"), str(scratch)))
        if args.worktree and placed.remote:
            # #94: the same --worktree, on the placed device: preflight and
            # checkout there before any worker is recorded or started.
            placed.remote_checkout(
                harness=command[0],
                repo=repo,
                head=head,
                detail=detail,
                url_template=worktrees.url_template(args.git_url_template),
                brief=brief[0] if box_brief is not None else None,
            )
        made = None
        if args.worktree and not placed.remote:
            made = worktrees.prepare(
                worktrees.work_root(args.work_root),
                args.job_id,
                repo,
                head,
                url_template=worktrees.url_template(args.git_url_template),
                adopt=_retry_of(detail),
                log=log,
            )
            checkout.append(made)
            environment["ORCH_JOB_WORKTREE"] = str(made.path)
            log(f"job worktree {made.path}")
        bot_harness = telemetry.HARNESS_BY_BOT.get(args.bot)
        exposure.record(queue.connect, args.job_id, lambda: exposure.gather(
            argv=codex_sandbox.for_device(command).argv if placed.remote else command,
            harness=telemetry.stream_flags(command, bot_harness).harness or bot_harness,
            brief=brief[0] if placed.remote else exposure.brief_file(box_brief),
            host=placed.device.name,
            factory_root=factory_root,
            shell=exposure.remote_shell(placed.transport, placed.target, remote_env)
            if placed.target is not None else exposure.local_shell(environment),
            launch_dir=placed.target.job_dir if placed.target is not None
            else None if made is None else str(made.path),
        ), log)
        placed.before_start()
        return None if made is None else made.path

    def remove_containers() -> None:
        if placed.target is not None:
            if placed.follow_end is None or not placed.follow_end.worker_ended:
                log(f"job {args.job_id}: its worker on {placed.device.name} may still run, "
                    f"so its {containers.LABEL} containers are left to the waiter's sweep")
                return
            runner = containers.remote_runner(placed.transport, placed.target)
        elif placed.live_worker() is not None:
            log(f"job {args.job_id}: its worker still runs, "
                f"so its {containers.LABEL} containers are left to the waiter's sweep")
            return
        else:
            runner = containers.local_runner()
        if (failure := containers.remove(args.job_id, runner, placed.device.name)) is not None:
            log(failure)

    def teardown() -> None:
        remove_containers()
        if (failure := remove_scratch(scratch)) is not None:
            log(failure)
        for path in reversed(created):
            with contextlib.suppress(OSError):
                path.rmdir()
        if checkout:
            try:
                worktrees.finish(checkout[0], log)
            except (worktrees.WorktreeError, OSError) as exc:
                log(f"could not remove job worktree {checkout[0].path}: {exc}")

    harness = command
    if placed.remote:
        # A remote device has no orch.db (the waiter finishes its job on the
        # box), so no box state dir is added; placed.follow runs a codex
        # command by its bare name on the device's login PATH and keeps
        # each --add-dir only where the device has it (Factory, Nov 13).
        device = codex_sandbox.for_device(command)
        if list(device.argv) != list(command) or device.guarded_dirs:
            guarded = " ".join(device.guarded_dirs)
            log(f"codex on {placed.device.name}: runs as {device.argv[0]!r} from its "
                "login PATH" + (f"; --add-dir {guarded} only if it exists there"
                                if guarded else ""))
    else:
        # PR 0.3 security boundary: never grant a sandboxed `codex exec` write
        # access to the orch state dir (orch.db, app-tokens/, locks). Strip
        # ORCH_DB and ORCH_JOB_PROOF from its child environment; the waiter
        # finishes the job outside the sandbox via the ORCH-HANDOFF marker.
        if codex_sandbox.is_sandboxed_exec(command):
            environment.pop("ORCH_DB", None)
            environment.pop(job_token.PROOF_ENV, None)
            log(f"sandboxed codex exec: kept {database.parent} outside sandbox; "
                "waiter finishes job via ORCH-HANDOFF")
    code: int | None = None  # #91: how the command ended, for ended() and finish()
    budget = job_run.Budget(timeout_seconds)  # #199: the brief prints it
    try:
        if placed.remote:
            code = job_run.follow(
                lambda control: placed.follow(
                    harness,
                    remote_env,
                    brief[0],
                    control,
                    report_env=session is not None,
                    output=session.consume if session is not None else None,
                    budget=budget,
                ),
                beat=beat,
                interval=interval,
                log=log,
                setup=setup,
                teardown=teardown,
                budget=budget,
            )
        else:
            code = job_run.run(
                harness,
                beat=beat,
                interval=interval,
                log=log,
                env=environment,
                setup=setup,
                teardown=teardown,
                started=placed.started,
                open_gate=placed.open_gate,
                output=session.consume if session is not None else None,
                execed=placed.execed,
                budget=budget,
                cpu_cap=CPU_CAP,
            )
        return code
    except placed_run.CheckoutError as exc:
        reason = " ".join(str(exc).split())
        try:
            requeued = placed.requeue(reason)
        except (QueueError, sqlite3.Error, OSError) as error:
            log(f"job {args.job_id} not started: {reason}; could not requeue it: "
                f"{' '.join(str(error).split())}")
            return 1
        if not requeued:
            log(f"job {args.job_id} not started: {reason}; it changed meanwhile, not requeued")
            return 1
        log(
            f"job {args.job_id} not started: {reason}; slot released and job requeued "
            "(no round or attempt charged)"
        )
        return REQUEUED_EXIT
    except worktrees.WorktreeError as exc:
        log(str(exc))
        return 1
    except (QueueError, sqlite3.Error, OSError) as exc:
        print(f"{prog}: {' '.join(str(exc).split()) or type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        if budget.expired:
            _record_wall_clock(queue, args.job_id, budget, placed.device.name, log, claim_proof)
            if session is not None:
                session.wall_clock(budget.elapsed())
        usage_probe.join_pass(usage_reads)
        if session is not None:
            if placed.target is not None:
                session.add_rollout(usage_probe.remote_codex(
                    placed.transport, placed.target, session.harness, session.summary.thread_id,
                    home=placed.account_home,
                ))
            usage_probe.read_idle_codex(
                machines, placed.device.name, session.harness, (placed.account, placed.account_home),
                lambda found, account, home: session.add_rollout(found, account, own_login=home is None),
                (lambda home: usage_probe.codex_remote(placed.transport, placed.target, None, home=home))
                if placed.target is not None
                else (lambda home: codex_rollout.newest(home or codex_rollout.default_home(dict(inherited)))),
                log,
            )
            session.finish(code)
        placed.ended(code)  # #91: the exit reason, while the job still runs
        # A remote run's job is finished by its waiter on the box, from the
        # run's exit status and final ORCH-HANDOFF line (lib/waiter.py), so
        # only a box run is failed here.
        # #199: a box run stopped past its budget is failed too, but only
        # once no worker of it runs (run reaped its own), so T6 never
        # retries it beside a live one.
        sandboxed_marker = (
            placed.autostart and not placed.remote and codex_sandbox.is_sandboxed_exec(command)
        )
        settle = (
            (lambda: _settle_box(queue, args.job_id, owner, log, placed.live_worker, claim_proof))
            if (placed.autostart or budget.expired)
            and not placed.remote
            and (not sandboxed_marker or budget.expired)
            else None
        )
        try:
            # #91: requeues a harness that never started; otherwise settles
            # the job while its slot is held (#99 round 2).
            placed.finish(code, settle)
        except (QueueError, sqlite3.Error, OSError) as exc:
            log(f"could not release the device slot: {' '.join(str(exc).split())}")
            if settle is not None:
                settle()  # a settled or requeued job is no longer running: left alone


def _settle_box(
    queue: SQLiteJobQueue,
    job_id: str,
    owner: str,
    log: Callable[[str], None],
    live: Callable[[], int | None],
    proof: Proof,
) -> None:
    """Fail an ended box run's job for T6, unless `live` names a worker that still runs.

    #199: then the poller's dead-job check fails it once the worker is gone,
    so no retry starts beside it.
    """
    pid = live()
    if pid is not None:
        log(f"worker pid {pid} still runs; job {job_id} not failed while it runs")
        return
    _fail_unfinished(queue, job_id, owner, log, proof)


def _fail_unfinished(
    queue: SQLiteJobQueue,
    job_id: str,
    owner: str,
    log: Callable[[str], None],
    proof: Proof,
) -> None:
    """An autostart run whose command ended without `orch job done` goes to T6 now (#80)."""
    try:
        with queue.connect() as connection:
            row = connection.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None or row["state"] != "running":
            return
        retry_locked(
            lambda: queue.finish(job_id, owner=owner, handoff_comment_id=None, proof=proof)
        )
    except QueueError:
        return
    except (sqlite3.Error, OSError) as exc:
        log(f"could not fail job {job_id}: {' '.join(str(exc).split())}")
        return
    log(f"the command ended without orch job done; failed job {job_id} for T6")


def _record_wall_clock(
    queue: SQLiteJobQueue,
    job_id: str,
    budget: job_run.Budget,
    device: str,
    log: Callable[[str], None],
    proof: Proof,
) -> None:
    """#199: the `wall-clock` job event, while the job still runs. Never raises."""
    name = signal.Signals(budget.signum).name if budget.signum is not None else "a stop"
    hours = budget.elapsed() / 3600
    detail = (
        f"its command on {device} ran past its {budget.describe()}; stopped with {name} "
        f"after {budget.elapsed() / 60:.1f} minutes ({hours:.2f} slot-hours lost)"
    )

    def write() -> None:
        with queue.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            require_proof(connection, job_id, proof)
            row = connection.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is not None and row["state"] == "running":
                record_job_event(connection, job_id, job_run.WALL_CLOCK, detail, queue.now())
            connection.commit()

    try:
        retry_locked(write)
    except (QueueError, sqlite3.Error, OSError) as exc:
        log(f"could not record job {job_id}'s wall-clock stop: {' '.join(str(exc).split())}")
        return
    log(f"job {job_id}: {detail}")


def _job_row(queue: SQLiteJobQueue, job_id: str) -> tuple[str, str | None, int, str | None]:
    try:
        with queue.connect() as connection:
            row = connection.execute(
                "SELECT repo, head, timeout_seconds, detail FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
    except (QueueError, sqlite3.Error, OSError) as exc:
        raise WaitError(f"cannot read job {job_id}: {' '.join(str(exc).split())}") from exc
    if row is None:
        raise WaitError(f"unknown job {job_id!r}")
    return str(row["repo"]), row["head"], int(row["timeout_seconds"]), row["detail"]


def _stdin_brief() -> bytes:
    """This process's stdin as the remote brief, when it is a regular file (#128).

    A pipe or terminal is never read: it may never end.
    """
    try:
        if not stat.S_ISREG(os.fstat(0).st_mode):
            return b""
        data = sys.stdin.buffer.read(MAX_BRIEF_BYTES + 1)
    except (OSError, ValueError, AttributeError):
        return b""
    if len(data) > MAX_BRIEF_BYTES:
        raise placed_run.CheckoutError(f"the brief on stdin is over {MAX_BRIEF_BYTES} bytes")
    return data


def _read_brief(path: str | None) -> bytes | None:
    """The box's brief (ORCH_JOB_BRIEF), to copy beside a remote checkout (#94)."""
    if path is None:
        return None
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_BRIEF_BYTES + 1)
    except OSError as exc:
        raise placed_run.CheckoutError(
            f"cannot read the brief {path}: {' '.join(str(exc).split()) or type(exc).__name__}"
        ) from exc
    if len(data) > MAX_BRIEF_BYTES:
        raise placed_run.CheckoutError(f"the brief {path} is over {MAX_BRIEF_BYTES} bytes")
    return data


def _linear_webhook(argv: list[str]) -> int:
    parser = _Parser(prog="orch linear-webhook", add_help=True)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    serve = commands.add_parser("serve")
    serve.add_argument("--db", type=Path)
    serve.add_argument("--orch", type=Path)
    serve.add_argument("--bind")
    args = parser.parse_args(argv)
    try:
        config = _runtime_config(args.orch)
    except ValueError as exc:
        print(f"orch linear-webhook: {exc}", file=sys.stderr)
        return 1
    webhook = config.linear_webhook
    if webhook is None:
        print(
            "orch linear-webhook: linear_webhook is not configured in orch.yml",
            file=sys.stderr,
        )
        return 2
    bind = parse_bind(args.bind) if args.bind is not None else (
        webhook.host,
        webhook.port,
    )
    if bind is None:
        print("orch linear-webhook: --bind must be host:port", file=sys.stderr)
        return 2
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(line_buffering=True)
    try:
        path = _db_path(args.db)
        if not path.is_file():
            raise ValueError(
                f"dispatcher database {path} does not exist; run orch init-db first"
            )
        app = linear_webhook.WebhookApp(
            secret=linear_webhook.read_secret(webhook.secret_file),
            project_ids=frozenset(
                project_id
                for project in config.projects
                for project_id in project.linear_project_ids
            ),
            db_path=path,
            path=webhook.path,
            log=linear_webhook.utc_log,
        )
        server = linear_webhook.make_server(app, *bind)
    except (
        linear_webhook.WebhookSecretError,
        WaitError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        print(f"orch linear-webhook: {' '.join(str(exc).split())}", file=sys.stderr)
        return 1
    host, port = server.server_address[:2]
    shown = f"[{host}]" if ":" in str(host) else str(host)
    linear_webhook.utc_log(f"linear-webhook: listening on {shown}:{port}{webhook.path}")
    signal.signal(signal.SIGTERM, _exit_on_term)
    try:
        # Heartbeat (#92), and the tunnel probe when public_url is set.
        with health.listener_beats(path, webhook.public_url):
            server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0


def _gc(argv: list[str]) -> int:
    parser = _Parser(prog="orch gc", add_help=True)
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--older-than", type=float, default=worktrees.DEFAULT_GC_HOURS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not args.older_than >= 0:
        print("orch gc: --older-than must be 0 or more hours", file=sys.stderr)
        return 2
    root = worktrees.work_root(args.work_root)
    problems = worktrees.gc(
        root,
        older_than_hours=args.older_than,
        log=lambda message: print(message, flush=True),
        dry_run=args.dry_run,
    )
    return 1 if problems else 0


def _move_db(argv: list[str]) -> int:
    parser = _Parser(prog="orch move-db", add_help=True)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    drained = commands.add_parser("drained")
    drained.add_argument("--db", type=Path, required=True)
    manifest = commands.add_parser("manifest")
    manifest.add_argument("--db", type=Path, required=True)
    manifest.add_argument("--out", type=Path, required=True)
    manifest.add_argument("--source-host", required=True)
    manifest.add_argument("--to", required=True)
    manifest.add_argument("--retired", required=True)
    check = commands.add_parser("check")
    check.add_argument("--db", type=Path, required=True)
    check.add_argument("--manifest", type=Path, required=True)
    unlocked = commands.add_parser("unlocked")
    unlocked.add_argument("--db", type=Path, required=True)
    merge = commands.add_parser("merge-detail")
    merge.add_argument("--from", dest="source", type=Path, required=True)
    merge.add_argument("--to", dest="target", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "unlocked":
        held = _held_locks(args.db)
        for path in held:
            role = "poller" if path == _lock_path(args.db) else "waiter"
            print(f"orch move-db unlocked: a {role} still holds {path}", file=sys.stderr)
        return 1 if held else 0
    try:
        if args.command == "drained":
            facts = db_facts(args.db)
            print(f"running jobs {facts.running_jobs}, held device slots {facts.held_slots}")
            return 0 if facts.running_jobs == 0 and facts.held_slots == 0 else 1
        if args.command == "merge-detail":
            merge_job_detail(args.source, args.target)
            return 0
        if args.command == "check":
            facts = check_manifest(args.db, args.manifest)
            print(f"{args.db} matches {args.manifest}; integrity ok; row counts:")
            for name, count in facts.tables.items():
                print(f"  {name} {count}")
            return 0
        write_manifest(
            args.db, args.out, source_host=args.source_host, to=args.to, retired=args.retired
        )
    except (BackupError, OSError) as exc:
        print(f"orch move-db {args.command}: {exc}", file=sys.stderr)
        return 2 if args.command == "drained" else 1
    print(f"wrote {args.out}")
    return 0


def _usage(argv: list[str]) -> int:
    parser = _Parser(prog="orch usage", add_help=True, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    record = commands.add_parser("record", allow_abbrev=False)
    record.add_argument("--account", required=True)
    record.add_argument("--window", required=True)
    record.add_argument("--used", required=True)
    record.add_argument("--resets-at", required=True)
    record.add_argument("--observed-at")
    record.add_argument("--db", type=Path)
    record.add_argument("--machines", type=Path)
    migrate = commands.add_parser("migrate", allow_abbrev=False)
    migrate.add_argument("--db", type=Path)
    args, unknown = parser.parse_known_args(argv)
    prog = f"orch usage {args.command}"
    if unknown:
        print(f"{prog}: refused: unknown arguments (not echoed)", file=sys.stderr)
        return 2
    if args.command == "migrate":
        return _usage_migrate(args)
    try:
        machines = _machines_config(args.machines, required=True)
        assert machines is not None
        reading = usage_record.parse(
            machines,
            account=args.account,
            window=args.window,
            used=args.used,
            resets_at=args.resets_at,
            observed_at=args.observed_at,
            now=time.time(),
        )
        written = usage_record.store(_db_path(args.db), reading)
    except (WaitError, ValueError) as exc:
        print(f"{prog}: refused: {' '.join(str(exc).split())}", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"{prog}: {' '.join(str(exc).split()) or type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"recorded {reading.account} {reading.window}" if written else "kept newer reading")
    return 0


def _accounts(argv: list[str]) -> int:
    if argv[:1] == ["signin-broker"]:
        from lib import signin_broker

        return signin_broker.main(argv[1:])
    parser = _Parser(prog="orch accounts", add_help=True, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    prepare = commands.add_parser("prepare", allow_abbrev=False)
    prepare.add_argument("--device", required=True)
    prepare.add_argument("--account")
    prepare.add_argument("--machines", type=Path)
    prepare.set_defaults(run=_accounts_prepare)
    doctor = commands.add_parser("doctor", allow_abbrev=False)
    doctor.add_argument("--db", type=Path)
    doctor.add_argument("--machines", type=Path)
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(run=_accounts_doctor)
    args = parser.parse_args(argv)
    return args.run(args, f"orch accounts {args.command}")


def _accounts_prepare(args: argparse.Namespace, prog: str) -> int:
    try:
        machines = _machines_config(args.machines, required=True)
        assert machines is not None
        changes = account_home.prepare(
            machines, args.device, getpass.getuser(), Path.home(), args.account
        )
    except ValueError as exc:
        print(f"{prog}: refused: {' '.join(str(exc).split())}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return 1
    for change in changes:
        print(f"{change.action} {change.path}" + (f" ({change.detail})" if change.detail else ""))
    if not changes:
        print("ok: nothing to change")
    return 1 if any(change.action == "conflict" for change in changes) else 0


def _accounts_doctor(args: argparse.Namespace, prog: str) -> int:
    from lib import self_deploy

    try:
        path = _db_path(args.db)
        machines = _machines_config(args.machines, required=True)
        assert machines is not None
        with contextlib.closing(sqlite3.connect(path, timeout=30, isolation_level=None)) as connection:
            rows = account_doctor.run(
                connection, machines, SshTransport(), time.time(), self_deploy.own_repo(ROOT)
            )
    except (ValueError, WaitError) as exc:
        print(f"{prog}: refused: {' '.join(str(exc).split())}", file=sys.stderr)
        return 2
    except (sqlite3.Error, OSError) as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([asdict(row) for row in rows]))
        return 0
    for row in rows:
        print(f"{row.account}@{row.device} {row.status} {row.auth_method or '-'} {row.detail}".rstrip())
    return 0


def _db_path(path: Path | None) -> Path:
    if path is not None:
        return path
    env = os.environ.get(DB_ENV)
    if not env:
        raise WaitError(f"pass --db or set {DB_ENV} to the dispatcher database")
    return Path(env)


def _parser() -> _Parser:
    parser = _Parser(prog="orch check-config", add_help=True)
    parser.add_argument("--orch", type=Path)
    parser.add_argument("--machines", type=Path)
    parser.add_argument("--factory", type=_factory_arg, action="append")
    parser.add_argument("--routing", type=Path)
    parser.add_argument("--allow-placeholders", action="store_true")
    parser.add_argument("--digest", action="store_true")
    return parser


_FACTORY_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def _factory_arg(value: str) -> tuple[str | None, Path]:
    repo, sep, rest = value.partition("=")
    if sep and _FACTORY_REPO.fullmatch(repo):
        return repo, Path(rest)
    return None, Path(value)


def _install_skills(argv: list[str]) -> int:
    parser = _Parser(prog="orch install-skills", add_help=True)
    parser.add_argument("--home", type=Path)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--cloud", action="store_true")
    parser.add_argument("--allow-unstable-checkout", action="store_true")
    parser.add_argument("--remove-claude-extras", action="store_true")
    args = parser.parse_args(argv)
    try:
        anchor, mode = _anchor("install-skills", args.home, args.project, args.cloud)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.remove_claude_extras:
        if mode != "local":
            print("orch install-skills: --remove-claude-extras is for a local install", file=sys.stderr)
            return 2
        errors = remove_claude_extras(anchor, rules_text(ROOT))
    else:
        unstable = unstable_checkout(ROOT) if mode == "local" and not args.allow_unstable_checkout else None
        if unstable:
            print(f"orch install-skills: {unstable}", file=sys.stderr)
            return 1
        errors = install_skills(ROOT, anchor, mode, rules=rules_text(ROOT) if mode == "local" else None)
    if errors:
        for message in errors:
            print(message, file=sys.stderr)
        return 1
    print("ok")
    return 0


def _doctor(argv: list[str]) -> int:
    parser = _Parser(prog="orch doctor", add_help=True)
    parser.add_argument("--home", type=Path)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--cloud", action="store_true")
    parser.add_argument("--responses", type=Path)
    parser.add_argument("--print-canary", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--machines", type=Path)
    parser.add_argument("--orch", type=Path)
    parser.add_argument("--harness", action="append", choices=[d.name for d in all_drivers()])
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--work", type=Path)
    args = parser.parse_args(argv)
    if args.print_canary and args.responses is not None:
        print("orch doctor: --print-canary does not take --responses", file=sys.stderr)
        return 2
    live_only = [flag for flag in ("machines", "orch", "harness", "timeout", "work")
                 if getattr(args, flag) is not None]
    if args.live and (args.print_canary or args.responses is not None or args.cloud):
        print("orch doctor: --live does not take --responses, --print-canary or --cloud",
              file=sys.stderr)
        return 2
    if live_only and not args.live:
        print(f"orch doctor: --{live_only[0]} needs --live", file=sys.stderr)
        return 2
    try:
        anchor, mode = _anchor("doctor", args.home, args.project, args.cloud)
        live = _live_canary(args) if args.live else None
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    code, prompt, errors = doctor(
        ROOT,
        anchor,
        mode,
        args.responses,
        print_canary=args.print_canary,
        live=live,
    )
    if prompt and not args.live:
        print(prompt, end="")
    for message in errors:
        print(message, file=sys.stderr)
    if code == 0 and not args.print_canary:
        print("ok")
    return code


def _live_canary(args: argparse.Namespace) -> Callable[[str], list[str]]:
    machines = _machines_config(args.machines, required=True)
    orch_path = args.orch if args.orch is not None else ROOT / "orch.yml"
    config, findings = load_orch_yml(orch_path, allow_placeholders=True)
    errors = [item.message for item in findings if item.severity == "error"]
    if config is None or errors:
        raise ValueError(f"{orch_path}: {errors[0] if errors else 'not loaded'}")
    run_id = f"doctor-{secrets.token_hex(4)}"
    work = args.work if args.work is not None else Path(tempfile.gettempdir()) / "orch-doctor"
    return lambda token: doctor_live.live_canary(
        machines, config.refused_harnesses, token, harnesses=args.harness,
        timeout=args.timeout if args.timeout is not None else skill_probe.DEFAULT_TIMEOUT,
        work=work, run_id=run_id,
    )


def _doctor_runtime(argv: list[str]) -> int:
    parser = _Parser(prog="orch doctor-runtime", add_help=True)
    parser.parse_args(argv)
    errors: list[str] = []
    try:
        __import__("yaml")
    except ImportError:
        errors.append("PyYAML is not importable")
    expected_orch = Path.home() / ".local/bin/orch"
    orch = shutil.which("orch")
    if orch is None:
        errors.append("orch is not on PATH")
    elif Path(orch).resolve() != expected_orch.resolve():
        errors.append(f"orch on PATH is not {expected_orch}")
    configured = os.environ.get(DB_ENV)
    if not configured:
        errors.append(f"{DB_ENV} is not set")
    else:
        database = Path(configured)
        if not database.is_file():
            errors.append(f"{DB_ENV} database does not exist")
        else:
            try:
                with sqlite3.connect(database) as connection:
                    connection.execute("SELECT 1").fetchone()
                    tables = {
                        str(row[0])
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        )
                    }
                    if not {"jobs", "handled_comments"} <= tables:
                        errors.append(
                            f"{DB_ENV} database is missing runtime tables"
                        )
            except sqlite3.Error:
                errors.append(f"{DB_ENV} database is not valid SQLite")
    for error in errors:
        print(f"orch doctor-runtime: {error}", file=sys.stderr)
    if errors:
        return 1
    print("ok")
    return 0


def _anchor(
    command: str,
    home: Path | None,
    project: Path | None,
    cloud: bool,
) -> tuple[Path, Mode]:
    if cloud and home is not None:
        raise ValueError(f"orch {command}: --home is the local install; use --project with --cloud")
    if not cloud and project is not None:
        raise ValueError(f"orch {command}: --project is the cloud install; pass --cloud")
    if cloud:
        anchor = project if project is not None else Path.cwd()
        mode: Mode = "cloud"
        return anchor, mode
    anchor = home if home is not None else Path.home()
    mode = "local"
    return anchor, mode
