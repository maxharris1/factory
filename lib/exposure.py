"""What a plan, build, fix or review job ran under, recorded in orch.db at its start.

Each field is read on the job's own device and is `unknown` when it cannot be
told; recording never delays the job past its reads' timeouts or fails it.
"""

from __future__ import annotations

import hashlib
import json
import re
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


def _argv_model(argv: Sequence[str], harness: str | None) -> str | None:
    options = list(argv[1:])
    if "--" in options:
        options = options[: options.index("--")]
    flag = config = None
    words = iter(options)
    for word in words:
        if word in ("--model", "-m"):
            flag = next(words, None) or flag
        elif word.startswith("--model="):
            flag = word.removeprefix("--model=") or flag
        elif harness == "codex" and word in ("-c", "--config"):
            setting = next(words, "")
            if setting.startswith("model="):
                config = _toml_model(setting.removeprefix("model=")) or config
        elif harness == "codex" and word.startswith("--config=model="):
            config = _toml_model(word.removeprefix("--config=model=")) or config
    return flag or config


def model(argv: Sequence[str], harness: str | None, shell: Shell) -> str:
    found = _argv_model(argv, harness)
    if found:
        return found
    if harness != "codex":
        return UNKNOWN
    text = shell('exec cat -- "${CODEX_HOME:-$HOME/.codex}/config.toml"', ())
    if text is None:
        return UNKNOWN
    try:
        configured = tomllib.loads(text).get("model")
    except tomllib.TOMLDecodeError:
        return UNKNOWN
    return configured if isinstance(configured, str) and configured else UNKNOWN


def cli_version(binary: str, shell: Shell) -> str:
    out = shell('exec "$1" --version', (binary,))
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
) -> Exposure:
    # lib.brief imports lib.queue, which imports this module.
    from lib.brief.skills import pinned_paths

    return Exposure(
        model=model(argv, harness, shell),
        cli_version=cli_version(binary, shell),
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
