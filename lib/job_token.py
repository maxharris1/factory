"""The per-run secret that lets an autostart run finish its own job (#96).

`orch job run` mints a token for each autostart claim it runs, records only
its SHA-256 on the job's device slot, and hands the token to the harness in
the ORCH_JOB_PROOF environment variable, never on disk. `orch job done` for
that job is accepted only from a process that presents the token.

A process-tree check cannot do this: a harness sandbox with its own PID
namespace (Codex's Linux sandbox) cannot see `orch job run` as an ancestor,
but it inherits the environment. The variable's name has no KEY, SECRET or
TOKEN in it, so Codex's default shell_environment_policy passes it on to the
commands the harness runs. A file would not do: the waiter and the bot's
other sessions run as the same Unix user, so any of them could find it.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Mapping

PROOF_ENV = "ORCH_JOB_PROOF"


def mint() -> tuple[str, str]:
    """A fresh token and the hash that goes on the slot."""
    token = secrets.token_urlsafe(32)
    return token, digest(token)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def presented(environ: Mapping[str, str]) -> str | None:
    """The token this process holds, from ORCH_JOB_PROOF."""
    return environ.get(PROOF_ENV, "").strip() or None


def matches(token: str | None, stored: object) -> bool:
    if token is None or not isinstance(stored, str) or not stored:
        return False
    return hmac.compare_digest(digest(token), stored)
