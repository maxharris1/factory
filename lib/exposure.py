"""What a plan, build, fix or review job ran under, recorded in orch.db at its start.

Each field is read on the job's own device and is `unknown` when it cannot be
told; recording never delays the job past its reads' timeouts or fails it.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shlex
import sqlite3
import subprocess
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
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
    recorded_at REAL NOT NULL
)
"""

_REVISION = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# A binary that ignores --version may print anything, a secret included.
_VERSION = re.compile(r"[\w .,:;()/+@~-]*\d+\.\d+[\w .,:;()/+@~-]*")


@dataclass(frozen=True)
class Exposure:
    model: str
    cli_version: str
    brief_sha256: str
    skill_paths: tuple[str, ...] | None
    host: str
    factory_revision: str


Shell = Callable[[str, Sequence[str]], str | None]


def ensure_table(connection: sqlite3.Connection) -> None:
    connection.execute(DDL)


def write(connection: sqlite3.Connection, job_id: str, exposure: Exposure, at: float) -> None:
    skills = UNKNOWN if exposure.skill_paths is None else json.dumps(list(exposure.skill_paths))
    connection.execute(
        f"INSERT OR REPLACE INTO {TABLE} (job_id, model, cli_version, brief_sha256, skill_paths, "
        "host, factory_revision, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, exposure.model, exposure.cli_version, exposure.brief_sha256, skills,
         exposure.host, exposure.factory_revision, at),
    )


def read(connection: sqlite3.Connection, job_id: str) -> Exposure | None:
    try:
        row = connection.execute(
            f"SELECT model, cli_version, brief_sha256, skill_paths, host, factory_revision "
            f"FROM {TABLE} WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return None
        raise
    if row is None:
        return None
    model, cli, brief, skills, host, revision = row
    paths = None if skills == UNKNOWN else tuple(json.loads(skills))
    return Exposure(model, cli, brief, paths, host, revision)


def local_shell(environ: Mapping[str, str]) -> Shell:
    env = {name: value for name, value in environ.items() if name not in (PROOF_ENV, TAG_ENV)}

    def shell(script: str, args: Sequence[str]) -> str | None:
        try:
            completed = subprocess.run(
                ["/bin/sh", "-c", script, SHELL_NAME, *args],
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=TIMEOUT_SECONDS,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if completed.returncode != 0:
            return None
        return completed.stdout.decode("utf-8", "replace").strip() or None

    return shell


def remote_shell(transport: Transport, target: RemoteTarget) -> Shell:
    """A link that timed out is not asked again: a lost device delays its start one timeout at most."""
    timed_out: list[bool] = []

    def shell(script: str, args: Sequence[str]) -> str | None:
        if timed_out or not isinstance(transport, ByteTransport):
            return None
        quoted = " ".join(shlex.quote(arg) for arg in args)
        line = f"exec {LOGIN_SHELL} -lc {shlex.quote(script)} {SHELL_NAME} {quoted}"
        result = transport.exchange(ssh_argv(target, line), timeout=TIMEOUT_SECONDS)
        if result.timed_out:
            timed_out.append(True)
            return None
        if result.code != 0:
            return None
        return result.stdout.decode("utf-8", "replace").strip() or None

    return shell


def _toml_model(value: str) -> str:
    try:
        parsed = tomllib.loads(f"model = {value}")["model"]
    except tomllib.TOMLDecodeError:
        parsed = None
    return parsed if isinstance(parsed, str) else value.strip("\"'")


@dataclass(frozen=True)
class _Selected:
    """What a harness command line says about its model."""

    model: str | None = None
    profile: str | None = None
    cd: str | None = None
    profile_overridden: bool = False


def _selected(argv: Sequence[str], harness: str | None) -> _Selected:
    options = list(argv[1:])
    if "--" in options:
        options = options[: options.index("--")]
    codex = harness == "codex"
    short = {"-m": "--model"} | ({"-c": "--config", "-p": "--profile", "-C": "--cd"} if codex else {})
    names = {"--model"} | ({"--config", "--profile", "--cd"} if codex else set())
    flag = config = profile = cd = None
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
        elif name == "--profile":
            profile = value
        elif name == "--cd":
            cd = value
        elif value is not None:
            key, _, setting = value.partition("=")
            key = key.strip()
            if key == "model":
                config = _toml_model(setting) or config
            # `profile` and `[profiles.<name>]` select a model only in some codex versions.
            overridden = overridden or key == "profile" or key.startswith("profiles.")
    return _Selected(flag or config, profile, cd, overridden)


_PROFILE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*")
# $1 marker, $2 launch directory (empty: the shell's own), $3 codex -C, $4 profile.
# A launch directory not made yet has no .codex: its nearest existing ancestor is walked.
_CODEX_CONFIGS = r"""
m=$1 home=${CODEX_HOME:-$HOME/.codex} d=${2:-.}
if [ -n "$3" ]; then
  case $3 in /*) d=$3 ;; *) d=$d/$3 ;; esac
  [ -d "$d" ] || exit 1
else
  until [ -d "$d" ]; do d=$(dirname -- "$d"); done
fi
cd -- "$d" && d=$(pwd -P) || exit 1
show() { [ -e "$2" ] || return 0; printf '%s %s\n' "$m" "$1"; cat -- "$2" || exit 1; echo; }
show base "$home/config.toml"
[ -z "$4" ] || show profile "$home/$4.config.toml"
while :; do
  [ "$d/.codex/config.toml" -ef "$home/config.toml" ] || show project "$d/.codex/config.toml"
  [ "$d" = / ] && break
  d=$(dirname -- "$d")
done
"""


def _codex_configs(text: str | None, marker: str) -> list[tuple[str, dict]] | None:
    """Each (kind, parsed file) the script showed; None if it failed or a file is not TOML."""
    if text is None:
        return None
    shown: list[tuple[str, list[str]]] = []
    for line in text.split("\n"):
        if line.startswith(f"{marker} "):
            shown.append((line.removeprefix(f"{marker} "), []))
        elif shown:
            shown[-1][1].append(line)
    try:
        return [(kind, tomllib.loads("\n".join(lines))) for kind, lines in shown]
    except tomllib.TOMLDecodeError:
        return None


def model(argv: Sequence[str], harness: str | None, shell: Shell, workdir: str | None = None) -> str:
    """The model the harness runs; codex's from its config files as seen from workdir."""
    chosen = _selected(argv, harness)
    if chosen.model:
        return chosen.model
    if harness != "codex" or chosen.profile_overridden:
        return UNKNOWN
    if chosen.profile is not None and not _PROFILE.fullmatch(chosen.profile):
        return UNKNOWN
    marker = f"{SHELL_NAME}-{secrets.token_hex(16)}"
    configs = _codex_configs(
        shell(_CODEX_CONFIGS, (marker, workdir or "", chosen.cd or "", chosen.profile or "")), marker
    )
    if configs is None:
        return UNKNOWN
    # Codex reads a project's config only in a trusted project, which is not knowable here.
    if any({"model", "profile", "profiles"} & parsed.keys() for kind, parsed in configs if kind == "project"):
        return UNKNOWN
    files = dict(configs)
    base = files.get("base", {})
    found = base.get("model")
    if chosen.profile is None:
        if "profile" in base:
            return UNKNOWN
    else:
        if "profile" not in files:
            return UNKNOWN
        found = files["profile"].get("model", found)
        tables = base.get("profiles")
        legacy = tables.get(chosen.profile) if isinstance(tables, dict) else None
        if isinstance(legacy, dict) and legacy.get("model", found) != found:
            return UNKNOWN
    return found if isinstance(found, str) and found else UNKNOWN


def cli_version(binary: str, shell: Shell, workdir: str | None = None) -> str:
    """The first line of `binary --version`, a relative path run from workdir."""
    out = shell(
        'case $1 in /*) ;; */*) [ -z "$2" ] || cd -- "$2" || exit 1 ;; esac; exec "$1" --version',
        (binary, workdir or ""),
    )
    lines = [line.strip() for line in (out or "").splitlines() if line.strip()]
    first = lines[0][:VERSION_LIMIT].rstrip() if lines else ""
    return first if _VERSION.fullmatch(first) else UNKNOWN


def factory_revision(root: str | None, shell: Shell) -> str:
    if root is None:
        return UNKNOWN
    out = shell('exec git -C "$1" rev-parse HEAD', (root,))
    return out if out is not None and _REVISION.fullmatch(out) else UNKNOWN


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
    binary: str,
    harness: str | None,
    brief: bytes | None,
    host: str,
    factory_root: str | None,
    shell: Shell,
    workdir: str | None,
) -> Exposure:
    """workdir: the directory the harness is launched in; None, the shell's own."""
    # lib.brief imports lib.queue, which imports this module.
    from lib.brief.skills import pinned_paths

    return Exposure(
        model=model(argv, harness, shell, workdir),
        cli_version=cli_version(binary, shell, workdir),
        brief_sha256=brief_sha256(brief),
        skill_paths=None if brief is None else pinned_paths(brief.decode("utf-8", "replace")),
        host=host or UNKNOWN,
        factory_revision=factory_revision(factory_root, shell),
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
        with closing(connect()) as connection, connection:
            ensure_table(connection)
            write(connection, job_id, found, clock())
    except Exception as exc:
        log(f"exposure: not recorded ({' '.join(str(exc).split()) or type(exc).__name__})")
        return None
    log(
        f"exposure: recorded (model {found.model}, cli {found.cli_version}, host {found.host}, "
        f"revision {found.factory_revision[:12]})"
    )
    return found
