"""A sandboxed `codex exec` harness gets the orch state dir as a writable root (#103).

`codex exec --sandbox workspace-write` lets the commands the model runs
write only the working directory (the job worktree) and its temp dirs. The
harness's own `orch job done` writes orch.db, and SQLite also creates the
-wal and -shm files beside it, so in that sandbox it failed with "attempt to
write a readonly database" and every Codex job ended failed (job-failed-2).

`orch job run` passes a box run's command through `with_state_dir`, which
adds `--add-dir <dir of ORCH_DB>` right after `exec` when, and only when,
the argv is a sandboxed codex exec:

* argv[0] is `codex` (any directory), and its first positional word is the
  `exec` subcommand (or its alias `e`);
* the effective sandbox is workspace-write: the last `-s/--sandbox` value,
  else the last `-c/--config sandbox_mode=...`, else `--full-auto` (which
  means workspace-write); `--dangerously-bypass-approvals-and-sandbox`
  means no sandbox at all.

The directory is taken from the resolved ORCH_DB path, never a fixed home
path: its absolute parent (where the waiter lock lives) and, if the path is
a symlink, the real file's parent too (where SQLite puts -wal and -shm). A
directory already given with an absolute `--add-dir` (say, a box-local
waiter config's manual stopgap) is not added again. Everything else (claude,
cursor, an unsandboxed or read-only codex, a wrapper such as `env codex`)
comes back unchanged. Words after `--` are the prompt, never options.

The box's directory is added only if it exists (`os.path.isdir`).

A remote (placed) run is never passed through `with_state_dir`: the remote
device has no orch.db (its harness ends with ORCH-HANDOFF and the waiter
runs `orch job done` on the box), and a box path means nothing there. The
waiter config is shared by every device a bot runs on, so a codex command
written for the box (`/home/box/.local/bin/codex ... --add-dir
/home/box/.local/state/factory`) failed on the PC with exit 127. For a
remote run `for_device` makes the command device-neutral (Factory, Nov 13):

* argv[0] becomes the bare name `codex`, which the device's login shell
  (`bash -lc`, lib/remote.py) resolves through that device's own PATH;
* every `--add-dir` the command names (before `--`) is taken out and given
  back only where the device has that directory: lib/remote.py puts a
  `test -d` guard for each into the start script, so the device decides at
  start time. Codex resolves a relative `--add-dir` against its working
  directory (the last `-C`/`--cd`, itself relative to the job directory),
  so the guard tests it there. No directory is invented for the device:
  machines.yml names no state dir, and the box's ORCH_DB directory is never
  sent.

Any other command (claude, cursor, a wrapper such as `env codex`) comes back
unchanged, with no guarded directories.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

ADD_DIR = "--add-dir"
CODEX = "codex"
WORKSPACE_WRITE = "workspace-write"

_EXEC = frozenset({"exec", "e"})
_CD = frozenset({"-C", "--cd"})
# codex options (global and exec) whose value is the next word.
_VALUE_OPTIONS = frozenset(
    {
        "-a", "--ask-for-approval",
        "-c", "--config",
        "-C", "--cd",
        "-i", "--image",
        "-m", "--model",
        "-o", "--output-last-message",
        "-p", "--profile",
        "-s", "--sandbox",
        "--add-dir",
        "--color",
        "--cyber-access-program",
        "--disable",
        "--enable",
        "--local-provider",
        "--output-schema",
        "--thread-source",
    }
)
_BYPASS = frozenset({"--dangerously-bypass-approvals-and-sandbox", "--yolo"})
_IMPLIES_WORKSPACE_WRITE = frozenset({"--full-auto"})
_SANDBOX_MODE = re.compile(r"""^\s*sandbox_mode\s*=\s*(['"]?)([A-Za-z-]+)\1\s*$""")


def state_dirs(db: Path) -> tuple[Path, ...]:
    """The orch state dir(s) for ORCH_DB: its parent, and its real file's parent."""
    absolute = Path(os.path.abspath(db)).parent
    real = Path(os.path.realpath(db)).parent
    return (absolute,) if absolute == real else (absolute, real)


def is_sandboxed_exec(command: Sequence[str]) -> bool:
    """True when command is a `codex exec` running in workspace-write sandbox mode."""
    argv = list(command)
    return exec_index(argv) is not None and _workspace_write(_options(argv))


def with_state_dir(
    command: Sequence[str], db: Path, *, is_dir: Callable[[str], bool] | None = None
) -> list[str]:
    """`command` with `--add-dir <state dir>` after `exec` if it is a sandboxed codex exec.

    A state dir that does not exist (`is_dir`, default os.path.isdir) is not
    added. Returns a new list; any other command comes back as an unchanged
    copy.
    """
    exists = is_dir or _is_dir
    argv = list(command)
    exec_at = exec_index(argv)
    if exec_at is None:
        return argv
    options = _options(argv)
    if not _workspace_write(options):
        return argv
    present = {
        _same_path(value)
        for name, value in options
        if name == ADD_DIR and value is not None and os.path.isabs(value)
    }
    missing = [
        str(path)
        for path in state_dirs(db)
        if _same_path(str(path)) not in present and exists(str(path))
    ]
    if not missing:
        return argv
    added = [part for path in missing for part in (ADD_DIR, path)]
    return [*argv[: exec_at + 1], *added, *argv[exec_at + 1 :]]


@dataclass(frozen=True)
class DeviceCommand:
    """A harness command for a remote device (Factory, Nov 13).

    argv is what the device runs under `bash -lc`. Each of guarded_dirs is
    given to it as `--add-dir <dir>`, at index insert_at of argv, only when
    `test -d <dir>` holds on the device (lib/remote.py start_script); a
    relative dir is tested under cd, codex's -C/--cd, when argv names one.
    """

    argv: tuple[str, ...]
    guarded_dirs: tuple[str, ...] = ()
    insert_at: int = 0
    cd: str | None = None


def for_device(command: Sequence[str]) -> DeviceCommand:
    """A codex command made device-neutral for a remote run; anything else as is.

    For argv[0] named `codex` (any directory): argv[0] becomes the bare
    `codex`, resolved by the device's login PATH, and each `--add-dir` (or
    `--add-dir=`) before `--` is taken out and becomes a guarded dir, kept
    in order, once each; they go back at the first one's place. cd is the
    last `-C`/`--cd` value before `--`, if any.
    """
    argv = list(command)
    if not argv or Path(argv[0]).name != CODEX:
        return DeviceCommand(tuple(argv))
    kept = [CODEX]
    dirs: list[str] = []
    insert_at: int | None = None
    index = 1
    while index < len(argv):
        part = argv[index]
        if part == "--":
            kept.extend(argv[index:])
            break
        value: str | None = None
        if part == ADD_DIR and index + 1 < len(argv):
            value, index = argv[index + 1], index + 2
        elif part.startswith(ADD_DIR + "="):
            value, index = part.partition("=")[2], index + 1
        if value is not None:
            if insert_at is None:
                insert_at = len(kept)
            if value not in dirs:
                dirs.append(value)
            continue
        kept.append(part)
        index += 1
        if _is_option(part) and part in _VALUE_OPTIONS and index < len(argv):
            kept.append(argv[index])  # an option's value, never an --add-dir
            index += 1
    cd = None
    for name, value in _options(argv):
        if name in _CD and value is not None:
            cd = value
    return DeviceCommand(
        tuple(kept), tuple(dirs), insert_at if insert_at is not None else 0, cd
    )


def exec_index(argv: list[str]) -> int | None:
    """The index of the `exec` subcommand word, if argv is `codex ... exec ...`."""
    if not argv or Path(argv[0]).name != "codex":
        return None
    index = 1
    while index < len(argv):
        part = argv[index]
        if part == "--":
            return None
        if _is_option(part):
            index += 2 if part in _VALUE_OPTIONS else 1
            continue
        return index if part in _EXEC else None
    return None


def _options(argv: list[str]) -> list[tuple[str, str | None]]:
    """Every (option, value) before `--`, with `--opt=v` and `-sV` split apart."""
    found: list[tuple[str, str | None]] = []
    index = 1
    while index < len(argv):
        part = argv[index]
        if part == "--":
            break
        index += 1
        if not _is_option(part):
            continue
        if part.startswith("--") and "=" in part:
            name, _, value = part.partition("=")
            found.append((name, value))
        elif part in _VALUE_OPTIONS:
            found.append((part, argv[index] if index < len(argv) else None))
            index += 1
        elif not part.startswith("--") and len(part) > 2 and part[:2] in _VALUE_OPTIONS:
            found.append((part[:2], part[2:]))  # -sworkspace-write
        else:
            found.append((part, None))
    return found


def _workspace_write(options: list[tuple[str, str | None]]) -> bool:
    if any(name in _BYPASS for name, _ in options):
        return False
    sandbox = config = implied = None
    for name, value in options:
        if name in ("-s", "--sandbox"):
            sandbox = value
        elif name in ("-c", "--config") and value is not None:
            match = _SANDBOX_MODE.match(value)
            if match:
                config = match.group(2)
        elif name in _IMPLIES_WORKSPACE_WRITE:
            implied = WORKSPACE_WRITE
    effective = sandbox if sandbox is not None else config if config is not None else implied
    return effective == WORKSPACE_WRITE


def _is_option(part: str) -> bool:
    return part.startswith("-") and part != "-"


def _is_dir(path: str) -> bool:
    return os.path.isdir(path)


def _same_path(value: str) -> str:
    return os.path.realpath(os.path.normpath(value))
