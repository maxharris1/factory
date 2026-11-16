"""The failed CI checks, their Actions runs and each failed job's log tail.

The tail is cut before it is redacted, so redaction never scans a whole log.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from lib.dispatch.types import FailedCheck

LOG_LINES = 150
LOG_JOBS = 5


@dataclass(frozen=True)
class CIJobLog:
    check: str
    run_id: str | None
    run_url: str | None
    log_tail: str | None
    cut: str | None


@dataclass(frozen=True)
class CIEvidence:
    failed_checks: tuple[str, ...]
    runs: tuple[tuple[str, str], ...]
    jobs: tuple[CIJobLog, ...]


def ci_evidence(
    *,
    failed: Sequence[FailedCheck],
    logs: Mapping[str, str | None],
    limit: int = LOG_LINES,
) -> CIEvidence:
    runs: dict[str, str] = {}
    jobs: list[CIJobLog] = []
    for check in failed:
        if check.run_id is not None and check.run_url is not None:
            runs.setdefault(check.run_id, check.run_url)
        text = logs.get(check.job_id) if check.job_id is not None else None
        tail, cut = _tail(text, limit) if text is not None else (None, None)
        jobs.append(CIJobLog(check.name, check.run_id, check.run_url, tail, cut))
    return CIEvidence(
        failed_checks=tuple(check.name for check in failed),
        runs=tuple(runs.items()),
        jobs=tuple(jobs),
    )


def _tail(text: str, limit: int) -> tuple[str, str | None]:
    from lib.dashboard import redact, redact_key_blocks
    from lib.dispatch.errors_text import _redact

    lines = text.rstrip().splitlines()
    cut = None
    if len(lines) > limit:
        cut = f"[cut {len(lines) - limit} earlier lines]"
        lines = lines[len(lines) - limit :]
    kept = [_redact(redact(line)) for line in redact_key_blocks(lines)]
    return "\n".join([cut, *kept] if cut else kept), cut
