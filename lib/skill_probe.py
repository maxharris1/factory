"""Skill-loading and helper-agent probe (#119).

Factory briefs say "Run as poteto-mode", and jobs often never read the
skill. This probe starts each harness the way the waiter does and reads the
transcript the harness writes itself, so the answer comes from what reached
the model's context and never from what the model says.

* Launch: the bot's own argv from its waiter config (`load_harness_command`),
  unchanged on the box and made device-neutral by `for_device` elsewhere,
  with only `harness_env` added and the prompt on stdin. A remote device is
  reached through `remote.start_script` over the bot's own SSH alias.
* Read = yes only when a line from the body of `SKILL.md` (the canary, never
  frontmatter, which Claude lists without opening the file) shows up in a
  record the model did not write: a tool result that is not an error, or an
  injected user, system or attachment record. Assistant text never counts.
* Helper = yes when a spawn call has a result that is not an error.
* No transcript: every cell is `unknown`.

The probe opens only the canary skill, the waiter config, machines.yml, its
run and output directories, and the harness transcripts of its own runs
(`~/.claude/projects/<slug>/`, `$CODEX_HOME/sessions/`).

Codex record names (`spawn_agent`, the rollout layout) follow the Codex CLI
rollout format and are provisional until a live codex-bot run on the box
confirms them; a renamed tool is one edit to CODEX_HELPER_TOOLS.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Literal

from lib.codex_sandbox import for_device
from lib.harness_env import harness_env
from lib.machines_yml import BOT_USERS, MachinesConfig, load_machines_yml
from lib.remote import LOGIN_SHELL, RemoteError, RemoteTarget, new_tag, ssh_argv, start_script
from lib.waiter import WaiterError, default_harness_config, load_harness_command

Harness = Literal["claude", "codex"]
Outcome = Literal["yes", "no", "missing", "n/a", "unknown"]

SKILL = "poteto-mode"
POTETO_AGENT_TYPES = frozenset({"poteto-agent", "pstack:poteto-agent"})
CLAUDE_AGENT_TOOLS = frozenset({"Agent", "Task"})
CODEX_HELPER_TOOLS = frozenset({"spawn_agent"})
MISSING_AGENT = re.compile(r"not found|unknown agent|no such agent|invalid agent type", re.I)
INSTALLED = {
    "claude": (".claude", "skills", SKILL, "SKILL.md"),
    "codex": (".agents", "skills", SKILL, "SKILL.md"),
}
DEFAULT_TIMEOUT = 600.0
FETCH_TIMEOUT = 120.0


class ProbeError(Exception):
    """The probe cannot run. One line."""


class Style(Enum):
    RUN_AS = "run-as"
    READ_PATH = "read-path"
    SLASH = "slash"
    CODEX_MENTION = "codex-skill"


@dataclass(frozen=True)
class StyleSpec:
    style: Style
    harnesses: frozenset[str]
    opening: Callable[[str], str]


STYLES: tuple[StyleSpec, ...] = (
    StyleSpec(Style.RUN_AS, frozenset({"claude", "codex"}), lambda _: f"Run as {SKILL}."),
    StyleSpec(
        Style.READ_PATH,
        frozenset({"claude", "codex"}),
        lambda path: f"Read {path} and follow it.",
    ),
    StyleSpec(Style.SLASH, frozenset({"claude"}), lambda _: f"/{SKILL}"),
    StyleSpec(Style.CODEX_MENTION, frozenset({"codex"}), lambda _: f"${SKILL}"),
)


@dataclass(frozen=True)
class Evidence:
    read: Outcome
    read_via: str | None
    helper: Outcome
    poteto_agent: Outcome
    harness_version: str | None


UNKNOWN = Evidence("unknown", None, "unknown", "unknown", None)


@dataclass(frozen=True)
class Launch:
    device: str
    harness: str
    style: Style
    argv: tuple[str, ...]
    env: Mapping[str, str]
    run_dir: str
    skill_md: str
    remote: RemoteTarget | None = None
    guarded_dirs: tuple[str, ...] = ()
    insert_at: int = 0
    cd: str | None = None

    @property
    def name(self) -> str:
        return f"{self.device}-{self.harness}-{self.style.value}"


@dataclass(frozen=True)
class ProbeResult:
    date: str
    device: str
    harness: str
    style: Style
    evidence: Evidence
    note: str = ""

    def to_json(self) -> str:
        data = asdict(self)
        data["style"] = self.style.value
        return json.dumps(data, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> ProbeResult:
        data = json.loads(text)
        return cls(
            date=data["date"],
            device=data["device"],
            harness=data["harness"],
            style=Style(data["style"]),
            evidence=Evidence(**data["evidence"]),
            note=data.get("note", ""),
        )


def skill_canary(skill_md: str) -> str:
    """The first non-blank body line after the H1: never frontmatter, never the title."""
    lines = skill_md.splitlines()
    start = 0
    if lines and lines[0].strip() == "---":
        closing = next(
            (index for index in range(1, len(lines)) if lines[index].strip() == "---"), None
        )
        if closing is None:
            raise ProbeError("SKILL.md frontmatter is not closed")
        start = closing + 1
    seen_title = False
    for line in lines[start:]:
        text = line.strip()
        if not text:
            continue
        if not seen_title and text.startswith("# "):
            seen_title = True
            continue
        return text
    raise ProbeError("SKILL.md has no body line to use as a canary")


def style_spec(style: Style) -> StyleSpec:
    return next(spec for spec in STYLES if spec.style is style)


def prompt_for(style: Style, harness: str, skill_path: str, canary: str) -> str:
    """The style's opening line, then fixed text. It never holds the canary."""
    helpers = (
        "Start one helper agent of type general-purpose that replies ok. "
        "Then start one helper agent of type poteto-agent that replies ok."
        if harness == "claude"
        else "Start one sub-agent that replies ok."
    )
    prompt = (
        f"{style_spec(style).opening(skill_path)}\n\n"
        "This is a factory skill-loading probe. Do only this:\n"
        f"1. {helpers}\n"
        "2. Reply with one line saying whether you read the skill, then stop.\n"
        "Read no other file, write no file, and make no network or GitHub call.\n"
    )
    if canary in prompt:
        raise ProbeError("the probe prompt contains the canary")
    return prompt


def _text(value: object) -> str:
    """Every string inside a record's content, joined."""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return "\n".join(_text(item) for item in value.values())
    if isinstance(value, list):
        return "\n".join(_text(item) for item in value)
    return ""


def _blocks(record: Mapping) -> list:
    message = record.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if isinstance(content, list):
        return [block for block in content if isinstance(block, Mapping)]
    return []


def claude_evidence(records: Iterable[Mapping], canary: str) -> Evidence:
    records = [record for record in records if isinstance(record, Mapping)]
    if not records:
        return UNKNOWN
    version = next(
        (str(r["version"]) for r in records if isinstance(r.get("version"), str)), None
    )
    calls: dict[str, Mapping] = {}
    results: dict[str, tuple[str, bool]] = {}
    read_via: str | None = None
    for record in records:
        kind = record.get("type")
        if kind == "assistant":
            for block in _blocks(record):
                if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                    calls[block["id"]] = block
            continue
        if kind == "attachment" or (kind in ("user", "system") and not _blocks(record)):
            if read_via is None and canary in _text(
                record.get("attachment") or record.get("message") or record.get("content")
            ):
                read_via = "injected"
            continue
        if kind != "user":
            continue
        for block in _blocks(record):
            if block.get("type") == "tool_result":
                body = _text(block.get("content"))
                error = bool(block.get("is_error"))
                results[str(block.get("tool_use_id"))] = (body, error)
                if read_via is None and not error and canary in body:
                    call = calls.get(str(block.get("tool_use_id")), {})
                    read_via = str(call.get("name") or "tool")
            elif read_via is None and canary in _text(block):
                read_via = "injected"
    helper: Outcome = "no"
    poteto: Outcome = "no"
    for call_id, call in calls.items():
        if call.get("name") not in CLAUDE_AGENT_TOOLS:
            continue
        arguments = call.get("input") if isinstance(call.get("input"), Mapping) else {}
        kind = arguments.get("subagent_type") or "general-purpose"
        outcome = _spawn_outcome(results.get(call_id))
        if kind == "general-purpose":
            helper = _better(helper, outcome)
        elif kind in POTETO_AGENT_TYPES:
            poteto = _better(poteto, outcome)
    return Evidence("yes" if read_via else "no", read_via, helper, poteto, version)


def _spawn_outcome(result: tuple[str, bool] | None) -> Outcome:
    if result is None:
        return "no"
    body, error = result
    if not error:
        return "yes"
    return "missing" if MISSING_AGENT.search(body) else "no"


def _better(current: Outcome, new: Outcome) -> Outcome:
    order = ("no", "missing", "yes")
    return new if order.index(new) > order.index(current) else current


def codex_evidence(records: Iterable[Mapping], canary: str) -> Evidence:
    records = [record for record in records if isinstance(record, Mapping)]
    if not records:
        return UNKNOWN
    version = None
    calls: dict[str, str] = {}
    outputs: dict[str, tuple[str, bool]] = {}
    read_via: str | None = None
    for record in records:
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if record.get("type") == "session_meta":
            version = version or (
                str(payload["cli_version"]) if payload.get("cli_version") else None
            )
            continue
        if record.get("type") != "response_item":
            continue
        kind = payload.get("type")
        if kind in ("function_call", "custom_tool_call", "local_shell_call"):
            name = payload.get("name") or ("shell" if kind == "local_shell_call" else "tool")
            calls[str(payload.get("call_id"))] = str(name)
        elif kind in ("function_call_output", "custom_tool_call_output"):
            output = payload.get("output")
            failed = isinstance(output, Mapping) and output.get("success") is False
            body = _text(output)
            call_id = str(payload.get("call_id"))
            outputs[call_id] = (body, failed)
            if read_via is None and not failed and canary in body:
                read_via = calls.get(call_id, "tool")
        elif kind == "message" and payload.get("role") != "assistant":
            if read_via is None and canary in _text(payload.get("content")):
                read_via = "injected"
    helper: Outcome = "no"
    for call_id, name in calls.items():
        if name in CODEX_HELPER_TOOLS:
            helper = _better(helper, _spawn_outcome(outputs.get(call_id)))
    return Evidence("yes" if read_via else "no", read_via, helper, "n/a", version)


PARSERS: Mapping[str, Callable[[Iterable[Mapping], str], Evidence]] = {
    "claude": claude_evidence,
    "codex": codex_evidence,
}


def parse_jsonl(text: str) -> list[Mapping]:
    records = []
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, Mapping):
            records.append(record)
    return records


def harness_of(bot: str, machines: MachinesConfig) -> str:
    for device in machines.devices:
        for harness, user in device.harnesses.items():
            if user == bot:
                return harness
    raise ProbeError(f"machines.yml maps no harness to {bot}")


def plan_launches(
    machines: MachinesConfig,
    bot: str,
    command: Sequence[str],
    *,
    run_id: str,
    local_root: str,
    homes: Mapping[str, str],
    skill_md: str | None = None,
    only_device: str | None = None,
) -> list[Launch]:
    """One launch per enabled device x applicable style for bot's harness.

    homes maps a device name to the bot's home there (for the installed
    SKILL.md path); skill_md, when given, is used on every device instead.
    """
    harness = harness_of(bot, machines)
    if harness not in PARSERS:
        raise ProbeError(f"{harness} is not a probed harness")
    launches = []
    for device in machines.devices:
        if only_device is not None and device.name != only_device:
            continue
        if not device.eligible(harness) or device.harnesses[harness] != bot:
            continue
        path = skill_md or str(Path(homes.get(device.name, "~"), *INSTALLED[harness]))
        for spec in STYLES:
            if harness not in spec.harnesses:
                continue
            job_id = f"{run_id}-{harness}-{spec.style.value}"
            env = harness_env(harness)
            if device.local:
                launches.append(
                    Launch(device.name, harness, spec.style, tuple(command), env,
                           str(Path(local_root, job_id)), path)
                )
                continue
            alias = device.ssh_alias_for(harness)
            workdir = device.workdir_for(harness)
            if alias is None or workdir is None:
                continue
            target = RemoteTarget(device.name, alias, bot, workdir, job_id, new_tag())
            neutral = for_device(command)
            launches.append(
                Launch(device.name, harness, spec.style, neutral.argv, env, target.job_dir,
                       path, target, neutral.guarded_dirs, neutral.insert_at, neutral.cd)
            )
    return launches


def launch_argv(launch: Launch) -> list[str]:
    if launch.remote is None:
        return list(launch.argv)
    return ssh_argv(
        launch.remote,
        start_script(
            launch.remote,
            launch.argv,
            launch.env,
            guarded_dirs=launch.guarded_dirs,
            insert_at=launch.insert_at,
            cd=launch.cd,
        ),
    )


def claude_slug(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def claude_transcript(config_dir: Path, run_dir: str) -> Path | None:
    """The newest session file of a Claude run in run_dir, confined to its project dir."""
    projects = config_dir / "projects"
    found: list[Path] = []
    for cwd in dict.fromkeys((run_dir, os.path.realpath(run_dir))):
        folder = projects / claude_slug(cwd)
        if not folder.is_dir() or not _inside(folder, projects):
            continue
        found += [p for p in folder.glob("*.jsonl") if p.is_file() and _inside(p, folder)]
    return max(found, key=lambda p: p.stat().st_mtime, default=None)


def codex_transcript(codex_home: Path, run_dir: str, since: float) -> Path | None:
    """The rollout whose session_meta cwd is run_dir, written since the run began."""
    sessions = codex_home / "sessions"
    if not sessions.is_dir():
        return None
    wanted = {run_dir, os.path.realpath(run_dir)}
    newest: Path | None = None
    for path in sessions.rglob("rollout-*.jsonl"):
        if not path.is_file() or not _inside(path, sessions) or path.stat().st_mtime < since:
            continue
        with path.open(encoding="utf-8", errors="replace") as handle:
            first = parse_jsonl(handle.readline())
        payload = first[0].get("payload") if first else None
        if isinstance(payload, Mapping) and payload.get("cwd") in wanted:
            if newest is None or path.stat().st_mtime > newest.stat().st_mtime:
                newest = path
    return newest


def fetch_script(harness: str, run_dir: str) -> str:
    """Under the bot's login shell, cat the one transcript of the run in run_dir."""
    if harness == "claude":
        script = (
            'f=$(ls -t -- "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/$1"/*.jsonl '
            "2>/dev/null | head -n 1); "
            '[ -n "$f" ] && [ -f "$f" ] && exec cat -- "$f"; exit 3'
        )
        args = [claude_slug(run_dir)]
    else:
        script = (
            's="${CODEX_HOME:-$HOME/.codex}/sessions"; [ -d "$s" ] || exit 3; '
            'f=$(find "$s" -type f -name "rollout-*.jsonl" -newer "$1" | while read -r f; do '
            'head -n 1 "$f" | grep -qF -e "\\"cwd\\":\\"$1\\"" -e "\\"cwd\\": \\"$1\\"" '
            '&& { printf "%s\\n" "$f"; break; }; done); '
            '[ -n "$f" ] && exec cat -- "$f"; exit 3'
        )
        args = [run_dir]
    quoted = " ".join(shlex.quote(arg) for arg in args)
    return f"exec {LOGIN_SHELL} -lc {shlex.quote(script)} skill-probe {quoted}"


HOME_SCRIPT = f"exec {LOGIN_SHELL} -lc 'printf %s \"$HOME\"'"


Spawn = Callable[..., int | None]
Call = Callable[..., subprocess.CompletedProcess]


def spawn(argv: Sequence[str], *, stdin: bytes, log: Path, cwd: str | None,
          env: Mapping[str, str], timeout: float) -> int | None:
    """Run argv in its own session; on timeout stop its whole group. None on timeout."""
    with log.open("wb") as out:
        child = subprocess.Popen(
            list(argv), stdin=subprocess.PIPE, stdout=out, stderr=subprocess.STDOUT,
            cwd=cwd, env=dict(env), start_new_session=True,
        )
        try:
            child.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            child.wait()
            return None
    return child.returncode


def call(argv: Sequence[str], *, timeout: float = FETCH_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(argv), input=b"", capture_output=True, timeout=timeout, check=False
    )


def run_launch(
    launch: Launch,
    prompt: str,
    *,
    timeout: float,
    log_path: Path,
    run: Spawn = spawn,
    fetch: Call = call,
    clock: Callable[[], float] = time.time,
    claude_dir: Path | None = None,
    codex_home: Path | None = None,
) -> tuple[str, str]:
    """Start the harness once, then return (transcript JSONL text, note)."""
    started = clock()
    if launch.remote is None:
        Path(launch.run_dir).mkdir(parents=True, exist_ok=False)
    code = run(
        launch_argv(launch),
        stdin=prompt.encode("utf-8"),
        log=log_path,
        cwd=launch.run_dir if launch.remote is None else None,
        env={**os.environ, **launch.env},
        timeout=timeout,
    )
    note = f"timed out after {timeout:g}s" if code is None else (f"exit {code}" if code else "")
    if launch.remote is not None:
        try:
            fetched = fetch(ssh_argv(launch.remote, fetch_script(launch.harness, launch.run_dir)))
        except subprocess.TimeoutExpired:
            return "", note or "transcript fetch timed out"
        text = fetched.stdout.decode("utf-8", "replace") if fetched.returncode == 0 else ""
        return text, note or ("" if text else "no transcript")
    if launch.harness == "claude":
        base = claude_dir or Path(
            os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"
        )
        path = claude_transcript(base, launch.run_dir)
    else:
        base = codex_home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        path = codex_transcript(base, launch.run_dir, started - 1)
    if path is None:
        return "", note or "no transcript"
    return path.read_text(encoding="utf-8", errors="replace"), note


def remote_home(target: RemoteTarget, fetch: Call = call) -> str | None:
    """The bot's home on a remote device, from its login shell."""
    try:
        done = fetch(ssh_argv(target, HOME_SCRIPT))
    except subprocess.TimeoutExpired:
        return None
    home = done.stdout.decode("utf-8", "replace").strip()
    return home if done.returncode == 0 and home.startswith("/") else None


def new_run_id() -> str:
    return f"skill-probe-{secrets.token_hex(4)}"


def render(results: Sequence[ProbeResult]) -> str:
    """docs/skill-loading.md: one row per device x harness x style, n/a where it does not apply."""
    by_key = {(r.device, r.harness, r.style): r for r in results}
    devices = sorted({r.device for r in results})
    harnesses = [h for h in PARSERS if any(r.harness == h for r in results)]
    lines = [
        "| Date | Device | Harness | Version | Style | Read | Read via | Helper | poteto-agent | Note |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for device in devices:
        for harness in harnesses:
            if not any(r.device == device and r.harness == harness for r in results):
                continue
            for spec in STYLES:
                result = by_key.get((device, harness, spec.style))
                if result is None:
                    if harness in spec.harnesses:
                        continue
                    lines.append(
                        f"| | {device} | {harness} | | {spec.style.value} "
                        "| n/a | | n/a | n/a | style does not apply |"
                    )
                    continue
                e = result.evidence
                lines.append(
                    f"| {result.date} | {device} | {harness} | {e.harness_version or ''} "
                    f"| {spec.style.value} | {e.read} | {e.read_via or ''} | {e.helper} "
                    f"| {e.poteto_agent} | {result.note} |"
                )
    return "\n".join(lines) + "\n"
def _pinned_skill() -> Path:
    return Path(__file__).resolve().parent.parent / "skills" / "pstack" / "skills" / SKILL / "SKILL.md"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="skill-probe", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="start each harness once per style and record evidence")
    run_p.add_argument("--bot", required=True, choices=sorted(BOT_USERS.values()))
    run_p.add_argument("--machines", type=Path, default=Path("machines.yml"))
    run_p.add_argument("--config", type=Path, help="waiter config (default: the bot's own)")
    run_p.add_argument("--device", help="probe only this device")
    run_p.add_argument("--out", type=Path, help="result directory (must not exist)")
    run_p.add_argument("--work", type=Path, help="local run directory root (default: temp)")
    run_p.add_argument("--skill-md", help="absolute SKILL.md path the read-path style names")
    run_p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    run_p.add_argument("--dry-run", action="store_true", help="print launches; start nothing")
    render_p = sub.add_parser("render", help="print the results table as markdown")
    render_p.add_argument("results", nargs="+", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "render":
            sys.stdout.write(render([ProbeResult.from_json(p.read_text()) for p in args.results]))
            return 0
        return _run(args)
    except (ProbeError, WaiterError, RemoteError, OSError) as exc:
        print(f"skill-probe: {' '.join(str(exc).split())}", file=sys.stderr)
        return 1


def _run(args: argparse.Namespace) -> int:
    if args.skill_md is not None and not args.skill_md.startswith("/"):
        raise ProbeError("--skill-md must be an absolute path")
    machines, findings = load_machines_yml(args.machines, allow_placeholders=True)
    if machines is None:
        errors = [f.message for f in findings if f.severity == "error"] or ["invalid"]
        raise ProbeError(f"{args.machines}: {errors[0]}")
    command = load_harness_command(args.config or default_harness_config(args.bot), args.bot)
    canary = skill_canary(_pinned_skill().read_text(encoding="utf-8"))
    run_id = new_run_id()
    work = args.work or Path(tempfile.gettempdir()) / "skill-probe"
    launches = plan_launches(
        machines, args.bot, command.command, run_id=run_id, local_root=str(work),
        homes={d.name: str(Path.home()) for d in machines.devices if d.local},
        skill_md=args.skill_md, only_device=args.device,
    )
    if not launches:
        raise ProbeError(f"no enabled device runs {args.bot}")
    if args.dry_run:
        for launch in launches:
            print(f"# {launch.name} in {launch.run_dir}")
            print(shlex.join(launch_argv(launch)))
            print(prompt_for(launch.style, launch.harness, launch.skill_md, canary))
        return 0
    if args.out is None:
        raise ProbeError("--out is required unless --dry-run")
    args.out.mkdir(parents=True, exist_ok=False)
    homes: dict[str, str | None] = {}
    date = datetime.now(timezone.utc).date().isoformat()
    for launch in launches:
        if launch.remote is not None and args.skill_md is None:
            if launch.device not in homes:
                homes[launch.device] = remote_home(launch.remote)
            home = homes[launch.device]
            if home is None:
                result = ProbeResult(date, launch.device, launch.harness, launch.style,
                                     UNKNOWN, "device did not answer")
                (args.out / f"{launch.name}.json").write_text(result.to_json())
                continue
            launch = replace(launch, skill_md=str(Path(home, *INSTALLED[launch.harness])))
        prompt = prompt_for(launch.style, launch.harness, launch.skill_md, canary)
        text, note = run_launch(
            launch, prompt, timeout=args.timeout, log_path=args.out / f"{launch.name}.log"
        )
        if text:
            (args.out / f"{launch.name}.transcript.jsonl").write_text(text)
        evidence = PARSERS[launch.harness](parse_jsonl(text), canary)
        result = ProbeResult(date, launch.device, launch.harness, launch.style, evidence, note)
        (args.out / f"{launch.name}.json").write_text(result.to_json())
        print(f"{launch.name}: read={evidence.read} helper={evidence.helper} {note}".rstrip())
    return 0
