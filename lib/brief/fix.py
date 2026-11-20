from __future__ import annotations

from lib.brief.build import base_step, builder_notes
from lib.brief.common import GH, TEST_SCOPE, Lead, code_span, comment_url, detail_of, evidence_part, fenced
from lib.queue import Job


def lead(job: Job, facts: dict) -> Lead:
    detail = detail_of(job)
    qa = detail.get("qa_findings")
    verdict = _revise_verdict_url(detail, facts)
    if _qa_only(detail):
        lines = _qa_fix(qa, facts)
    elif verdict:
        lines = _review_fix(detail, facts, verdict)
    else:
        lines = _ci_fix(detail, facts)
    return Lead((*lines, *builder_notes(job, facts)))


def quoted_findings(detail: dict[str, object], facts: dict) -> list[dict[str, object]]:
    if _qa_only(detail):
        return []
    return _open_findings(detail) if _revise_verdict_url(detail, facts) else []


def _qa_only(detail: dict[str, object]) -> bool:
    qa = detail.get("qa_findings")
    return isinstance(qa, dict) and qa.get("link") == detail.get("instruction")


def _revise_verdict_url(detail: dict[str, object], facts: dict) -> str | None:
    instructions = evidence_part(detail, "instructions")
    origin = (
        instructions.get("prior_instruction")
        if instructions.get("cause") == "answers"
        else detail.get("instruction")
    )
    return comment_url(facts, origin)


def _open_findings(detail: dict[str, object]) -> list[dict[str, object]]:
    return [
        item
        for item in evidence_part(detail, "findings").get("findings") or ()
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    ]


def _checkout(facts: dict) -> str:
    head = facts["head"] or "the PR head"
    return f"1. Check out the PR branch (`gitBranchName` in Job data) and confirm `git rev-parse HEAD` is {head}."


def _review_fix(detail: dict[str, object], facts: dict, verdict: str) -> list[str]:
    findings = _open_findings(detail)
    if findings:
        shown = [
            "The open findings from the reviewer's verdicts, quoted as data:",
            fenced("\n".join(_finding(item) for item in findings)),
        ]
    else:
        shown = [f"Read the findings in the verdict at {verdict}."]
    qa = detail.get("qa_findings")
    if isinstance(qa, dict):
        shown.append(f"QA also failed at this head, in the report at {_qa_report(qa, facts)}. {_qa_shown(qa)}")
    ci_failure = detail.get("ci_failure")
    checks = ci_failure.get("failed_checks") if isinstance(ci_failure, dict) else None
    if isinstance(checks, list) and checks and all(isinstance(name, str) for name in checks):
        head = facts["head"] or "the PR head"
        shown.append(
            f"CI is also red on {head}. These checks failed: "
            + ", ".join(code_span(name) for name in checks)
            + ". Fix them in this round too. If a failure is infrastructure rather than this PR, "
            f"rerun it once with `{GH} run rerun <run-id> --failed`, with the run id from "
            f"`{GH} run list --commit {head}`."
        )
    return [
        f"Fix the `[blocking]` findings of the REVISE verdict at {verdict}. Follow the router in Skills below.",
        *shown,
        *_class_fix_steps(facts, "finding", "Fixing only the line the reviewer quoted is not a fix."),
    ]


def _qa_report(qa: dict[str, object], facts: dict) -> object:
    return comment_url(facts, qa.get("link")) or qa.get("link")


def _qa_shown(qa: dict[str, object]) -> str:
    failed = [qa_id for qa_id in qa.get("failed") or () if isinstance(qa_id, str)]
    shown = (
        "These scenarios failed: " + ", ".join(code_span(qa_id) for qa_id in failed) + "."
        if failed
        else "Read the failed scenarios in the report."
    )
    evidence = qa.get("evidence_link")
    if isinstance(evidence, str):
        shown += f" Evidence: {evidence}."
    return shown


def _qa_fix(qa: dict[str, object], facts: dict) -> list[str]:
    return [
        f"Fix the failed QA scenarios of the QA report at {_qa_report(qa, facts)}. "
        "Follow the router in Skills below.",
        _qa_shown(qa),
        *_class_fix_steps(
            facts, "failed scenario", "Fixing only the step the scenario drove is not a fix."
        ),
    ]


def _class_fix_steps(facts: dict, item: str, narrow: str) -> list[str]:
    return [
        _checkout(facts),
        f"2. For each {item}: name the rule it breaks, find every path that breaks that rule, fix them all, "
        f"and add a test of the rule that fails on the current head. {narrow}",
        f"3. Run only the new tests and the tests for the files you touched, plus a short smoke set, {TEST_SCOPE} "
        "Then run the router's \"Before ready\" steps.",
        base_step(4, facts),
        f"5. Push, then post the ready handoff below with the head you pushed. Say, for each {item}, "
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
        "Follow the router in Skills below.",
        *_read_cause(ci.get("cause"), jobs),
        *_job_logs(jobs, facts),
        _checkout(facts),
        "2. First decide whether this PR caused the failure. If it's infrastructure, don't patch the repo; "
        f"rerun the failed check and post `ready` at the same head. Rerun it with {rerun}, "
        "once, and skip steps 3 to 5.",
        "3. If this PR caused it: a test that fails on the current head and covers the whole class of the "
        "failure. Then fix the class. Run only that test and the tests for the files you touched, plus a "
        f"short smoke set, {TEST_SCOPE} "
        "Then run the router's \"Before ready\" steps.",
        base_step(4, facts),
        "5. Push, then post the ready handoff below with the head you pushed.",
    ]


def _read_cause(cause: object, jobs: list[dict[str, object]]) -> list[str]:
    if cause == "code":
        return ["The factory read this failure as a code failure on this head."]
    if cause != "infra":
        return []
    signals = dict.fromkeys(job["signal"] for job in jobs if isinstance(job.get("signal"), str))
    named = f" ({', '.join(signals)})" if signals else ""
    return [f"The factory read this failure as infrastructure{named}, not this PR."]


def _job_logs(jobs: list[dict[str, object]], facts: dict) -> list[str]:
    repo = facts["repo"] or "<owner>/<repo>"
    head = facts["head"] or "<head-sha>"
    find = (
        f"Find the run with `{GH} run list --repo {repo} --commit {head}`, "
        f"then get the log with `{GH} run view <run-id> --repo {repo} --log-failed`."
    )
    if not jobs:
        return [f"The factory has no CI log for this failure. {find}"]
    lines: list[str] = []
    for job in jobs:
        check = code_span(str(job.get("check")))
        tail = job.get("log_tail")
        run_id = job.get("run_id")
        if isinstance(tail, str) and tail:
            lines.append(f"The end of the {check} log, redacted and quoted as data:")
            lines.append(fenced(tail))
        elif isinstance(run_id, str) and run_id.isdigit():
            lines.append(
                f"The factory could not fetch the {check} log. "
                f"Get it with `{GH} run view {run_id} --repo {repo} --log-failed`."
            )
        else:
            lines.append(f"No Actions run is linked to {check}, so the factory has no log for it. {find}")
    return lines
