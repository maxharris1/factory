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
from pathlib import PurePath
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
    A profile inside the run's HOME is refused, since gh finds
    $HOME/.config/gh by itself. Paths are compared, never opened."""
    env = dict(environ)
    if approvals is None or not approvals.app_trust or approvals.gh_config_dir is None:
        return env
    profile = _normal(approvals.gh_config_dir)
    home = env.get("HOME") or os.path.expanduser("~")
    if _within(profile, _normal(home)):
        raise ApproverProfileExposed(
            f"the approver gh profile {profile} is inside the run's HOME {home}"
        )
    if "GH_CONFIG_DIR" in env and _within(_normal(env["GH_CONFIG_DIR"]), profile):
        del env["GH_CONFIG_DIR"]
    if "XDG_CONFIG_HOME" in env and _within(_normal(env["XDG_CONFIG_HOME"]) / "gh", profile):
        del env["XDG_CONFIG_HOME"]
    return env


def _normal(path: str | os.PathLike[str]) -> PurePath:
    return PurePath(os.path.abspath(os.fspath(path)))


def _within(path: PurePath, parent: PurePath) -> bool:
    return path == parent or parent in path.parents
