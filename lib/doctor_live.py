from __future__ import annotations

import json
from collections.abc import Callable, Collection, Sequence
from pathlib import Path

from lib.harnesses import all_drivers
from lib.machines_yml import MachinesConfig
from lib.pstack_pin import canary_prompt_for, score_reply
from lib.remote import RemoteError
from lib.skill_probe import Spawn, plan_canary, spawn, start_launch
from lib.telemetry import final_message
from lib.waiter import WaiterError, default_harness_config, load_harness_command


def waiter_command(bot: str) -> Sequence[str]:
    return load_harness_command(default_harness_config(bot), bot).command


def last_reply(stdout: str) -> str:
    """The reply from the last `started-by:` line on, from plain or JSON-event stdout."""
    parts: list[str] = []
    streaming = False
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        if not isinstance(event, dict):
            parts.append(line + "\n")
            streaming = False
            continue
        message = final_message(event)
        delta = message is not None and event.get("delta") is True
        if message is not None:
            parts.append(message if delta and streaming else "\n" + message)
        streaming = delta
    lines = "".join(parts).splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip().startswith("started-by:")]
    return "\n".join(lines[starts[-1]:] if starts else lines)


def live_canary(
    machines: MachinesConfig,
    refused: Collection[str],
    token: str,
    *,
    harnesses: Collection[str] | None,
    timeout: float,
    work: Path,
    run_id: str,
    load: Callable[[str], Sequence[str]] = waiter_command,
    run: Spawn = spawn,
    out: Callable[[str], None] = print,
) -> list[str]:
    drivers = {d.name: d for d in all_drivers() if harnesses is None or d.name in harnesses}
    commands: dict[str, Sequence[str]] = {}
    unreadable: dict[str, str] = {}
    for name, driver in drivers.items():
        if name in refused:
            continue
        try:
            commands[name] = load(driver.bot_user)
        except WaiterError as exc:
            unreadable[name] = str(exc)
    rows = plan_canary(machines, refused, commands, run_id=run_id, local_root=str(work),
                       drivers=list(drivers.values()))
    if not rows:
        return [f"machines.yml enables no {' or '.join(drivers)} device"]
    work.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    for row in rows:
        target = f"{row.harness}@{row.device}"
        if row.harness in unreadable:
            errors.append(f"{target}: {unreadable[row.harness]}; "
                          f"run bin/orch doctor --live --harness {row.harness} as {row.bot}")
            continue
        if row.launch is None:
            out(f"{target}: skipped ({row.skip})")
            continue
        log = work / f"{run_id}-{row.launch.name}.log"
        try:
            code = start_launch(row.launch, canary_prompt_for(drivers[row.harness]),
                                timeout=timeout, log_path=log, run=run)
        except OSError as exc:
            errors.append(f"{target}: did not load the pinned version (could not start: {exc})")
            continue
        except RemoteError as exc:
            errors.append(f"{target}: did not load the pinned version ({exc})")
            continue
        if code is None:
            error = f"{target}: did not load the pinned version (timed out after {timeout:g}s)"
        else:
            reply = last_reply(log.read_text(encoding="utf-8", errors="replace"))
            error = score_reply(target, row.bot, reply, token)
        if error is None:
            out(f"{target}: ok")
        else:
            errors.append(error)
    return errors
