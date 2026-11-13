"""The per-run secret that lets an autostart run finish its own job (#96).

`orch job run` mints a token for each autostart claim it runs, records only
its SHA-256 on the job's device slot, and hands the token to the harness in
ORCH_JOB_TOKEN and in a private file named by ORCH_JOB_PROOF_FILE. `orch job
done` for that job is accepted only from a process that presents the token.

A process-tree check cannot do this: a harness sandbox with its own PID
namespace (Codex's Linux sandbox) cannot see `orch job run` as an ancestor,
but it inherits the environment and can read the run's files. The file is
there for a harness that drops variables whose names contain TOKEN from the
commands it runs.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import tempfile
from collections.abc import Mapping
from pathlib import Path

TOKEN_ENV = "ORCH_JOB_TOKEN"
PROOF_FILE_ENV = "ORCH_JOB_PROOF_FILE"


def mint() -> tuple[str, str]:
    """A fresh token and the hash that goes on the slot."""
    token = secrets.token_urlsafe(32)
    return token, digest(token)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def presented(environ: Mapping[str, str]) -> str | None:
    """The token this process holds: ORCH_JOB_TOKEN, else the proof file's."""
    token = environ.get(TOKEN_ENV, "").strip()
    if token:
        return token
    path = environ.get(PROOF_FILE_ENV, "").strip()
    if not path:
        return None
    try:
        token = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return token or None


def matches(token: str | None, stored: object) -> bool:
    if token is None or not isinstance(stored, str) or not stored:
        return False
    return hmac.compare_digest(digest(token), stored)


def write_proof_file(token: str, directory: Path | None) -> Path:
    """Write the token to a new owner-only file in `directory` (else the temp dir)."""
    descriptor, name = tempfile.mkstemp(prefix="job-proof-", dir=directory)
    try:
        os.write(descriptor, token.encode("utf-8"))
    finally:
        os.close(descriptor)
    return Path(name)


def remove_proof_file(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
