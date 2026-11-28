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
from typing import TYPE_CHECKING

from lib import harnesses
from lib.github_app_token import TOKEN_VARIABLES
from lib.harnesses import AccountIsolation
from lib.harnesses.claude import CLAUDE_BG_WAIT_ENV, CLAUDE_SECURE_STORAGE_ENV

if TYPE_CHECKING:
    from lib.orch_yml import Approvals

CPU_CAP = 4
TEST_WORKERS = 2

_BY_HARNESS = {
    driver.name: dict(driver.extra_env())
    for driver in harnesses.all_drivers()
    if driver.extra_env()
}

ACCOUNT_ISOLATION = MappingProxyType(
    {
        driver.name: isolation
        for driver in harnesses.all_drivers()
        if (isolation := driver.account_isolation()) is not None
    }
)


def cpu_cap_env(cap: int = CPU_CAP) -> dict[str, str]:
    """Vitest 1 and 2 default a pool's minimum to host CPUs - 1 and refuse a
    maximum below it, so each maximum goes with a minimum of 1."""
    workers = str(max(min(TEST_WORKERS, cap - 1), 1))
    return {
        "ORCH_CPU_CAP": str(cap),
        "PYTEST_XDIST_AUTO_NUM_WORKERS": workers,
        "VITEST_MAX_THREADS": workers,
        "VITEST_MAX_FORKS": workers,
        "VITEST_MIN_THREADS": "1",
        "VITEST_MIN_FORKS": "1",
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
    driver = harnesses.find(harness)
    extra = dict(driver.extra_env()) if driver is not None else _BY_HARNESS.get(harness, {})
    env = {**cpu_cap_env(), **extra}
    if home is not None:
        env[_isolation(harness).home] = home
    return env


def account_tokens(harness: str, home: str | None = None) -> tuple[str, ...]:
    return () if home is None else _isolation(harness).tokens


def account_environ(base: Mapping[str, str], harness: str, home: str | None) -> dict[str, str]:
    """base as the account at home sees it; None is the device's own login."""
    dropped = set(account_tokens(harness, home))
    if home is not None:
        driver = harnesses.find(harness)
        if driver is not None:
            dropped.update(driver.dropped_account_env())
    env = {name: value for name, value in base.items() if name not in dropped}
    if home is not None:
        env[_isolation(harness).home] = home
    return env


def _isolation(harness: str) -> AccountIsolation:
    driver = harnesses.find(harness)
    if driver is not None:
        isolation = driver.account_isolation()
        if isolation is not None:
            return isolation
    if harness in ACCOUNT_ISOLATION:
        return ACCOUNT_ISOLATION[harness]
    raise ValueError(f"harness {harness!r} cannot run from an account folder")


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
