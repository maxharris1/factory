"""`orch dashboard snapshot` and `orch dashboard push`.

There is no live server on the box: the box only writes the snapshot and,
when configured, pushes it out. Paths:

  --db   else $ORCH_DB (required)
  --log  else $ORCH_POLL_LOG, else poller.log next to the database
  --out  else $ORCH_DASHBOARD_DIR, else dashboard/ next to the database
  --orch else the checkout's orch.yml (only its dashboard section is used)
  --machines else the checkout's machines.yml, when present
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from lib.dashboard import (
    DashboardError,
    collect_snapshot,
    write_snapshot,
)
from lib.dashboard_config import DashboardConfig, REFRESH_MAX, REFRESH_MIN
from lib.dashboard_push import NoopTransport, run_push, transport_for
from lib.orch_yml import load_orch_yml

ROOT = Path(__file__).resolve().parent.parent


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        print(f"{self.prog}: {message}", file=sys.stderr)
        raise ValueError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="orch dashboard", add_help=True)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    snapshot = commands.add_parser("snapshot", help="write status.json and index.html")
    push = commands.add_parser("push", help="snapshot and push when configured")
    for sub in (snapshot, push):
        sub.add_argument("--db", type=Path)
        sub.add_argument("--log", type=Path)
        sub.add_argument("--out", type=Path)
        sub.add_argument("--orch", type=Path)
        sub.add_argument("--machines", type=Path)
    snapshot.add_argument("--refresh", type=int)
    push.add_argument("--once", action="store_true")
    return parser


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    prog = f"orch dashboard {args.command}"
    if args.command == "snapshot" and args.refresh is not None and not (
        REFRESH_MIN <= args.refresh <= REFRESH_MAX
    ):
        print(f"{prog}: --refresh must be 15 to 30 seconds", file=sys.stderr)
        return 2
    config = _config(args.orch, prog)
    if config is None:
        return 1

    if args.command == "push":
        transport = transport_for(config.push)
        # Unconfigured push never resolves paths, opens the database or writes.
        if not isinstance(transport, NoopTransport) and not _paths_ok(args, prog):
            return 2
        return run_push(
            transport=transport,
            snapshot=lambda: _snapshot(args, config, None),
            interval=config.push.interval_seconds,
            once=args.once,
            sleep=time.sleep,
            log=lambda line: print(line, file=sys.stderr, flush=True),
        )

    if not _paths_ok(args, prog):
        return 2
    try:
        status, index = _snapshot(args, config, args.refresh)
    except (DashboardError, OSError) as exc:
        print(f"{prog}: {' '.join(str(exc).split())}", file=sys.stderr)
        return 1
    print(f"wrote {status} {index}")
    return 0


def _paths_ok(args: argparse.Namespace, prog: str) -> bool:
    if args.db is None and not os.environ.get("ORCH_DB"):
        print(f"{prog}: pass --db or set ORCH_DB to the dispatcher database", file=sys.stderr)
        return False
    return True


def _config(path: Path | None, prog: str) -> DashboardConfig | None:
    orch_path = path if path is not None else ROOT / "orch.yml"
    if path is None and not orch_path.is_file():
        return DashboardConfig()
    config, findings = load_orch_yml(orch_path, allow_placeholders=True)
    errors = [item for item in findings if item.severity == "error"]
    if config is None or errors:
        for item in errors or findings:
            print(f"{prog}: {item.format(str(orch_path))}", file=sys.stderr)
        return None
    return config.dashboard


def _snapshot(
    args: argparse.Namespace,
    config: DashboardConfig,
    refresh: int | None,
) -> tuple[Path, Path]:
    db = args.db if args.db is not None else Path(os.environ["ORCH_DB"])
    log = args.log or _env_path("ORCH_POLL_LOG") or db.parent / "poller.log"
    out = args.out or _env_path("ORCH_DASHBOARD_DIR") or db.parent / "dashboard"
    machines = args.machines if args.machines is not None else ROOT / "machines.yml"
    snapshot = collect_snapshot(
        db,
        log,
        now=time.time(),
        machines_path=machines,
        refresh_seconds=refresh if refresh is not None else config.refresh_seconds,
    )
    return write_snapshot(out, snapshot)


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None
