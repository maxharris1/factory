"""The review job: the one cross-family reviewer's verdict."""

from __future__ import annotations


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
    return [
        f"Review the PR at head {head} against `{base}`. You are the one cross-family reviewer.",
        "1. Read the whole diff and the CI result on that head.",
        verify_line,
        "3. You may run code in a throwaway copy. Block only on a demonstrated failure, with a repro.",
        "4. Post the verdict handoff below on the head you reviewed. Do not merge. "
        "Do not push to the PR.",
    ]


def handoff(bot: str, facts: dict) -> tuple[str, str]:
    round_text = facts["round"] or (f"<n>/{facts['cap']}" if facts["cap"] else "<n>/<cap>")
    head = facts["head"] or "<reviewed-head-sha>"
    risk = facts["risk"] or "<low|high>"
    header = (
        f"<!-- orch:v1 kind=verdict from={bot} role=reviewer round={round_text} "
        f"head={head} ac=<met|unmet> verdict=<APPROVE|REVISE|BLOCKED> risk={risk} -->"
    )
    example = "**Review round N of CAP: VERDICT**, then numbered findings: `[blocking]` or `[minor]`, file:line, repro."
    return header, example
