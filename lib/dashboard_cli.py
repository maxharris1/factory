"""`orch dashboard snapshot` and `orch dashboard push`.

There is no live server on the box: the box only writes the snapshot and,
when configured, pushes it out. Paths:

  --db   else $ORCH_DB (required)
  --log  else $ORCH_POLL_LOG, else poller.log next to the database
  --out  else $ORCH_DASHBOARD_DIR, else dashboard/ next to the database
  --orch else the checkout's orch.yml (only its dashboard, job_states and
         poll_seconds are used)
  --machines else the checkout's machines.yml, when present

`snapshot` writes status.json, the five pages (index.html, job.html,
activity.html, efficiency.html, health.html) and tickets.json (a record per
tracked ticket); `push` sends them all and, over ssh, each settled QA run's
evidence folder (lib/qa_evidence_push.py).
`push --status-only` sends status.json alone (the live pushes `orch poll`
starts on every orch.db change); `--last-cycle EPOCH` is when the poller's
last cycle ended, reported as poll health. `--poll-seconds` (30 or 60) is the
running poller's poll_seconds, which `orch poll` passes so poll health's
`cycle_seconds` and thresholds match the interval it actually sleeps; without
it orch.yml's poll_seconds is used (60 when there is no orch.yml). Each push
snapshot first records the `dashboard` heartbeat in orch.health.db
(lib/health.py, #92), at the writer's publication interval, which the
snapshot also carries as `health.publish_seconds`; a heartbeat that cannot be
written is logged and the push goes on.
"""

from __future__ import annotations

import argparse
import math
import os
import sqlite3
import sys
import time
from collections.abc import Callable
from pathlib import Path

from lib import health
from lib.dashboard import (
    DashboardError,
    collect_snapshot,
    write_snapshot_pages,
)
from lib.dashboard_config import DashboardConfig, REFRESH_MAX, REFRESH_MIN
from lib.dashboard_push import NoopTransport, SshTransport, run_push, transport_for
from lib.job_state import JobStateConfig
from lib.orch_yml import POLL_SECONDS, POLL_SECONDS_ALLOWED, OrchConfig, load_orch_yml
from lib.qa_evidence_push import STORE_NAME, push_evidence

ROOT = Path(__file__).resolve().parent.parent


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        print(f"{self.prog}: {message}", file=sys.stderr)
        raise ValueError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="orch dashboard", add_help=True)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    snapshot = commands.add_parser("snapshot", help="write status.json, the pages and tickets.json")
    push = commands.add_parser("push", help="snapshot and push when configured")
    for sub in (snapshot, push):
        sub.add_argument("--db", type=Path)
        sub.add_argument("--log", type=Path)
        sub.add_argument("--out", type=Path)
        sub.add_argument("--orch", type=Path)
        sub.add_argument("--machines", type=Path)
    snapshot.add_argument("--refresh", type=int)
    push.add_argument("--once", action="store_true")
    push.add_argument("--status-only", action="store_true")
    push.add_argument("--last-cycle", type=_epoch)
    push.add_argument("--poll-seconds", type=int, choices=POLL_SECONDS_ALLOWED)
    return parser


def _epoch(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a Unix time in seconds") from None
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be a Unix time in seconds")
    return number


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    prog = f"orch dashboard {args.command}"
    if args.command == "snapshot" and args.refresh is not None and not (
        REFRESH_MIN <= args.refresh <= REFRESH_MAX
    ):
        print(f"{prog}: --refresh must be 15 to 30 seconds", file=sys.stderr)
        return 2
    loaded = _config(args.orch, prog)
    if loaded is None:
        return 1
    config, job_states, poll_seconds, orch = loaded

    if args.command == "push":
        transport = transport_for(config.push)
        # Unconfigured push never resolves paths, opens the database or writes.
        if not isinstance(transport, NoopTransport) and not _paths_ok(args, prog):
            return 2

        cycle = args.poll_seconds if args.poll_seconds is not None else poll_seconds
        # How often this writer publishes (#92): the push loop's interval, or
        # the poller's schedule (a live push publishes every poll cycle).
        every = config.push.every_minutes
        if not args.once:
            publish = config.push.interval_seconds
        elif every and not config.push.live:
            publish = every * 60
        else:
            publish = cycle

        def snapshot() -> tuple[Path, ...]:
            # The writer's heartbeat goes first, so the snapshot shows it.
            health.beat_once(
                _db(args),
                health.DASHBOARD,
                interval=publish,
                machines=args.machines,
                log=_log,
            )
            paths = _snapshot(
                args,
                config,
                None,
                last_cycle_at=args.last_cycle,
                job_states=job_states,
                poll_seconds=cycle,
                orch=orch,
                publish_seconds=publish,
            )
            # Live pushes send status.json alone; the page polls it.
            return paths[:1] if args.status_only else paths

        return run_push(
            transport=transport,
            snapshot=snapshot,
            interval=config.push.interval_seconds,
            once=args.once,
            sleep=time.sleep,
            log=_log,
            evidence=_evidence(args, transport, orch),
        )

    if not _paths_ok(args, prog):
        return 2
    try:
        paths = _snapshot(
            args, config, args.refresh, job_states=job_states, poll_seconds=poll_seconds,
            orch=orch,
        )
    except (DashboardError, OSError) as exc:
        print(f"{prog}: {' '.join(str(exc).split())}", file=sys.stderr)
        return 1
    print("wrote " + " ".join(str(path) for path in paths))
    return 0


def _evidence(
    args: argparse.Namespace, transport: object, orch: OrchConfig | None
) -> Callable[[], None] | None:
    """A full ssh push's QA evidence step, or None when there is nothing to send."""
    roots = {
        project.repo.lower(): project.qa_storage.evidence_root
        for project in (orch.projects if orch is not None else ())
        if project.qa_storage is not None
    }
    if not isinstance(transport, SshTransport) or args.status_only or not roots:
        return None

    def evidence() -> None:
        connection = sqlite3.connect(f"{_db(args).resolve().as_uri()}?mode=ro", uri=True)
        try:
            push_evidence(transport, connection=connection, roots=roots, store=_out(args) / STORE_NAME, log=_log)
        finally:
            connection.close()

    return evidence


def _log(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def _paths_ok(args: argparse.Namespace, prog: str) -> bool:
    if args.db is None and not os.environ.get("ORCH_DB"):
        print(f"{prog}: pass --db or set ORCH_DB to the dispatcher database", file=sys.stderr)
        return False
    return True


def _config(
    path: Path | None, prog: str
) -> tuple[DashboardConfig, JobStateConfig, int, OrchConfig | None] | None:
    orch_path = path if path is not None else ROOT / "orch.yml"
    if path is None and not orch_path.is_file():
        return DashboardConfig(), JobStateConfig(), POLL_SECONDS, None
    config, findings = load_orch_yml(orch_path, allow_placeholders=True)
    errors = [item for item in findings if item.severity == "error"]
    if config is None or errors:
        for item in errors or findings:
            print(f"{prog}: {item.format(str(orch_path))}", file=sys.stderr)
        return None
    return config.dashboard, config.job_states, config.poll_seconds, config


def _snapshot(
    args: argparse.Namespace,
    config: DashboardConfig,
    refresh: int | None,
    *,
    last_cycle_at: float | None = None,
    job_states: JobStateConfig | None = None,
    poll_seconds: int = POLL_SECONDS,
    orch: OrchConfig | None = None,
    publish_seconds: float | None = None,
) -> tuple[Path, ...]:
    """status.json first, then every page."""
    db = _db(args)
    log = args.log or _env_path("ORCH_POLL_LOG") or db.parent / "poller.log"
    machines = args.machines if args.machines is not None else ROOT / "machines.yml"
    snapshot = collect_snapshot(
        db,
        log,
        now=time.time(),
        uptime=health.read_uptime(),
        machines_path=machines,
        refresh_seconds=refresh if refresh is not None else config.refresh_seconds,
        last_cycle_at=last_cycle_at,
        job_states=job_states,
        poll_seconds=poll_seconds,
        orch=orch,
        publish_seconds=publish_seconds,
    )
    return write_snapshot_pages(_out(args), snapshot)


def _db(args: argparse.Namespace) -> Path:
    return args.db if args.db is not None else Path(os.environ["ORCH_DB"])


def _out(args: argparse.Namespace) -> Path:
    return args.out or _env_path("ORCH_DASHBOARD_DIR") or _db(args).parent / "dashboard"


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None
