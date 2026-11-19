"""The failed CI checks, their Actions runs and each failed job's log tail."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

from lib.dispatch.helpers import failed_checks
from lib.dispatch.types import CHECK_FAILED, BaseCheck, Cause, CIResult, FailedCheck, result_cause

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
INFRA_LINE_PREFIXES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), label)
    for pattern, label in (
        (r"The job was not acquired by Runner of type \S+", "runner not acquired"),
        (r"No \S+ runner picked up the \S+ job within", "runner pickup timed out"),
        (
            r"The job was not started because (?:recent account payments have failed"
            r"|your spending limit needs to be increased)",
            "billing or spending limit",
        ),
        (r"The job was not started because your account is locked", "billing or spending limit"),
        (r"The job was not started\b", "job not started"),
        (r"The (?:self-hosted |hosted )?runner\b.* lost communication with the server", "runner lost"),
        (r"The runner has received a shutdown signal", "runner lost"),
        (r"No hosted runner found", "hosted runner unavailable"),
        (r"The hosted runner encountered an error while running your job", "hosted runner unavailable"),
        (r"GitHub Actions has encountered an internal error when running your job", "hosted runner unavailable"),
    )
)
_LINE_PREFIX = re.compile(r"^##\[error\]\s*")


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
        jobs.append(
            CIJobLog(check.name, check.run_id, check.run_url, tail, cut, check.cause, check.signal)
        )
    return CIEvidence(
        failed_checks=tuple(check.name for check in failed),
        runs=tuple(runs.items()),
        jobs=tuple(jobs),
        cause=result_cause(job.cause for job in jobs),
    )


def classify_logged(
    result: CIResult,
    logs: Mapping[str, str | None],
    steps: Mapping[str, int | None] | None = None,
) -> CIResult:
    if result.status != "failure":
        return result
    checks = []
    for check in failed_checks(result):
        text = logs.get(check.job_id) if check.job_id is not None else None
        if text is not None:
            signal = None if failing_tests(text) else _infra_signal((text,))
        elif steps is not None and check.job_id is not None and steps.get(check.job_id) == 0:
            signal = "job not started"
        else:
            signal = None
        checks.append(check if signal is None else replace(check, cause="infra", signal=signal))
    return replace(result, failed=tuple(checks))


def _infra_signal(texts: Iterable[str]) -> str | None:
    for text in texts:
        for line in text.splitlines():
            entry = _ACTIONS_TIMESTAMP.sub("", _JOB_STEP.sub("", _ANSI.sub("", line))).strip()
            entry = _LINE_PREFIX.sub("", entry)
            for pattern, label in INFRA_LINE_PREFIXES:
                if pattern.match(entry):
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


def base_red(
    failed: Sequence[FailedCheck],
    pr_logs: Mapping[str, str | None],
    base: Mapping[str, BaseCheck],
    base_logs: Mapping[str, str | None],
) -> bool:
    """True when the base branch explains every failed PR check (#955).

    With failing tests named on both sides, a check is explained only when
    each of its tests also fails somewhere on the base: a shard can move a
    test, and a PR that broke one test while the base flakes on another is
    still charged. Without them, the same-named base check must have failed.
    """
    red = [check for check in base.values() if check.conclusion in CHECK_FAILED]
    base_tests = {
        test for check in red for test in failing_tests(base_logs.get(check.job_id) or "")
    }

    def explained(check: FailedCheck) -> bool:
        text = pr_logs.get(check.job_id) if check.job_id is not None else None
        tests = failing_tests(text or "")
        if tests and base_tests:
            return base_tests.issuperset(tests)
        same = base.get(check.name)
        return same is not None and same.conclusion in CHECK_FAILED

    return bool(failed) and all(map(explained, failed))


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
