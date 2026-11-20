from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace

from lib.containers import FINISHED_STATES, Runner, failure
from lib.remote import (
    GONE,
    RUNNING,
    SSH_UNREACHABLE,
    ListedRun,
    RemoteTarget,
    leftovers_script,
    read_leftovers,
    read_reap,
    reap_script,
)

REAPS_PER_SWEEP = 20


@dataclass(frozen=True)
class Keep:
    reason: str


@dataclass(frozen=True)
class Reap:
    pass


Verdict = Keep | Reap


def decide(
    run: ListedRun, job_state: Callable[[str], str | None], held_tags: Callable[[], frozenset[str] | None]
) -> Verdict:
    state = job_state(run.job_id)
    if state is None:
        return Keep("its job is not in orch.db")
    if state not in FINISHED_STATES:
        return Keep(f"its job is {state}")
    held = held_tags()
    if held is None:
        return Keep("the held slots are unreadable")
    if run.tag in held:
        return Keep("a held slot carries its tag")
    if not run.ended:
        return Keep("its run has not ended")
    return Reap()


def plan_leftovers(
    listing: Iterable[ListedRun],
    job_state: Callable[[str], str | None],
    held_tags: Callable[[], frozenset[str] | None],
) -> list[tuple[ListedRun, Verdict]]:
    return [(run, decide(run, job_state, held_tags)) for run in listing]


def sweep(
    runner: Runner,
    base: RemoteTarget,
    job_state: Callable[[str], str | None],
    held_tags: Callable[[], frozenset[str] | None],
) -> list[str]:
    device = base.device
    listed = runner(leftovers_script(f"{base.workdir_root.rstrip('/')}/jobs"))
    if (failed := failure(listed)) is not None:
        return [f"could not list runs on {device} ({failed})"]
    runs, unreadable = read_leftovers(listed.stdout)
    lines = [f"kept {unreadable} unreadable run line(s) on {device}"] if unreadable else []
    reaps = deferred = 0
    for run, verdict in plan_leftovers(runs, job_state, held_tags):
        where = f"run {run.tag} of job {run.job_id} on {device}"
        if isinstance(verdict, Keep):
            lines.append(f"kept {where}: {verdict.reason}")
            continue
        if reaps == REAPS_PER_SWEEP:
            deferred += 1
            continue
        reaps += 1
        reaped = runner(reap_script(replace(base, job_id=run.job_id, tag=run.tag)))
        states = read_reap(reaped.stdout)
        if (failed := failure(reaped)) is not None or states is None:
            lines.append(f"could not sweep {where} ({failed or 'no reap line'})")
            if reaped.code == SSH_UNREACHABLE:
                return lines
        elif states[1] == GONE:
            lines.append(f"killed what was left of {where}")
        elif states[0] != GONE:
            left = "still running" if states[1] == RUNNING else "not known to be gone"
            lines.append(f"kept {where}: {left} after a kill")
    if deferred:
        lines.append(f"deferred {deferred} run(s) on {device} to the next sweep")
    return lines
