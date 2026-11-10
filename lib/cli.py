"""orch check-config, install-skills, and doctor."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from lib.factory_md import EXAMPLE_PATHS, load_factory_md
from lib.load import Finding
from lib.orch_yml import load_orch_yml
from lib.pstack_pin import Mode, doctor, install_skills

ROOT = Path(__file__).resolve().parent.parent

USAGE = """\
usage: orch <command> [options]

commands:
  check-config     validate orch.yml and factory.md
  install-skills   install the pinned pstack skills
  doctor           canary the pinned poteto-mode skill

check-config [--orch PATH] [--factory PATH ...]
  With no paths, checks orch.yml and the example factory.md files.
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
"""


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE, end="")
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    command = argv[0]
    if command == "check-config":
        handler = _check_config
    elif command == "install-skills":
        handler = _install_skills
    elif command == "doctor":
        handler = _doctor
    else:
        print(f"unknown command: {command}", file=sys.stderr)
        print(USAGE, end="", file=sys.stderr)
        return 2
    try:
        return handler(argv[1:])
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
