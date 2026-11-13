"""Environment a job's harness needs for an unattended run (#105).

Keyed by the job's harness (its queue recipient), never by a binary name:
orch passes the bot's own command through unchanged and only adds these
variables. Claude print mode stops background tasks a while after its last
turn, which killed a fix job's test run before it pushed; a ceiling of 0
lifts that. Claude reads it only in print mode.

harness_environ keeps Max's approver gh profile out of every run once the
GitHub App switch is on (#122).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lib.orch_yml import Approvals

CLAUDE_BG_WAIT_ENV = "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"

_BY_HARNESS = {"claude": {CLAUDE_BG_WAIT_ENV: "0"}}


def harness_env(harness: str) -> dict[str, str]:
    return dict(_BY_HARNESS.get(harness, {}))


class ApproverProfileExposed(Exception):
    """Max's approver gh profile sits where a harness run would find it."""


def harness_environ(
    environ: Mapping[str, str],
    approvals: Approvals | None,
) -> dict[str, str]:
    """The environment a harness run inherits (#122). With the app switch on
    it never names Max's approver gh profile: GH_CONFIG_DIR at or inside the
    profile, or XDG_CONFIG_HOME whose gh directory is there, is dropped.
    The run is refused when the profile is inside its HOME or its default
    gh directory, $HOME/.config/gh, is the profile. Symlinks and bind mounts
    count as the profile: paths are resolved and stat'ed, never opened."""
    env = dict(environ)
    if approvals is None or not approvals.app_trust or approvals.gh_config_dir is None:
        return env
    profile = approvals.gh_config_dir
    home = env.get("HOME") or os.path.expanduser("~")
    if _within(profile, home) or _within(os.path.join(home, ".config", "gh"), profile):
        raise ApproverProfileExposed(
            f"the approver gh profile {profile} is reachable from the run's HOME {home}"
        )
    if "GH_CONFIG_DIR" in env and _within(env["GH_CONFIG_DIR"], profile):
        del env["GH_CONFIG_DIR"]
    if "XDG_CONFIG_HOME" in env and _within(os.path.join(env["XDG_CONFIG_HOME"], "gh"), profile):
        del env["XDG_CONFIG_HOME"]
    return env


def _within(path: str | os.PathLike[str], parent: str | os.PathLike[str]) -> bool:
    """path is parent or below it, through any symlink or bind mount."""
    resolved = Path(os.path.realpath(path))
    target = Path(os.path.realpath(parent))
    if resolved == target or target in resolved.parents:
        return True
    try:
        mark = os.stat(target)
    except OSError:
        return False
    for candidate in (resolved, *resolved.parents):
        try:
            if os.path.samestat(os.stat(candidate), mark):
                return True
        except OSError:
            continue
    return False
