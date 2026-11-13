"""Factory command-line entry point."""

from __future__ import annotations

import argparse
import fcntl
import os
import shutil
import signal
import sqlite3
import sys
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import Any

from lib import job_run, linear_webhook, placed_run, waiter, worktrees
from lib.dashboard_cli import main as dashboard_main
from lib.dashboard_push import LivePusher, PollPusher, poll_pusher_for
from lib.dispatcher import Dispatcher, PollLoop, ReviewShipper, ShipError
from lib.eval_report import EvalError, build_report, render
from lib.factory_md import EXAMPLE_PATHS, load_factory_md
from lib.grok_delivery import GrokDeliveryError, WebhookGrokDelivery
from lib.hook_t7 import T7Hook
from lib.hook_t8 import T8Hook
from lib.load import Finding
from lib.device_monitor import DeviceMonitor
from lib.feeder import Feeder
from lib.intake_live import GitHubIntakeClient, LinearIntakeClient
from lib.device_pool import DevicePool
from lib.machines_yml import MachinesConfig, load_machines_yml
from lib.orch_yml import OrchConfig, load_orch_yml, parse_bind
from lib.poll_source import LivePollSource, PollSourceError
from lib.pstack_pin import Mode, doctor, install_skills
from lib.queue import SLOTS_TABLE, QueueError, SQLiteJobQueue
from lib.remote import SshTransport
from lib.wait import (
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
  install-skills   install the pinned pstack skills
  doctor           canary the pinned poteto-mode skill
  doctor-runtime   check PyYAML and the shared dispatcher database
  eval-report      weekly eval report from a local log directory
  poll             poll configured trackers and dispatch work
  init-db          create and initialize the dispatcher database
  wait             block until the dispatcher queues a job for a bot
  job              run, heartbeat or finish a job that wait returned
  gc               remove stale job worktrees with no changes
  dashboard        write (and optionally push) the read-only status snapshot
  linear-webhook   serve the Linear webhook listener that marks issues dirty

check-config [--orch PATH] [--machines PATH] [--factory PATH ...]
             [--allow-placeholders]
  With no paths, checks orch.yml, machines.yml and the example factory.md
  files. When --orch is given, machines.yml is checked only with --machines.
  --allow-placeholders permits and reports REPLACE_WITH_ install values only.
  A disabled or placeholder device is named on stderr and never scheduled.
  An unknown model name warns on stderr and still exits 0.

install-skills [--home PATH] [--project PATH] [--cloud]
  Symlink each pinned skill into ~/.claude/skills, ~/.agents/skills,
  and ~/.cursor/skills. --cloud copies them into the project at
  .claude/skills, .agents/skills, and .cursor/skills.

doctor [--home PATH] [--project PATH] [--cloud]
       [--responses DIR] [--print-canary]
  Print a canary that asks the harness to name poteto-mode and quote
  a token from the pinned skill. Score the replies the owning bots
  write. Exit 1 if any harness did not load that version.
  --print-canary prints the prompt and does not read replies.

eval-report --input PATH [--since TIME] [--until TIME]
  Print the weekly eval report for one directory of fixtures or
  collected logs. It does not contact GitHub or Linear.

poll [--db PATH] [--orch PATH] [--machines PATH] [--once] [--pidfile PATH]
  Poll GitHub and Linear, apply dispatcher transitions, and deliver work.
  --once runs one complete poll cycle. --db defaults to $ORCH_DB.
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

wait <bot> [--db PATH] [--machines PATH]
  <bot> is claude-bot, codex-bot, or cursor-bot. Blocks until the
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
        [--status-file PATH] -- COMMAND [ARG ...]
  Run COMMAND (the harness) with an orch-owned heartbeat: one heartbeat
  before it starts (a job that is not running is refused and nothing
  runs), then one every --interval seconds (default 15, or a third of the
  job's timeout_seconds if shorter; an override is capped at 30 seconds
  and at a third of the timeout, so a live job never shows as stale). The heartbeat stops when COMMAND exits, when the job
  is no longer running, or when this process dies. SIGTERM and SIGHUP are
  forwarded to COMMAND; a Ctrl-C reaches COMMAND through the process group.
  A signal before COMMAND starts means it never runs. Exits with COMMAND's
  status; never finishes the job. COMMAND gets ORCH_JOB_ID, ORCH_JOB_BOT and ORCH_DB.
  The job must hold the device slot orch wait claimed for this bot. On a
  remote device COMMAND runs there as the bot's user over the device's
  ssh_alias (ssh -l <bot> -- <alias>), under its login shell (bash -lc),
  in <workdir_root>/jobs/<job-id>, with ORCH_JOB_ID, ORCH_JOB_BOT,
  ORCH_JOB_DEVICE and ORCH_JOB_WORKDIR; stdin is forwarded. When COMMAND
  ends the slot is released once the worker is confirmed gone (a remote
  worker still running is sent SIGTERM first); otherwise it stays held for
  the poller to confirm.
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
job heartbeat <bot> <job-id> [--db PATH]
job done <bot> <job-id> (--handoff COMMENT-ID | --failed) [--db PATH]
  heartbeat keeps a running job out of the T6 timeout. done finishes it
  with the handoff comment ID; --failed hands it to T6. Repeating the
  same done is a no-op.

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
    if argv[0] == "poll":
        return _call(_poll, argv[1:])
    if argv[0] == "init-db":
        return _call(_init_db, argv[1:])
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
        factory_paths = [ROOT / relative for relative in EXAMPLE_PATHS]

    failed = _report(
        orch_path,
        load_orch_yml(
            orch_path,
            allow_placeholders=args.allow_placeholders,
        )[1],
    )
    machines_path = args.machines
    if machines_path is None and args.orch is None:
        machines_path = ROOT / "machines.yml"
    if machines_path is not None:
        failed = _report(
            machines_path,
            load_machines_yml(
                machines_path,
                allow_placeholders=args.allow_placeholders,
            )[1],
        ) or failed
    for path in factory_paths:
        failed = _report(path, load_factory_md(path)[1]) or failed
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
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--since")
    parser.add_argument("--until")
    args = parser.parse_args(argv)
    try:
        built = build_report(args.input, since=args.since, until=args.until)
    except EvalError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for warning in built.warnings:
        print(warning, file=sys.stderr)
    print(render(built.report), end="")
    return 0


def _wait(argv: list[str]) -> int:
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
        args.harness_config is not None or args.orch is not None or args.state_dir is not None
    ):
        print("orch wait: --harness-config, --orch and --state-dir need --autostart", file=sys.stderr)
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
        pool = _device_pool(queue, machines)
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
    if harness is not None:
        return _wait_autostart(args, queue, pool, harness)
    signal.signal(signal.SIGTERM, _exit_on_term)
    try:
        wait_for_job(queue, recipient, sys.stdout, pool=pool)
    except WaitError as exc:
        _quiet_stdout()
        print(f"orch wait: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
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
            dispatcher, source = _runtime(
                path,
                args.orch,
                create=False,
                machines_path=args.machines,
            )
            after_poll = _dashboard_pusher(path, args.orch)
            loop = PollLoop(dispatcher, source)
            if args.once:
                loop.run(max_polls=1, after_poll=after_poll)
            else:
                # dashboard.push.live: watch orch.db for changes between and
                # during cycles; stopped on the way out, so a stop starts no push.
                watcher = after_poll if isinstance(after_poll, LivePusher) else None
                if watcher is not None:
                    watcher.start()
                try:
                    loop.run(
                        on_error=lambda exc: dispatcher.record_poll_error(
                            "poll",
                            "-",
                            type(exc).__name__,
                        ),
                        after_poll=after_poll,
                    )
                finally:
                    if watcher is not None:
                        watcher.stop()
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
        print(f"orch poll: {' '.join(str(exc).split())}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


def _dashboard_pusher(db_path: Path, orch_path: Path | None) -> PollPusher | None:
    """The poll loop's dashboard push hook, or None when push is not set.

    `_runtime` has already validated the file strictly; this reads only its
    dashboard section. Placeholders elsewhere do not matter here.
    """
    config_path = orch_path if orch_path is not None else ROOT / "orch.yml"
    config, findings = load_orch_yml(config_path, allow_placeholders=True)
    if config is None or any(item.severity == "error" for item in findings):
        return None
    return poll_pusher_for(
        config.dashboard.push,
        db=db_path,
        orch=orch_path,
        environ=os.environ,
        log=lambda line: print(line, file=sys.stderr, flush=True),
        poll_seconds=config.poll_seconds,
    )


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
        and any(
            project.linear_project_id is not None
            for project in config.projects
        )
        and not config.trusted_linear_users
    ):
        raise ValueError("trusted_linear_users is required for Linear polling")
    if config.grok is None:
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
    cleared = [config.grok.webhook_url_env, config.grok.webhook_key_env]
    if config.linear is not None:
        cleared.append(config.linear.api_key_env)
    delivery = WebhookGrokDelivery(
        url_env=config.grok.webhook_url_env,
        key_env=config.grok.webhook_key_env,
        auth_env=config.grok.webhook_auth_env,
    )
    t8 = T8Hook()
    source = LivePollSource(config)
    devices = None
    machines = None
    if not create:
        machines = _machines_config(machines_path, required=False)
        if machines is not None:
            devices = DeviceMonitor(
                _device_pool(queue, machines),
                machines,
                SshTransport(),
            )
    feeder = None
    if not create and any(project.intake is not None for project in config.projects):
        feeder = _feeder(config, queue, machines, source, cleared)
    dispatcher = Dispatcher(
        path,
        queue,
        trusted_logins=trusted,
        review_actions=source.review_actions_by_repo(),
        grok_delivery=delivery,
        hooks=(T7Hook(), t8),
        devices=devices,
        job_states=config.job_states,
        log=lambda line: print(f"orch poll: {line}", file=sys.stderr, flush=True),
        feeder=feeder,
    )
    source.error_sink = dispatcher.record_poll_error
    source.deferral_sink = dispatcher.record_poll_deferral
    source.terminal_sink = dispatcher.record_terminal
    if config.linear_webhook is not None:
        source.linear_dirty = linear_webhook.LinearDirtyQueue(path)
        source.active_tickets = dispatcher.active_job_tickets
        source.unpolled_tickets = dispatcher.unpolled_linear_tickets
    t8.shipper = ReviewShipper(dispatcher, source)
    return dispatcher, source


def _feeder(
    config: OrchConfig,
    queue: SQLiteJobQueue,
    machines: MachinesConfig | None,
    source: LivePollSource,
    cleared: list[str],
) -> Feeder:
    """The backlog feeder (issue #85) for projects with an orch.yml intake."""
    linear = None
    if config.linear is not None:
        linear = LinearIntakeClient(
            source,
            {
                project.linear_project_id: project.repo
                for project in config.projects
                if project.linear_project_id is not None
            },
        )
    return Feeder(
        queue,
        config,
        machines,
        github=GitHubIntakeClient(cleared_variables=cleared),
        linear=linear,
        factory_for=source.factory_context,
        log=lambda line: print(f"orch poll: {line}", file=sys.stderr, flush=True),
    )


def _machines_config(path: Path | None, *, required: bool) -> MachinesConfig | None:
    """machines.yml for scheduling: placeholders allowed (never scheduled).

    With no --machines the checkout's machines.yml is used. A missing file
    is an error only when required; an invalid one always is.
    """
    machines_path = path if path is not None else ROOT / "machines.yml"
    if not machines_path.is_file() and not required and path is None:
        return None
    config, findings = load_machines_yml(machines_path, allow_placeholders=True)
    errors = [item for item in findings if item.severity == "error"]
    if config is None or errors:
        detail = errors[0].message if errors else "invalid machines.yml"
        raise ValueError(f"{machines_path}: {detail}")
    return config


def _device_pool(queue: SQLiteJobQueue, machines: MachinesConfig) -> DevicePool:
    try:
        return retry_locked(lambda: DevicePool(queue, machines))
    except (QueueError, sqlite3.Error, OSError) as exc:
        raise WaitError(f"cannot open the device pool: {' '.join(str(exc).split())}") from exc


def _pidfile_path(explicit: Path | None, database: Path) -> Path:
    if explicit is not None:
        return explicit
    configured = os.environ.get(PIDFILE_ENV)
    if configured:
        return Path(configured)
    return database.with_name("poller.pid")


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
    lock_path = path.with_name(path.name + ".lock")
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
    parser = _Parser(prog="orch job", add_help=True)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    heartbeat = commands.add_parser("heartbeat")
    done = commands.add_parser("done")
    for sub in (heartbeat, done):
        sub.add_argument("bot")
        sub.add_argument("job_id")
        sub.add_argument("--db", type=Path)
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
    try:
        if args.command == "heartbeat":
            retry_locked(
                lambda: queue.heartbeat(args.job_id, owner=recipient_for(args.bot))
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
            )
        )
    except (QueueError, sqlite3.Error, OSError) as exc:
        print(f"{prog}: {' '.join(str(exc).split()) or type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"{job.state} {job.id}")
    return 0


def _done_settled(
    queue: SQLiteJobQueue, job_id: str, handoff: str | None
) -> tuple[int, str] | None:
    """`orch job done` on a job that may already be settled (#80, one handoff per job).

    (0, line) when the same outcome is already recorded: a repeat is a no-op.
    (1, error) when a different outcome is recorded: nothing changes.
    (2, error) when the job is an autostart waiter's and this process is not
    in its `orch job run`'s process tree: a bot turn must not finish it.
    None when the job may be finished here.
    """
    target = "done" if handoff is not None else "failed"
    with queue.connect() as connection:
        row = connection.execute(
            "SELECT state, handoff_comment_id FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        slot = (
            connection.execute(
                f"SELECT * FROM {SLOTS_TABLE} WHERE job_id = ?", (job_id,)
            ).fetchone()
            if _has_slots(connection)
            else None
        )
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
    # Codex PR #82 r2 F2.4: this path never opens a DevicePool, so the slots
    # table may predate the autostart columns (an update while a job runs).
    # A missing column reads as NULL: no autostart waiter can have claimed a
    # job in a table it could not record itself in.
    autostart = _slot_value(slot, "autostart_pid")
    if state != "running" or autostart is None:
        return None
    runner = _slot_value(slot, "runner_pid")
    if runner is not None and worktrees.pid_alive(
        int(runner), _slot_value(slot, "runner_start")
    ) and worktrees.descends_from(os.getpid(), int(runner)):
        return None
    return 2, (
        f"job {job_id} was claimed by its autostart waiter (pid {autostart}), "
        "whose harness run finishes it; only that run may call orch job done, so a bot "
        "turn must not run or post for it. Nothing changed"
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
    args = parser.parse_args(own)
    if not command:
        print(f"{prog}: pass the command to run after --", file=sys.stderr)
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

    if args.interval is not None and interval < args.interval:
        log(f"--interval {args.interval:g} capped to {interval:g} seconds")

    def beat() -> object:
        return retry_locked(lambda: queue.heartbeat(args.job_id, owner=owner))

    try:
        # Codex PR #82 r2 F2.2: an autostart claim is checked before the
        # first heartbeat, read only, so a refused run leaves the job claimed
        # (and the poller's claimed-job release requeues it).
        checked_autostart = placed_run.check_autostart(queue, job_id=args.job_id, bot=args.bot)
    except placed_run.PlacementError as exc:
        print(f"{prog}: {' '.join(str(exc).split())}", file=sys.stderr)
        return 2
    except (sqlite3.Error, OSError) as exc:
        print(f"{prog}: {' '.join(str(exc).split()) or type(exc).__name__}", file=sys.stderr)
        return 1
    try:
        beat()  # a job that is not running is refused first (exit 1), as before F2-02
    except (QueueError, sqlite3.Error, OSError) as exc:
        print(f"{prog}: {' '.join(str(exc).split()) or type(exc).__name__}", file=sys.stderr)
        return 1
    try:
        machines = _machines_config(args.machines, required=True)
        assert machines is not None
        placed = placed_run.prepare(
            _device_pool(queue, machines),
            machines,
            job_id=args.job_id,
            bot=args.bot,
            owner=owner,
            worktree=args.worktree,
            log=log,
            transport=SshTransport(),
            checked_autostart=checked_autostart,
        )
    except (placed_run.PlacementError, WaitError, ValueError) as exc:
        print(f"{prog}: {' '.join(str(exc).split())}", file=sys.stderr)
        return 2
    except (QueueError, sqlite3.Error, OSError) as exc:
        print(f"{prog}: {' '.join(str(exc).split()) or type(exc).__name__}", file=sys.stderr)
        return 1

    environment = dict(os.environ)
    environment.update(
        {
            "ORCH_JOB_ID": args.job_id,
            "ORCH_JOB_BOT": args.bot,
            DB_ENV: str(database),
        }
    )
    checkout: list[worktrees.JobCheckout] = []
    box_brief = os.environ.get(waiter.BRIEF_ENV) or None
    remote_env = {
        "ORCH_JOB_ID": args.job_id,
        "ORCH_JOB_BOT": args.bot,
        "ORCH_JOB_DEVICE": placed.device.name,
        # #94: with --worktree, the checkout's and the brief's remote paths
        **placed.remote_env(worktree=args.worktree, brief=box_brief is not None),
    }

    def setup() -> Path | None:
        if args.worktree and placed.remote:
            # #94: the same --worktree, on the placed device: preflight and
            # checkout there before any worker is recorded or started.
            placed.remote_checkout(
                harness=command[0],
                repo=repo,
                head=head,
                detail=detail,
                url_template=worktrees.url_template(args.git_url_template),
                brief=_read_brief(box_brief),
            )
        placed.before_start()
        if not args.worktree or placed.remote:
            return None
        made = worktrees.prepare(
            worktrees.work_root(args.work_root),
            args.job_id,
            repo,
            head,
            url_template=worktrees.url_template(args.git_url_template),
        )
        checkout.append(made)
        environment["ORCH_JOB_WORKTREE"] = str(made.path)
        log(f"job worktree {made.path}")
        return made.path

    def teardown() -> None:
        if checkout:
            try:
                worktrees.finish(checkout[0], log)
            except (worktrees.WorktreeError, OSError) as exc:
                log(f"could not remove job worktree {checkout[0].path}: {exc}")

    try:
        return job_run.run(
            placed.argv(command, remote_env),
            beat=beat,
            interval=interval,
            log=log,
            env=environment,
            setup=setup,
            teardown=teardown,
            started=placed.started,
        )
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
        # A remote run's job is finished by its waiter on the box, from the
        # run's exit status and final ORCH-HANDOFF line (lib/waiter.py), so
        # only a box run is failed here.
        if placed.autostart and not placed.remote:
            _fail_unfinished(queue, args.job_id, owner, log)
        try:
            placed.finish()
        except (QueueError, sqlite3.Error, OSError) as exc:
            log(f"could not release the device slot: {' '.join(str(exc).split())}")


def _fail_unfinished(queue: SQLiteJobQueue, job_id: str, owner: str, log: Callable[[str], None]) -> None:
    """An autostart run whose command ended without `orch job done` goes to T6 now (#80)."""
    try:
        with queue.connect() as connection:
            row = connection.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None or row["state"] != "running":
            return
        retry_locked(lambda: queue.finish(job_id, owner=owner, handoff_comment_id=None))
    except QueueError:
        return  # finished between the read and the write
    except (sqlite3.Error, OSError) as exc:
        log(f"could not fail job {job_id}: {' '.join(str(exc).split())}")
        return
    log(f"the command ended without orch job done; failed job {job_id} for T6")


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
                project.linear_project_id
                for project in config.projects
                if project.linear_project_id is not None
            ),
            db_path=path,
            path=webhook.path,
            log=lambda line: print(line, flush=True),
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
    print(f"linear-webhook: listening on {shown}:{port}{webhook.path}", flush=True)
    signal.signal(signal.SIGTERM, _exit_on_term)
    try:
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
    parser.add_argument("--factory", type=Path, action="append")
    parser.add_argument("--allow-placeholders", action="store_true")
    return parser


def _install_skills(argv: list[str]) -> int:
    parser = _Parser(prog="orch install-skills", add_help=True)
    parser.add_argument("--home", type=Path)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--cloud", action="store_true")
    args = parser.parse_args(argv)
    try:
        anchor, mode = _anchor("install-skills", args.home, args.project, args.cloud)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    errors = install_skills(ROOT, anchor, mode)
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
    args = parser.parse_args(argv)
    if args.print_canary and args.responses is not None:
        print("orch doctor: --print-canary does not take --responses", file=sys.stderr)
        return 2
    try:
        anchor, mode = _anchor("doctor", args.home, args.project, args.cloud)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    code, prompt, errors = doctor(
        ROOT,
        anchor,
        mode,
        args.responses,
        print_canary=args.print_canary,
    )
    if prompt:
        print(prompt, end="")
    for message in errors:
        print(message, file=sys.stderr)
    if code == 0 and not args.print_canary:
        print("ok")
    return code


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
