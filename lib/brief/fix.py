"""The fix job: a REVISE verdict or a red CI run, fixed as a class.

A fix whose `instruction` links a comment is a review-fix: the open findings
from the evidence bundle are inlined, each fixed by its rule. Any other
instruction is the failed CI log, and the brief is a CI-fix.
"""

from __future__ import annotations

from lib.brief.build import base_step, builder_notes
from lib.brief.common import GH, comment_url, detail_of, evidence_part, fenced
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
    lines = _review_fix(detail, facts, verdict) if verdict else _ci_fix(facts)
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


def _ci_fix(facts: dict) -> list[str]:
    return [
        "Fix the red CI on this PR. The failed checks and log are in Job data (`instruction`, "
        "and `evidence.ci` when present). Follow the router in Skills above.",
        _checkout(facts),
        "2. First decide whether this PR caused the failure. If it's infrastructure, don't patch the repo; "
        f"rerun the failed check and post `ready` at the same head. Rerun it with `{GH} run rerun <run-id> --failed`, "
        "once, never wait on it, and skip steps 3 to 5.",
        "3. If this PR caused it: a test that fails on the current head and covers the whole class of the "
        "failure. Then fix the class. Run only that test and the tests for the files you touched, plus a "
        "short smoke set. Then run the router's \"Before ready\" steps.",
        base_step(4, facts),
        "5. Push, then post the ready handoff below with the head you pushed.",
    ]
