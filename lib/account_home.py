from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from lib.machines_yml import ACCOUNTS_DIR, MachinesConfig
from lib.pstack_pin import CLAUDE_EXTRAS, CLAUDE_HOME, RULES_PATH

Action = Literal["made", "mode", "linked", "conflict"]
CLAUDE_LINKS = ("skills", *CLAUDE_EXTRAS, RULES_PATH.parent.name, "settings.json")
LINKS = {"claude": CLAUDE_LINKS}
PRIVATE = 0o700


@dataclass(frozen=True)
class Change:
    action: Action
    path: str
    detail: str


class Refused(ValueError):
    pass


def prepare(
    config: MachinesConfig,
    device_name: str,
    user: str,
    home: Path,
    account: str | None = None,
) -> list[Change]:
    folders = _folders(config, device_name, user, home, account)
    if not folders:
        return []
    root = _ensure_dir(home / ACCOUNTS_DIR)
    if root is not None and root.action == "conflict":
        return [root]
    changes = [root]
    for harness, folder in folders:
        made = _ensure_dir(folder)
        changes.append(made)
        if made is None or made.action != "conflict":
            changes.extend(
                _link(folder / name, str(home / CLAUDE_HOME / name))
                for name in LINKS.get(harness, ())
            )
    return [change for change in changes if change is not None]


def _folders(
    config: MachinesConfig, device_name: str, user: str, home: Path, account: str | None
) -> list[tuple[str, Path]]:
    device = config.device(device_name)
    if device is None:
        raise Refused(f"unknown device {device_name!r}")
    harnesses = [harness for harness in device.harnesses if device.login_for(harness) == user]
    if not harnesses:
        raise Refused(f"{user!r} is not a bot login on {device_name!r}")
    for harness in harnesses:
        configured = device.home_for(harness)
        if configured is None:
            raise Refused(f"device {device_name!r} home is an install placeholder")
        if Path(configured) != home:
            raise Refused(f"home {home} is not {configured}, {user}'s home on {device_name!r}")
    selected = [
        (harness, item)
        for harness in harnesses
        for item in config.accounts_on(device_name, harness)[1:]
        if account is None or item == account
    ]
    if account is not None and not selected:
        raise Refused(f"{user!r} has no account {account!r} to prepare on {device_name!r}")
    return [
        (harness, Path(config.account_home(device_name, harness, item)))
        for harness, item in selected
    ]


def _ensure_dir(path: Path) -> Change | None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        os.mkdir(path, PRIVATE)
        os.chmod(path, PRIVATE)
        return Change("made", str(path), "mode 700")
    if not stat.S_ISDIR(info.st_mode):
        return Change("conflict", str(path), "not a directory; left as is")
    mode = stat.S_IMODE(info.st_mode)
    if mode != PRIVATE:
        os.chmod(path, PRIVATE)
        return Change("mode", str(path), f"{mode:o} -> 700")
    return None


def _link(path: Path, target: str) -> Change | None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        os.symlink(target, path)
        return Change("linked", str(path), f"-> {target}")
    if stat.S_ISLNK(info.st_mode) and os.readlink(path) == target:
        return None
    return Change("conflict", str(path), f"not a link to {target}; left as is")
