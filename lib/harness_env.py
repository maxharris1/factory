"""Environment a job's harness needs for an unattended run (#105).

Keyed by the job's harness (its queue recipient), never by a binary name:
orch passes the bot's own command through unchanged and only adds these
variables. Claude print mode stops background tasks a while after its last
turn, which killed a fix job's test run before it pushed; a ceiling of 0
lifts that. Claude reads it only in print mode.
"""

from __future__ import annotations

CLAUDE_BG_WAIT_ENV = "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"

_BY_HARNESS = {"claude": {CLAUDE_BG_WAIT_ENV: "0"}}


def harness_env(harness: str) -> dict[str, str]:
    return dict(_BY_HARNESS.get(harness, {}))
