"""orch check-config, wait, and job done."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from lib.dispatcher_queue import (
    JobQueueError,
    default_queue_path,
    is_job_id,
    open_queue,
)
from lib.factory_md import EXAMPLE_PATHS, load_factory_md
from lib.handoff import HandoffError
from lib.load import Finding
from lib.orch_yml import load_orch_yml
from lib.wait import format_wait_output, wait_for_job

ROOT = Path(__file__).resolve().parent.parent

USAGE = """\
usage: orch <command> [args]

commands:
  check-config [--orch PATH] [--factory PATH ...]
  wait <role> [--queue PATH]
  job done <job-id> [--queue PATH]

check-config validates orch.yml and factory.md. With no paths, it checks
orch.yml and the example factory.md files in this repo. Warnings (an
unknown model name) are printed to stderr and still exit 0.

wait blocks until the local job list has a job for <role>. It prints
that job and its orch:v1 handoff, then exits. It does not start a harness.

job done <job-id> marks a job returned by wait as complete.
"""

JOB_USAGE = """\
usage: orch job done <job-id> [--queue PATH]

Mark a job returned by orch wait as complete.
"""


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE, end="")
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    command, rest = argv[0], argv[1:]
    if command == "check-config":
        return _guard(_check_config, rest)
    if command == "wait":
        return _guard(_wait, rest)
    if command == "job":
        return _guard(_job, rest)
    print(f"unknown command: {command}", file=sys.stderr)
    print(USAGE, end="", file=sys.stderr)
    return 2


def _guard(fn: Callable[[list[str]], int], argv: list[str]) -> int:
    try:
        return fn(argv)
    except ValueError:
        return 2
    except SystemExit as exc:
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

    failed = _report(orch_path, load_orch_yml(orch_path)[1])
    for path in factory_paths:
        failed = _report(path, load_factory_md(path)[1]) or failed
    if failed:
        return 1
    print("ok")
    return 0


def _wait(argv: list[str]) -> int:
    parser = _Parser(prog="orch wait", add_help=True)
    parser.add_argument("role")
    parser.add_argument("--queue", type=Path)
    args = parser.parse_args(argv)
    queue = open_queue(_queue_path(args.queue))
    try:
        job = wait_for_job(queue, args.role)
    except HandoffError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    sys.stdout.write(format_wait_output(job))
    return 0


def _job(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(JOB_USAGE, end="")
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    if argv[0] != "done":
        print(f"unknown job command: {argv[0]}", file=sys.stderr)
        print(JOB_USAGE, end="", file=sys.stderr)
        return 2
    parser = _Parser(prog="orch job done", add_help=True)
    parser.add_argument("job_id")
    parser.add_argument("--queue", type=Path)
    args = parser.parse_args(argv[1:])
    if not is_job_id(args.job_id):
        print(f"bad job id {args.job_id!r}", file=sys.stderr)
        return 2
    try:
        open_queue(_queue_path(args.queue)).complete(args.job_id)
    except JobQueueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"done {args.job_id}")
    return 0


def _queue_path(path: Path | None) -> Path:
    if path is not None:
        return path
    return default_queue_path()


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


def _parser() -> _Parser:
    parser = _Parser(prog="orch check-config", add_help=True)
    parser.add_argument("--orch", type=Path)
    parser.add_argument("--factory", type=Path, action="append")
    return parser
