"""Environment a job's harness needs for an unattended run (#105).

Keyed by the job's harness (its queue recipient), never by a binary name:
orch passes the bot's own command through unchanged and only adds these
variables. Claude print mode stops background tasks a while after its last
turn, which killed a fix job's test run before it pushed; a ceiling of 0
lifts that. Claude reads it only in print mode.

Every harness also gets cpu_cap_env (#1221): test runners default to one
worker per host CPU, and the cap contains that. It is a constant in code,
not a machines.yml knob.

harness_environ keeps Max's approver gh profile out of every run once the
GitHub App switch is on (#122).
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, NamedTuple

from lib.github_app_token import TOKEN_VARIABLES

if TYPE_CHECKING:
    from lib.orch_yml import Approvals

CLAUDE_BG_WAIT_ENV = "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"
CPU_CAP = 4

_BY_HARNESS = {"claude": {CLAUDE_BG_WAIT_ENV: "0"}}


class AccountIsolation(NamedTuple):
    home: str
    tokens: tuple[str, ...]


ACCOUNT_ISOLATION = MappingProxyType(
    {
        "claude": AccountIsolation(
            "CLAUDE_CONFIG_DIR",
            ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
        ),
        "codex": AccountIsolation("CODEX_HOME", ("OPENAI_API_KEY", "CODEX_API_KEY")),
    }
)


def cpu_cap_env(cap: int = CPU_CAP) -> dict[str, str]:
    workers = str(cap - 1)
    return {
        "ORCH_CPU_CAP": str(cap),
        "PYTEST_XDIST_AUTO_NUM_WORKERS": workers,
        "VITEST_MAX_THREADS": workers,
        "VITEST_MAX_FORKS": workers,
    }


def cpu_window(allowed: Iterable[int], cap: int, tag: str) -> frozenset[int] | None:
    """cap consecutive CPUs of allowed, starting where the run's tag points,
    so concurrent runs spread across the host; None when allowed fits the cap."""
    cpus = sorted(allowed)
    if len(cpus) <= cap:
        return None
    start = int(tag[:8], 16) % len(cpus)
    return frozenset(cpus[(start + i) % len(cpus)] for i in range(cap))


def harness_env(harness: str, home: str | None = None) -> dict[str, str]:
    env = {**cpu_cap_env(), **_BY_HARNESS.get(harness, {})}
    if home is not None:
        env[_isolation(harness).home] = home
    return env


def account_tokens(harness: str, home: str | None = None) -> tuple[str, ...]:
    return () if home is None else _isolation(harness).tokens


def _isolation(harness: str) -> AccountIsolation:
    if harness not in ACCOUNT_ISOLATION:
        raise ValueError(f"harness {harness!r} cannot run from an account folder")
    return ACCOUNT_ISOLATION[harness]


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
    count as the profile, and a gh directory holding a symlink or hard link
    to one of the profile's files counts as the profile: paths are resolved
    and stat'ed, never opened."""
    env = {name: value for name, value in environ.items() if name not in TOKEN_VARIABLES}
    if approvals is None or not approvals.app_trust or approvals.gh_config_dir is None:
        return env
    profile = approvals.gh_config_dir
    profile_files = _file_ids(profile)
    home = env.get("HOME") or os.path.expanduser("~")

    def exposes(gh_dir: str) -> bool:
        return _within(gh_dir, profile) or not profile_files.isdisjoint(_file_ids(gh_dir))

    if _within(profile, home) or exposes(os.path.join(home, ".config", "gh")):
        raise ApproverProfileExposed(
            f"the approver gh profile {profile} is reachable from the run's HOME {home}"
        )
    if "GH_CONFIG_DIR" in env and exposes(env["GH_CONFIG_DIR"]):
        del env["GH_CONFIG_DIR"]
    if "XDG_CONFIG_HOME" in env and exposes(os.path.join(env["XDG_CONFIG_HOME"], "gh")):
        del env["XDG_CONFIG_HOME"]
    return env


def _file_ids(directory: str | os.PathLike[str]) -> set[tuple[int, int]]:
    """(device, inode) of each regular file directly in directory, through
    symlinks. Hard links and symlinks to one file share an id."""
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return set()
    ids = set()
    for entry in entries:
        try:
            info = os.stat(entry.path)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            ids.add((info.st_dev, info.st_ino))
    return ids


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
