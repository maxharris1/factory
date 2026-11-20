"""Main-fix filing (issue #1061): one GitHub issue per red streak on a base branch.

A streak runs from the first red read of a base's required checks to the
first green read. Its row in `feeder_main_fix` holds the issue's marker and
the title and body rendered when the streak began, so a retry after a crash
finds the issue it already filed and posts what the first red head showed.
This module decides and renders; lib/feeder.py reads GitHub and writes the row.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from lib.evidence.ci import CIEvidence, MainRed, failing_tests

MAIN_FIX_TABLE = "feeder_main_fix"
LOGGED_JOBS = 3
LINE_CHARS = 300
CHECKS_CHARS = 2_000
TESTS_CHARS = 3_000
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
    title: str
    body: str


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


@dataclass(frozen=True)
class Retire:
    row: MainFixRow


@dataclass(frozen=True)
class Comment:
    row: MainFixRow
    red: MainRed


@dataclass(frozen=True)
class Close:
    row: MainFixRow


Action = File | Resume | Retire | Comment | Close


def decide(
    row: MainFixRow | None, read: MainRead, *, issue_open: bool | None, feed_begun: bool
) -> tuple[Action, ...]:
    """What to do for one base, given its active row (filing or open) and
    this poll's read. issue_open is None when the issue's state is unknown."""
    if read.failed:
        return ()
    if read.red is None:
        if row is None:
            return ()
        if row.state == "filing" or feed_begun or issue_open is False:
            return (Retire(row),)
        return () if issue_open is None else (Close(row),)
    if row is None:
        return (File(read.red),)
    if row.state == "filing":
        return (Resume(row),)
    if issue_open is False:
        return (Retire(row), File(read.red))
    if issue_open is True and read.red.head != row.last_head:
        return (Comment(row, read.red),)
    return ()


def marked_issue_fate(row: MainFixRow, number: int) -> Literal["feed", "hold", "close"]:
    if row.state == "filing":
        return "hold"
    return "feed" if number == row.issue else "close"


def marker(repo: str, base: str, filed_at: float) -> str:
    return f"<!-- factory-main-fix id={repo.lower()}:{base}:{filed_at} -->"


def head_marker(row: MainFixRow, head: str) -> str:
    return f"<!-- factory-main-fix-head id={row.repo.lower()}:{row.base}:{row.filed_at} head={head} -->"


def close_marker(row: MainFixRow) -> str:
    return f"<!-- factory-main-fix-close id={row.repo.lower()}:{row.base}:{row.filed_at} -->"


def close_comment(row: MainFixRow) -> str:
    return (
        f"{close_marker(row)}\n\n`{row.base}` is green again and no build started on this issue, "
        "so the feeder closes it.\n"
    )


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
    patterns = ", ".join(f"`{pattern}`" for pattern in required)
    return _with_evidence(
        [
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
        ],
        evidence,
    )


def head_comment(base: str, red: MainRed, evidence: CIEvidence, marker: str) -> str:
    return _with_evidence([marker, "", f"`{base}` is still red, now at `{red.head}`."], evidence)


def _with_evidence(lines: list[str], evidence: CIEvidence) -> str:
    from lib.feeder import BODY_LIMIT, _fence

    tests = dict.fromkeys(
        test for job in evidence.jobs for test in failing_tests(job.log_tail or "")
    )
    lines = [
        *lines,
        "",
        "## Red checks",
        "",
        *_bounded(
            [f"- `{_clip(job.check)}`: {job.run_url or 'no run link'}" for job in evidence.jobs],
            CHECKS_CHARS,
        ),
    ]
    if tests:
        listed = _bounded([f"- `{_clip(test)}`" for test in tests], TESTS_CHARS)
        lines += ["", "## Failing tests", "", *listed]
    lines += ["", "## Logs"]
    slots: list[tuple[int, str]] = []
    for job in evidence.jobs[:LOGGED_JOBS]:
        lines += ["", f"### `{_clip(job.check)}`", ""]
        tail = _break_fences(job.log_tail)
        if tail is None:
            lines.append("Log unavailable.")
        else:
            fence = _fence(tail)
            lines += [fence, "", fence]
            slots.append((len(lines) - 2, tail))
    if len(evidence.jobs) > LOGGED_JOBS:
        lines += ["", f"Logs are shown for the first {LOGGED_JOBS} red jobs only."]
    if slots:
        room = (BODY_LIMIT - len("\n".join(lines)) - 1) // len(slots)
        for slot, tail in slots:
            lines[slot] = _fit(tail, room)
    return "\n".join(lines) + "\n"


def _clip(text: str) -> str:
    return text if len(text) <= LINE_CHARS else text[: LINE_CHARS - 1] + "…"


def _bounded(items: Sequence[str], budget: int) -> list[str]:
    size = 0
    for count, item in enumerate(items):
        size += len(item) + 1
        if size > budget:
            return [*items[:count], f"- and {len(items) - count} more."]
    return list(items)


def _break_fences(tail: str | None) -> str | None:
    if tail is None:
        return None
    return "\n".join(_FENCE_LINE.sub(r"\1\\", line) for line in tail.splitlines())


def _fit(tail: str, room: int) -> str:
    lines = tail.splitlines()
    budget = max(0, room - len(f"[cut {len(lines)} earlier lines]") - 1)
    kept: list[str] = []
    size = 0
    for line in reversed(lines):
        if size + len(line) + 1 > budget:
            break
        kept.append(line)
        size += len(line) + 1
    if not kept and lines:
        kept = [lines[-1][max(0, len(lines[-1]) - budget):]]
    dropped = len(lines) - len(kept)
    return "\n".join([*([f"[cut {dropped} earlier lines]"] if dropped else []), *reversed(kept)])
