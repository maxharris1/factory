"""The failed CI checks and the failed log, for the evidence bundle (#163).

The caller passes the failed check names, the run URL and a fetch callable
that returns the run's failed log for that URL; lib/github_actions.py is not
read here. Without a run URL nothing is fetched and the log tail is empty.

The log tail is the last `limit` lines of the log (LOG_LINES by default),
not counting blank lines at the end. A longer log is cut from the start, and
the tail opens with a note of how many lines were cut.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

LOG_LINES = 150


@dataclass(frozen=True)
class CIEvidence:
    failed_checks: tuple[str, ...]
    run_url: str | None
    log_tail: str
    cut: str | None


def ci_evidence(
    *,
    failed_checks: Sequence[str],
    run_url: str | None,
    fetch_log: Callable[[str], str],
    limit: int = LOG_LINES,
) -> CIEvidence:
    """The failed checks, the run URL and the log tail. See the module doc."""
    if run_url is None:
        return CIEvidence(tuple(failed_checks), None, "", None)
    lines = fetch_log(run_url).rstrip().splitlines()
    cut = None
    if len(lines) > limit:
        cut = f"[cut {len(lines) - limit} earlier lines]"
        lines = [cut, *lines[len(lines) - limit :]]
    return CIEvidence(tuple(failed_checks), run_url, "\n".join(lines), cut)
