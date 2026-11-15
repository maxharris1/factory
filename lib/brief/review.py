"""The review job: the one cross-family reviewer's verdict."""

from __future__ import annotations

from lib.followups import followups_active


def task_lines(facts: dict) -> list[str]:
    base = facts["base"] or "<base branch>"
    head = facts["head"] or "the PR head"
    verify = facts["verify"]
    verify_line = (
        f"2. Run the verify skill `{verify}` (UI and database checks), plus interrogate and blast-radius."
        if verify and verify != "none"
        else "2. factory.md names no verify skill (`none`): run interrogate and blast-radius, "
        "and say which checks did not run."
    )
    read_line = (
        "1. Read the whole diff. CI may still be running on that head. Read its result if it has finished. "
        "Do not wait for it, and do not block or REVISE on its status alone. The poller ships only on green CI."
        if facts["start_before_ci"]
        else "1. Read the whole diff and the CI result on that head."
    )
    followup = followups_active(facts["followup_from"], facts["round_number"])
    followup_lines = (
        [
            "4. Mark a finding `[follow-up]` instead of `[blocking]` when it breaks a rule an earlier round "
            "already raised (a variant of it), sits in code this PR did not change in the last round, or is "
            "an edge case outside the acceptance criteria. Each finding names the rule it breaks."
        ]
        if followup
        else []
    )
    last = 5 if followup else 4
    return [
        f"Review the PR at head {head} against `{base}`. You are the one cross-family reviewer. "
        "Follow the router in Skills above.",
        read_line,
        verify_line,
        "3. You may run code in a throwaway copy. Mark a finding `[blocking]` only when you have run it "
        "and seen it fail, and give the repro.",
        *followup_lines,
        f"{last}. Pick `ac` and the verdict by the definitions in the Review rules below. "
        "Post the verdict handoff below on the head you reviewed. Do not merge. Do not push to the PR.",
    ]


def handoff(bot: str, facts: dict) -> tuple[str, str]:
    round_text = facts["round"] or (f"<n>/{facts['cap']}" if facts["cap"] else "<n>/<cap>")
    head = facts["head"] or "<reviewed-head-sha>"
    risk = facts["risk"] or "<low|high>"
    header = (
        f"<!-- orch:v1 kind=verdict from={bot} role=reviewer round={round_text} "
        f"head={head} ac=<met|unmet> verdict=<APPROVE|REVISE|BLOCKED> risk={risk} -->"
    )
    tags = (
        "`[blocking]`, `[follow-up]` or `[minor]`"
        if followups_active(facts["followup_from"], facts["round_number"])
        else "`[blocking]` or `[minor]`"
    )
    example = f"**Review round N of CAP: VERDICT**, then numbered findings: {tags}, file:line, repro."
    return header, example
