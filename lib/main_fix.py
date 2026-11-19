"""Main-fix filing (issue #1061): one GitHub issue per red streak on a base branch.

A streak runs from the first red read of a base's required checks to the
first green read. Its row in `feeder_main_fix` holds the issue's marker, so
a retry after a crash finds the issue it already filed. This module decides
and renders; lib/feeder.py reads GitHub and writes the row.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from lib.evidence.ci import CIEvidence, MainRed, failing_tests

MAIN_FIX_TABLE = "feeder_main_fix"
LOGGED_JOBS = 3
_FENCE_LINE = re.compile(r"^(\s*)(?=```|~~~)")


@dataclass(frozen=True)
class MainFixRow:
    repo: str
    base: str
    marker: str
    state: Literal["filing", "open", "closed"]
    issue: int | None
    last_head: str
    filed_at: float


@dataclass(frozen=True)
class MainRead:
    red: MainRed | None
    failed: bool


@dataclass(frozen=True)
class File:
    red: MainRed


@dataclass(frozen=True)
class Resume:
    row: MainFixRow
    red: MainRed


@dataclass(frozen=True)
class Retire:
    row: MainFixRow


Action = File | Resume | Retire


def decide(row: MainFixRow | None, read: MainRead, *, issue_open: bool | None) -> tuple[Action, ...]:
    """What to do for one base, given its active row (filing or open) and
    this poll's read. issue_open is None when the issue's state is unknown."""
    if read.failed:
        return ()
    if read.red is None:
        return () if row is None else (Retire(row),)
    if row is None:
        return (File(read.red),)
    if row.state == "filing":
        return (Resume(row, read.red),)
    if issue_open is False:
        return (Retire(row), File(read.red))
    return ()


def marker(repo: str, base: str, filed_at: float) -> str:
    return f"<!-- factory-main-fix id={repo.lower()}:{base}:{filed_at} -->"


def issue_title(red: MainRed) -> str:
    names = ", ".join(check.name for check in red.checks)
    return f"Main is red at {red.head[:8]}: {names}"[:200]


def issue_body(
    repo: str,
    base: str,
    required: Sequence[str],
    red: MainRed,
    evidence: CIEvidence,
    marker: str,
) -> str:
    from lib.feeder import BODY_LIMIT, _fence

    patterns = ", ".join(f"`{pattern}`" for pattern in required)
    tests = dict.fromkeys(
        test for job in evidence.jobs for test in failing_tests(job.log_tail or "")
    )
    lines = [
        marker,
        "Risk: high",
        "Plan first: no",
        "Depends on: none",
        "Type: bug-fix",
        "",
        f"`{base}` is red at `{red.head}`. The feeder starts no other ticket on `{base}` "
        "until its required checks pass. Fix the failures below.",
        "",
        "## Acceptance criteria",
        "",
        f"- The required checks ({patterns}) pass on `{base}`.",
        "",
        "## Red checks",
        "",
        *(f"- `{job.check}`: {job.run_url or 'no run link'}" for job in evidence.jobs),
    ]
    if tests:
        lines += ["", "## Failing tests", "", *(f"- `{test}`" for test in tests)]
    lines += ["", "## Logs"]
    slots: list[tuple[int, str, str | None]] = []
    for job in evidence.jobs[:LOGGED_JOBS]:
        lines += ["", f"### `{job.check}`", ""]
        tail = _escaped(job.log_tail)
        if tail is None:
            lines.append("Log unavailable.")
        else:
            fence = _fence(tail)
            lines += [fence, "", fence]
            slots.append((len(lines) - 2, tail, job.cut))
    rest = evidence.jobs[LOGGED_JOBS:]
    if rest:
        lines += ["", "Other red jobs: " + ", ".join(f"`{job.check}`" for job in rest) + "."]
    if slots:
        room = (BODY_LIMIT - len("\n".join(lines)) - 1) // len(slots)
        for slot, tail, cut in slots:
            lines[slot] = _fit(tail, cut, room)
    return "\n".join(lines) + "\n"


def _escaped(tail: str | None) -> str | None:
    """The tail with fence-like lines broken, since lib/feeder._unfenced and
    parse_gate_lines toggle on any of them whatever its length."""
    if tail is None:
        return None
    return "\n".join(_FENCE_LINE.sub(r"\1\\", line) for line in tail.splitlines())


def _fit(tail: str, cut: str | None, room: int) -> str:
    """The tail's last lines that fit room, under one cut line counting
    every earlier line dropped here or by the log read."""
    lines = tail.splitlines()
    earlier = 0
    if cut is not None:
        earlier = int(re.findall(r"\d+", cut)[0])
        lines = lines[1:]
    budget = max(0, room - len(f"[cut {earlier + len(lines)} earlier lines]") - 1)
    kept: list[str] = []
    size = 0
    for line in reversed(lines):
        if size + len(line) + 1 > budget:
            break
        kept.append(line)
        size += len(line) + 1
    if not kept and lines:
        kept = [lines[-1][max(0, len(lines[-1]) - budget):]]
    dropped = earlier + len(lines) - len(kept)
    return "\n".join([*([f"[cut {dropped} earlier lines]"] if dropped else []), *reversed(kept)])
