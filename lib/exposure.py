"""What a plan, build, fix or review job ran under, recorded in orch.db at its start.

Each field is read on the job's own device and is `unknown` when it cannot be
told; recording never delays the job past its reads' timeouts or fails it.
The model is replaced by the one the harness reports in its stream, when it
reports one (lib/telemetry.py).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import shlex
import signal
import sqlite3
import subprocess
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from lib.job_token import PROOF_ENV
from lib.remote import LOGIN_SHELL, TAG_ENV, ByteTransport, RemoteTarget, Transport, ssh_argv

UNKNOWN = "unknown"
TABLE = "job_exposure"
TIMEOUT_SECONDS = 10.0
SHELL_NAME = "orch-exposure"
VERSION_LIMIT = 200

DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    job_id TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    cli_version TEXT NOT NULL,
    brief_sha256 TEXT NOT NULL,
    skill_paths TEXT NOT NULL,
    host TEXT NOT NULL,
    factory_revision TEXT NOT NULL,
    recorded_at REAL NOT NULL,
    effort TEXT NOT NULL DEFAULT 'unknown',
    harness TEXT NOT NULL DEFAULT 'unknown'
)
"""
ADDED_COLUMNS = {"effort": "TEXT NOT NULL DEFAULT 'unknown'", "harness": "TEXT NOT NULL DEFAULT 'unknown'"}

_REVISION = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# A binary that ignores --version may print anything, a secret included.
_VERSION = re.compile(r"[\w .,:;()/+@~-]*\d+\.\d+[\w .,:;()/+@~-]*")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,127}")
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")


@dataclass(frozen=True)
class Exposure:
    model: str
    cli_version: str
    brief_sha256: str
    skill_paths: tuple[str, ...] | None
    host: str
    factory_revision: str
    effort: str
    harness: str


Shell = Callable[[str, Sequence[str]], str | None]


def ensure_table(connection: sqlite3.Connection) -> None:
    connection.execute(DDL)
    present = {row[1] for row in connection.execute(f"PRAGMA table_info({TABLE})")}
    for name, kind in ADDED_COLUMNS.items():
        if name in present:
            continue
        try:
            connection.execute(f"ALTER TABLE {TABLE} ADD COLUMN {name} {kind}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc):
                raise


def write(connection: sqlite3.Connection, job_id: str, exposure: Exposure, at: float) -> None:
    skills = UNKNOWN if exposure.skill_paths is None else json.dumps(list(exposure.skill_paths))
    connection.execute(
        f"INSERT OR REPLACE INTO {TABLE} (job_id, model, cli_version, brief_sha256, skill_paths, "
        "host, factory_revision, recorded_at, effort, harness) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, exposure.model, exposure.cli_version, exposure.brief_sha256, skills,
         exposure.host, exposure.factory_revision, at, exposure.effort, exposure.harness),
    )


_COLUMNS = ("model", "cli_version", "brief_sha256", "skill_paths", "host", "factory_revision", "effort", "harness")


def _unmigrated_columns_as_unknown(present: set[str]) -> str:
    return ", ".join(f"'{UNKNOWN}'" if name in ADDED_COLUMNS and name not in present else name for name in _COLUMNS)


def _rows(connection: sqlite3.Connection, where: str = "", args: tuple = ()) -> list:
    present = {row[1] for row in connection.execute(f"PRAGMA table_info({TABLE})")}
    if not present:
        return []
    columns = _unmigrated_columns_as_unknown(present)
    return connection.execute(f"SELECT job_id, {columns} FROM {TABLE}{where}", args).fetchall()


def _parsed(row: Sequence[str]) -> Exposure:
    model, cli, brief, skills, host, revision, effort, harness = row
    paths = None if skills == UNKNOWN else tuple(json.loads(skills))
    return Exposure(model, cli, brief, paths, host, revision, effort, harness)


def reported_model(value: object) -> str | None:
    return value if isinstance(value, str) and _MODEL.fullmatch(value) else None


def replace_model(connection: sqlite3.Connection, job_id: str, model: str) -> str | None:
    try:
        row = connection.execute(f"SELECT model FROM {TABLE} WHERE job_id = ?", (job_id,)).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return None
        raise
    if row is None or row[0] == model:
        return None
    connection.execute(f"UPDATE {TABLE} SET model = ? WHERE job_id = ?", (model, job_id))
    return row[0]


def read(connection: sqlite3.Connection, job_id: str) -> Exposure | None:
    rows = _rows(connection, " WHERE job_id = ?", (job_id,))
    return _parsed(rows[0][1:]) if rows else None


def read_all(connection: sqlite3.Connection) -> dict[str, Exposure]:
    return {str(row[0]): _parsed(row[1:]) for row in _rows(connection)}


def local_shell(environ: Mapping[str, str]) -> Shell:
    env = {name: value for name, value in environ.items() if name not in (PROOF_ENV, TAG_ENV)}

    def shell(script: str, args: Sequence[str]) -> str | None:
        try:
            proc = subprocess.Popen(
                ["/bin/sh", "-c", script, SHELL_NAME, *args],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            return None
        with proc:
            try:
                out, _ = proc.communicate(timeout=TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
                return None
        if proc.returncode != 0:
            return None
        return out.decode("utf-8", "replace").strip() or None

    return shell


def remote_shell(transport: Transport, target: RemoteTarget, env: Mapping[str, str] | None = None) -> Shell:
    """A link that timed out is not asked again: a lost device delays its start one timeout at most."""
    timed_out: list[bool] = []
    exports = "".join(
        f"export {name}={shlex.quote(value)}; "
        for name, value in sorted((env or {}).items())
        if _ENV_NAME.fullmatch(name) and name not in (PROOF_ENV, TAG_ENV)
    )

    def shell(script: str, args: Sequence[str]) -> str | None:
        if timed_out or not isinstance(transport, ByteTransport):
            return None
        quoted = " ".join(shlex.quote(arg) for arg in args)
        line = f"exec {LOGIN_SHELL} -lc {shlex.quote(exports + script)} {SHELL_NAME} {quoted}"
        result = transport.exchange(ssh_argv(target, line), timeout=TIMEOUT_SECONDS)
        if result.timed_out:
            timed_out.append(True)
            return None
        if result.code != 0:
            return None
        return result.stdout.decode("utf-8", "replace").strip() or None

    return shell


def _toml_string(value: str) -> str:
    try:
        parsed = tomllib.loads(f"model = {value}")["model"]
    except tomllib.TOMLDecodeError:
        parsed = None
    return parsed if isinstance(parsed, str) else value.strip("\"'")


@dataclass(frozen=True)
class Chosen:
    model: str
    effort: str


@dataclass(frozen=True)
class _Command:
    launcher: tuple[str, ...]
    args: tuple[str, ...]
    config_readable: bool


_ENV_FLAGS = {"-0", "--null", "-v", "--debug"}
_ENV_CLEARS = {"-i", "-", "--ignore-environment"}
_ENV_VALUED = {"-u": "--unset", "--unset": "--unset", "-C": "--chdir", "--chdir": "--chdir"}
_CODEX_CONFIG_ENV = {"CODEX_HOME", "HOME"}


def _harness_command_if_followable(argv: Sequence[str]) -> _Command | None:
    if not argv or os.path.basename(argv[0]) != "env":
        return _Command(tuple(argv[:1]), tuple(argv[1:]), True)
    readable = True
    options = True
    words = iter(enumerate(argv[1:], 1))
    for index, word in words:
        if options and word == "--":
            options = False
        elif options and word in _ENV_FLAGS:
            pass
        elif options and word in _ENV_CLEARS:
            readable = False
        elif options and word.startswith("-"):
            if word.startswith("--") and "=" in word:
                name, _, value = word.partition("=")
            elif word[:2] in _ENV_VALUED and len(word) > 2:
                name, value = word[:2], word[2:]
            else:
                name, value = word, None
            if name not in _ENV_VALUED:
                return None
            if value is None:
                _, value = next(words, (None, None))
                if value is None:
                    return None
            readable = readable and _ENV_VALUED[name] == "--unset" and value not in _CODEX_CONFIG_ENV
        elif "=" in word:
            options = False
            readable = readable and word.partition("=")[0] not in _CODEX_CONFIG_ENV
        else:
            return _Command(tuple(argv[: index + 1]), tuple(argv[index + 1:]), readable)
    return None


@dataclass(frozen=True)
class _Selected:
    model: str | None = None
    effort: str | None = None
    profile: str | None = None
    cd: str | None = None
    profile_overridden: bool = False


def _selected(args: Sequence[str], harness: str | None) -> _Selected:
    options = list(args)
    if "--" in options:
        options = options[: options.index("--")]
    codex = harness == "codex"
    short = {"-m": "--model"} | ({"-c": "--config", "-p": "--profile", "-C": "--cd"} if codex else {})
    names = {"--model"} | ({"--config", "--profile", "--cd"} if codex else set())
    names |= {"--effort", "--settings"} if harness == "claude" else set()
    flag = config = effort = config_effort = profile = cd = settings = None
    overridden = False
    words = iter(options)
    for word in words:
        value: str | None
        if word.startswith("--") and "=" in word:
            name, _, value = word.partition("=")
        elif word[:2] in short and len(word) > 2:
            name, value = short[word[:2]], word[2:].removeprefix("=")
        else:
            name, value = short.get(word, word), None
        if name not in names:
            continue
        value = next(words, None) if value is None else value
        if name == "--model":
            flag = value or flag
        elif name == "--effort":
            effort = value or effort
        elif name == "--settings":
            settings = value
        elif name == "--profile":
            profile = value
        elif name == "--cd":
            cd = value
        elif value is not None:
            key, _, setting = value.partition("=")
            key = key.strip()
            if key == "model":
                config = _toml_string(setting) or config
            elif key == "model_reasoning_effort":
                config_effort = _toml_string(setting) or config_effort
            # `profile` and `[profiles.<name>]` select a model only in some codex versions.
            overridden = overridden or key == "profile" or key.startswith("profiles.")
    inline = _inline_settings(settings)
    model, chosen_effort = inline.get("model"), inline.get("effortLevel")
    return _Selected(
        flag or config or (model if isinstance(model, str) else None),
        effort or config_effort or (chosen_effort if isinstance(chosen_effort, str) else None),
        profile,
        cd,
        overridden,
    )


def _inline_settings(value: str | None) -> dict:
    """Claude's `--settings` takes inline JSON or a file path; a file is not read."""
    try:
        parsed = json.loads(value) if value is not None else None
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


_PROFILE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*")
_PROBE = r"""
m=$1 launch=$2 configs=$3 codex_cd=$4 profile=$5 root=$6 ticks=$7
shift 7
version() {
  if [ -n "$launch" ] && ! cd -- "$launch" 2>/dev/null; then
    [ ! -e "$launch.cleanup" ] || exit 1
    case "$*:$PATH" in *..*) exit 1 ;; esac
    command -p mkdir -- "$tmp/launch" && cd -- "$tmp/launch" || exit 1
  fi
  exec "$@" --version
}
bounded_version() {
  tmp=$(command -p mktemp -d) || return 1
  version "$@" >"$tmp/out" 2>/dev/null </dev/null &
  v=$!
  (
    n=0
    while kill -0 "$v" 2>/dev/null; do
      [ "$n" -lt "$ticks" ] || { kill -KILL "$v"; exit 0; }
      command -p sleep 0.1
      n=$((n + 1))
    done
  ) >/dev/null 2>&1 &
  w=$!
  wait "$v" && command -p cat -- "$tmp/out"
  s=$?
  wait "$w"
  command -p rm -rf -- "$tmp"
  return "$s"
}
revision() (
  exec git -C "$root" rev-parse HEAD
)
codex_configs() (
  home=${CODEX_HOME:-$HOME/.codex} d=${launch:-.}
  if [ -n "$codex_cd" ]; then
    case $codex_cd in /*) d=$codex_cd ;; *) d=$d/$codex_cd ;; esac
    [ -d "$d" ] || exit 1
  else
    case $d in /*) ;; *) d=$PWD/$d ;; esac
    until [ -d "$d" ]; do d=${d%/*}; d=${d:-/}; done
  fi
  cd -- "$d" && d=$(pwd -P) || exit 1
  file() { [ -e "$2" ] || return 0; printf '%s-config %s\n' "$m" "$1"; cat -- "$2" || exit 1; echo; }
  file base "$home/config.toml"
  [ -z "$profile" ] || file profile "$home/$profile.config.toml"
  while :; do
    [ "$d/.codex/config.toml" -ef "$home/config.toml" ] || file project "$d/.codex/config.toml"
    [ "$d" = / ] && break
    d=${d%/*}; d=${d:-/}
  done
)
section() {
  name=$1; shift
  out=$("$name" "$@") && printf '%s %s\n%s\n' "$m" "$name" "$out"
}
[ -z "$configs" ] || section codex_configs
[ -z "$root" ] || section revision
[ $# -eq 0 ] || section bounded_version "$@"
exit 0
"""


@dataclass(frozen=True)
class _Probed:
    version: str = UNKNOWN
    revision: str = UNKNOWN
    configs: list[tuple[str, dict]] | None = None


def _sections(text: str | None, marker: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    lines: list[str] | None = None
    for line in (text or "").split("\n"):
        if line.startswith(f"{marker} "):
            lines = found.setdefault(line.removeprefix(f"{marker} "), [])
        elif lines is not None:
            lines.append(line)
    return found


def _probe(
    shell: Shell,
    launch_dir: str | None,
    *,
    launcher: Sequence[str] = (),
    configs_of: _Selected | None = None,
    root: str | None = None,
) -> _Probed:
    if not launcher and configs_of is None and root is None:
        return _Probed()
    marker = f"{SHELL_NAME}-{secrets.token_hex(16)}"
    cd, profile = (configs_of.cd or "", configs_of.profile or "") if configs_of is not None else ("", "")
    text = shell(_PROBE, (
        marker, launch_dir or "", "1" if configs_of is not None else "", cd, profile, root or "",
        str(int(TIMEOUT_SECONDS / 2 / 0.1)), *launcher,
    ))
    sections = _sections(text, marker)
    lines = [line.strip() for line in sections.get("bounded_version", []) if line.strip()]
    first = lines[0][:VERSION_LIMIT].rstrip() if lines else ""
    revision = "\n".join(sections.get("revision", [])).strip()
    configs = sections.get("codex_configs")
    return _Probed(
        first if _VERSION.fullmatch(first) else UNKNOWN,
        revision if _REVISION.fullmatch(revision) else UNKNOWN,
        None if configs is None else _codex_configs(configs, f"{marker}-config"),
    )


def _codex_configs(text: list[str], marker: str) -> list[tuple[str, dict]] | None:
    shown: list[tuple[str, list[str]]] = []
    for line in text:
        if line.startswith(f"{marker} "):
            shown.append((line.removeprefix(f"{marker} "), []))
        elif shown:
            shown[-1][1].append(line)
    try:
        return [(kind, tomllib.loads("\n".join(lines))) for kind, lines in shown]
    except tomllib.TOMLDecodeError:
        return None


_CODEX_KEYS = {"model": "model", "effort": "model_reasoning_effort"}


def _codex_config_wanted(command: _Command | None, selected: _Selected, harness: str | None) -> bool:
    return (
        harness == "codex"
        and command is not None
        and command.config_readable
        and not (selected.model and selected.effort)
        and not selected.profile_overridden
        and (selected.profile is None or _PROFILE.fullmatch(selected.profile) is not None)
    )


def _settled_by_codex_config(selected: _Selected, configs: list[tuple[str, dict]] | None) -> dict[str, object]:
    if configs is None:
        return {}
    files = dict(configs)
    base = files.get("base", {})
    if selected.profile is None and "profile" in base:
        return {}
    if selected.profile is not None and "profile" not in files:
        return {}
    # Codex reads a project's config only in a trusted project, which is not knowable here.
    projects = [parsed for kind, parsed in configs if kind == "project"]
    if any({"profile", "profiles"} & parsed.keys() for parsed in projects):
        return {}
    tables = base.get("profiles")
    legacy = tables.get(selected.profile) if isinstance(tables, dict) and selected.profile is not None else None
    found: dict[str, object] = {}
    for field, key in _CODEX_KEYS.items():
        value = files.get("profile", {}).get(key, base.get(key))
        if any(key in parsed for parsed in projects):
            continue
        if isinstance(legacy, dict) and legacy.get(key, value) != value:
            continue
        found[field] = value
    return found


def _parsed_command(argv: Sequence[str], harness: str | None) -> tuple[_Command | None, _Selected, bool]:
    command = _harness_command_if_followable(argv)
    selected = _selected(command.args, harness) if command is not None else _Selected()
    return command, selected, _codex_config_wanted(command, selected, harness)


def _chosen(command: _Command | None, selected: _Selected, probed: _Probed) -> Chosen:
    if command is None:
        return Chosen(UNKNOWN, UNKNOWN)
    given = {field: value for field, value in (("model", selected.model), ("effort", selected.effort)) if value}
    found = _settled_by_codex_config(selected, probed.configs) | given
    return Chosen(reported_model(found.get("model")) or UNKNOWN, reported_model(found.get("effort")) or UNKNOWN)


def chosen(argv: Sequence[str], harness: str | None, shell: Shell, launch_dir: str | None = None) -> Chosen:
    command, selected, wanted = _parsed_command(argv, harness)
    return _chosen(command, selected, _probe(shell, launch_dir, configs_of=selected if wanted else None))



def parsed(argv: Sequence[str], harness: str | None) -> Chosen:
    """The model and effort `argv` itself names. Reads no file and runs nothing."""
    command, selected, _ = _parsed_command(argv, harness)
    return _chosen(command, selected, _Probed())

def cli_version(binary: str, shell: Shell, launch_dir: str | None = None) -> str:
    return _probe(shell, launch_dir, launcher=(binary,)).version


def factory_revision(root: str | None, shell: Shell) -> str:
    return _probe(shell, None, root=root).revision


def brief_sha256(brief: bytes | None) -> str:
    return hashlib.sha256(brief).hexdigest() if brief else UNKNOWN


def brief_file(path: str | None) -> bytes | None:
    if path is None:
        return None
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def gather(
    *,
    argv: Sequence[str],
    harness: str | None,
    brief: bytes | None,
    host: str,
    factory_root: str | None,
    shell: Shell,
    launch_dir: str | None,
) -> Exposure:
    # lib.brief imports lib.queue, which imports this module.
    from lib.brief.skills import pinned_paths

    command, selected, wanted = _parsed_command(argv, harness)
    probed = _probe(
        shell,
        launch_dir,
        launcher=command.launcher if command is not None else (),
        configs_of=selected if wanted else None,
        root=factory_root,
    )
    found = _chosen(command, selected, probed)
    return Exposure(
        model=found.model,
        cli_version=probed.version,
        brief_sha256=brief_sha256(brief),
        skill_paths=None if brief is None else pinned_paths(brief.decode("utf-8", "replace")),
        host=host or UNKNOWN,
        factory_revision=probed.revision,
        effort=found.effort,
        harness=harness or UNKNOWN,
    )


def record(
    connect: Callable[[], sqlite3.Connection],
    job_id: str,
    gather: Callable[[], Exposure],
    log: Callable[[str], None],
    clock: Callable[[], float] = time.time,
) -> Exposure | None:
    """Gather and store job_id's exposure; log one line either way. Never raises."""
    try:
        found = gather()
        with contextlib.closing(connect()) as connection, connection:
            ensure_table(connection)
            write(connection, job_id, found, clock())
    except Exception as exc:
        log(f"exposure: not recorded ({' '.join(str(exc).split()) or type(exc).__name__})")
        return None
    log(
        f"exposure: recorded (harness {found.harness}, model {found.model}, effort {found.effort}, "
        f"cli {found.cli_version}, host {found.host}, "
        f"revision {found.factory_revision[:12]})"
    )
    return found
