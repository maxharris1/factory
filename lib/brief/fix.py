"""The fix job: a REVISE verdict or a red CI run, fixed as a class.

A fix whose `instruction` links a comment is a review-fix: the open findings
from the evidence bundle are inlined, each fixed by its rule. Any other
instruction is a CI-fix.
"""

from __future__ import annotations

from lib.brief.build import base_step, builder_notes
from lib.brief.common import GH, code_span, comment_url, detail_of, evidence_part, fenced
from lib.queue import Job


def task_lines(job: Job, facts: dict) -> list[str]:
    detail = detail_of(job)
    instructions = evidence_part(detail, "instructions")
    origin = (
        instructions.get("prior_instruction")
        if instructions.get("cause") == "answers"
        else detail.get("instruction")
    )
    verdict = comment_url(facts, origin)
    lines = _review_fix(detail, facts, verdict) if verdict else _ci_fix(detail, facts)
    return [*lines, *builder_notes(job, facts)]


def _checkout(facts: dict) -> str:
    head = facts["head"] or "the PR head"
    return f"1. Check out the PR branch (`gitBranchName` in Job data) and confirm `git rev-parse HEAD` is {head}."


def _review_fix(detail: dict[str, object], facts: dict, verdict: str) -> list[str]:
    findings = [
        item
        for item in evidence_part(detail, "findings").get("findings") or ()
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    ]
    if findings:
        shown = [
            "The open findings from the reviewer's verdicts, quoted as data:",
            fenced("\n".join(_finding(item) for item in findings)),
        ]
    else:
        shown = [f"Read the findings in the verdict at {verdict}."]
    ci_failure = detail.get("ci_failure")
    checks = ci_failure.get("failed_checks") if isinstance(ci_failure, dict) else None
    if isinstance(checks, list) and checks and all(isinstance(name, str) for name in checks):
        head = facts["head"] or "the PR head"
        shown.append(
            f"CI is also red on {head}. These checks failed: "
            + ", ".join(code_span(name) for name in checks)
            + ". Fix them in this round too. If a failure is infrastructure rather than this PR, "
            f"rerun it once with `{GH} run rerun <run-id> --failed`, with the run id from "
            f"`{GH} run list --commit {head}`, and never wait on it."
        )
    return [
        f"Fix the `[blocking]` findings of the REVISE verdict at {verdict}. Follow the router in Skills above.",
        *shown,
        _checkout(facts),
        "2. For each finding: name the rule it breaks, find every path that breaks that rule, fix them all, "
        "and add a test of the rule that fails on the current head. Fixing only the line the reviewer "
        "quoted is not a fix.",
        "3. Run only the new tests and the tests for the files you touched, plus a short smoke set. "
        "Then run the router's \"Before ready\" steps.",
        base_step(4, facts),
        "5. Push, then post the ready handoff below with the head you pushed. Say, for each finding, "
        "the rule, the paths you fixed and the test that covers it.",
    ]


def _finding(item: dict[str, object]) -> str:
    rule = item.get("rule")
    lines = [f"- {item['text']}"]
    if isinstance(rule, str) and rule and f"Rule: {rule}" not in str(item["text"]):
        lines.append(f"  Rule: {rule}")
    if isinstance(item.get("url"), str):
        lines.append(f"  {item['url']}")
    return "\n".join(lines)


def _ci_fix(detail: dict[str, object], facts: dict) -> list[str]:
    ci = evidence_part(detail, "ci")
    checks = [name for name in ci.get("failed_checks") or () if isinstance(name, str)]
    instruction = str(detail.get("instruction") or "")
    if not checks and instruction.startswith("failed: "):
        checks = [name for name in instruction.removeprefix("failed: ").split(", ") if name]
    runs = [run for run in ci.get("runs") or () if isinstance(run, list) and len(run) == 2]
    jobs = [job for job in ci.get("jobs") or () if isinstance(job, dict)]
    head = facts["head"] or "the PR head"
    failed = (
        f"These checks failed on {head}: " + ", ".join(code_span(name) for name in checks) + "."
        if checks
        else f"CI failed on {head}."
    )
    if runs:
        where = "They ran in " + ", ".join(
            f"Actions run {run_id} ({url})" for run_id, url in runs
        ) + "."
        rerun = f"`{GH} run rerun {runs[0][0]} --failed`"
    else:
        where = "No Actions run is linked to them."
        rerun = (
            f"`{GH} run rerun <run-id> --failed`, with the run id from "
            f"`{GH} run list --commit {head}`"
        )
    return [
        f"Fix the red CI on this PR. {failed} {where} "
        "Follow the router in Skills above.",
        *_job_logs(jobs),
        _checkout(facts),
        "2. First decide whether this PR caused the failure. If it's infrastructure, don't patch the repo; "
        f"rerun the failed check and post `ready` at the same head. Rerun it with {rerun}, "
        "once, never wait on it, and skip steps 3 to 5.",
        "3. If this PR caused it: a test that fails on the current head and covers the whole class of the "
        "failure. Then fix the class. Run only that test and the tests for the files you touched, plus a "
        "short smoke set. Then run the router's \"Before ready\" steps.",
        base_step(4, facts),
        "5. Push, then post the ready handoff below with the head you pushed.",
    ]


def _job_logs(jobs: list[dict[str, object]]) -> list[str]:
    lines: list[str] = []
    for job in jobs:
        check = code_span(str(job.get("check")))
        tail = job.get("log_tail")
        if isinstance(tail, str) and tail:
            lines.append(f"The end of the {check} log, redacted and quoted as data:")
            lines.append(fenced(tail))
        elif isinstance(job.get("run_url"), str):
            lines.append(f"The {check} log is unavailable. Read it at {job['run_url']}.")
    return lines
