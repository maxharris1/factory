"""orch check-config. Other orch commands belong to later Phase 1 items."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from lib.factory_md import EXAMPLE_PATHS, load_factory_md
from lib.load import Finding
from lib.orch_yml import load_orch_yml

ROOT = Path(__file__).resolve().parent.parent

USAGE = """\
usage: orch check-config [--orch PATH] [--factory PATH ...]

Validate orch.yml and factory.md files. With no paths, checks orch.yml
and the example factory.md files in this repo. Warnings (an unknown
model name) are printed to stderr and still exit 0.
"""


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE, end="")
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    if argv[0] != "check-config":
        print(f"unknown command: {argv[0]}", file=sys.stderr)
        print(USAGE, end="", file=sys.stderr)
        return 2
    try:
        return _check_config(argv[1:])
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
