"""The review job: the one cross-family reviewer's verdict."""

from __future__ import annotations

from lib.followups import followups_active


def task_lines(facts: dict) -> list[str]:
    base = facts["base"] or "<base branch>"
    head = facts["head"] or "the PR head"
    verify = facts["verify"]
    later_round = (facts["round_number"] or 1) > 1
    followup = followups_active(facts["followup_from"], facts["round_number"])
    steps = [
        "Requirements first. Before the diff, list the ticket's acceptance criteria by `ac-<n>` and mark each "
        "`met`, `unmet`, `unverified` or `unsatisfiable`, with its evidence (the file:line, test or run that "
        "shows it). `unsatisfiable` means no change to this PR can meet it, for example a criterion about "
        "other PRs' state.",
        (
            "Read the whole diff. CI may still be running on that head. Read its result if it has finished. "
            "Do not wait for it, and do not block or REVISE on its status alone. The poller ships only on green CI."
            if facts["start_before_ci"]
            else "Read the whole diff and the CI result on that head."
        )
        + " A merge conflict with the base is `mergeable: no` in the verdict, not a finding.",
        *(
            [
                "Close the earlier rounds first: for each earlier finding by its `f-<n>` id, and each earlier unmet "
                "`ac-<n>`, say whether this head closes it. List each finding it closes as a `[closed]` item in the "
                "form below. Then review the delta from the last reviewed head "
                "(`git range-diff` when the branch was rebased). A new finding in code the delta did not change "
                "is `[minor]` unless it fails a criterion."
            ]
            if later_round
            else []
        ),
        f"Run the verify skill `{verify}` (UI and database checks), plus interrogate and blast-radius. "
        "A verify result you cannot read (unknown) is `verdict=BLOCKED`."
        if verify and verify != "none"
        else "factory.md names no verify skill (`none`): run interrogate and blast-radius, "
        "and say which checks did not run.",
        "You may run code in a throwaway copy. Mark a finding `[blocking]` only when you have run it "
        "and seen it fail, and give the repro.",
        "Give each finding an id `f-<n>` and name the rule it breaks. Reuse an earlier round's id for the same "
        "finding and number new ones after the highest earlier id. The same problem means the same broken rule, "
        "not the same line."
        + (
            " When a blocker breaks the same rule as an earlier round's blocker, set `repeat=<id>` in the header "
            "with that earlier id, ask Max what to do, and stop."
            if later_round and not followup
            else ""
        ),
        "When a criterion is `unsatisfiable`, set `unsat=<id>` in the header, for example `unsat=ac-3`, "
        "and count it as unmet for `ac`.",
        *(
            [
                "Mark a finding `[follow-up]` instead of `[blocking]` when it breaks a rule an earlier round "
                "already raised (a variant of it), sits in code this PR did not change in the last round, or is "
                "an edge case outside the acceptance criteria. Each finding names the rule it breaks."
            ]
            if followup
            else []
        ),
        "Pick `ac` and the verdict by the definitions in the Review rules below. Leave `repeat=` and `unsat=` "
        "out of the header when they do not apply. Post the verdict handoff below on the head you reviewed. "
        "Do not merge. Do not push to the PR.",
    ]
    return [
        f"Review the PR at head {head} against `{base}`. You are the one cross-family reviewer. "
        "Follow the router in Skills above.",
        *(f"{number}. {step}" for number, step in enumerate(steps, 1)),
    ]


def handoff(bot: str, facts: dict) -> tuple[str, str]:
    round_text = facts["round"] or (f"<n>/{facts['cap']}" if facts["cap"] else "<n>/<cap>")
    head = facts["head"] or "<reviewed-head-sha>"
    risk = facts["risk"] or "<low|high>"
    header = (
        f"<!-- orch:v1 kind=verdict from={bot} role=reviewer round={round_text} "
        f"head={head} ac=<met|unmet> verdict=<APPROVE|REVISE|BLOCKED> risk={risk} "
        "repeat=<id> unsat=<id> -->"
    )
    tags = (
        "`[blocking]`, `[follow-up]` or `[minor]`"
        if followups_active(facts["followup_from"], facts["round_number"])
        else "`[blocking]` or `[minor]`"
    )
    closed = (
        " Close an earlier finding with an item in this form: `1. [closed] f-<n>. <what shows it closed>.`"
        if (facts["round_number"] or 1) > 1
        else ""
    )
    example = (
        "**Review round N of CAP: VERDICT**, then each criterion by `ac-<n>` with its state and evidence, "
        "then the findings as a numbered list, one item each, in exactly this form: "
        f"`1. [blocking] f-<n>. Rule: <the rule it breaks>. <file:line>. <repro>.` The tag is one of {tags} "
        "and comes first, before the id. The dispatcher reads the tag, the id and the `Rule:` sentence from "
        f"that form to carry the finding into later rounds.{closed}"
    )
    return header, example
