"""The failed CI checks, their Actions runs and each failed job's log tail."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from lib.dispatch.types import Cause, FailedCheck, result_cause

LOG_LINES = 150
_ACTIONS_TIMESTAMP = re.compile(r"^\ufeff?\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ?")
FAILING_TESTS = 10
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# gh run view --log-failed prints job<TAB>step<TAB> before each stamped line.
_JOB_STEP = re.compile(r"^[^\t]*\t[^\t]*\t(?=\ufeff?\d{4}-\d{2}-\d{2}T)")
_PYTEST_SUMMARY = re.compile(
    r"^(?:FAILED|ERROR) ([\w./-]+\.py(?:::[^\s\[]+)*(?:\[[^\]]*\])?)(?: - .*)?\s*$"
)
# Not "The operation was canceled.": a person or a concurrency group cancels too.
INFRA_SIGNALS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), label)
    for pattern, label in (
        (r"not acquired by Runner", "runner not acquired"),
        (r"spending limit", "billing or spending limit"),
        (r"account payments have failed", "billing or spending limit"),
        (r"billing issue|locked due to a billing", "billing or spending limit"),
        (r"lost communication with the server", "runner lost"),
        (r"received a shutdown signal", "runner lost"),
        (r"no hosted runner", "hosted runner unavailable"),
        (r"hosted runner encountered an error", "hosted runner unavailable"),
        (r"internal error when running your job", "hosted runner unavailable"),
    )
)


@dataclass(frozen=True)
class CIJobLog:
    check: str
    run_id: str | None
    run_url: str | None
    log_tail: str | None
    cut: str | None
    cause: Cause
    signal: str | None


@dataclass(frozen=True)
class CIEvidence:
    failed_checks: tuple[str, ...]
    runs: tuple[tuple[str, str], ...]
    jobs: tuple[CIJobLog, ...]
    cause: Cause


def classify_failure(conclusion: str | None, texts: Iterable[str]) -> tuple[Cause, str | None]:
    signal = _infra_signal(texts)
    if signal is not None:
        return "infra", signal
    return ("code" if conclusion == "failure" else "unknown"), None


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
        cause, signal = check.cause, check.signal
        logged = _untested_log_signal(text) if text is not None else None
        if logged is not None:
            cause, signal = "infra", logged
        jobs.append(CIJobLog(check.name, check.run_id, check.run_url, tail, cut, cause, signal))
    return CIEvidence(
        failed_checks=tuple(check.name for check in failed),
        runs=tuple(runs.items()),
        jobs=tuple(jobs),
        cause=result_cause(job.cause for job in jobs),
    )


def _untested_log_signal(text: str) -> str | None:
    return None if failing_tests(text) else _infra_signal((text,))


def _infra_signal(texts: Iterable[str]) -> str | None:
    for text in texts:
        for pattern, label in INFRA_SIGNALS:
            if pattern.search(text):
                return label
    return None


def failing_tests(log_text: str) -> list[str]:
    """The test ids on pytest's FAILED and ERROR summary lines, first FAILING_TESTS distinct."""
    found: dict[str, None] = {}
    for line in log_text.splitlines():
        entry = _ACTIONS_TIMESTAMP.sub("", _JOB_STEP.sub("", _ANSI.sub("", line)))
        match = _PYTEST_SUMMARY.match(entry)
        if match is not None:
            found.setdefault(match.group(1))
            if len(found) == FAILING_TESTS:
                break
    return list(found)


def _tail(text: str, limit: int) -> tuple[str, str | None]:
    from lib.dashboard import redact, redact_key_blocks
    from lib.dispatch.errors_text import _redact

    unstamped = [_ACTIONS_TIMESTAMP.sub("", line) for line in text.rstrip().splitlines()]
    blocks = _scrub_credential_blocks(redact_key_blocks(unstamped))
    lines = _redact(redact("\n".join(blocks))).splitlines()
    if len(lines) <= limit:
        return "\n".join(lines), None
    cut = f"[cut {len(lines) - limit} earlier lines]"
    return "\n".join([cut, *lines[len(lines) - limit :]]), cut


def _scrub_credential_blocks(lines: list[str]) -> list[str]:
    from lib.dispatch.errors_text import _CREDENTIAL_WORD, scrub_detail

    result: list[str] = []
    at = 0
    while at < len(lines):
        line = lines[at]
        indent = _indent(line)
        end = at + 1
        while end < len(lines) and (not lines[end].strip() or _indent(lines[end]) > indent):
            end += 1
        while end > at + 1 and not lines[end - 1].strip():
            end -= 1
        if end > at + 1 and _CREDENTIAL_WORD.search(line):
            result.append(line[:indent] + scrub_detail("\n".join(lines[at:end])))
            at = end
        else:
            result.append(line)
            at += 1
    return result


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())
