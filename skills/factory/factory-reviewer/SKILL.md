---
name: factory-reviewer
description: Route a factory review job through pstack. Names the exact pstack files to run, the rules for helpers, later rounds, repeat blockers and BLOCKED, and how the verdict maps onto pstack's steps. Use when an orch brief starts a reviewer job.
---

# Factory reviewer

Read `<pstack>/skills/poteto-mode/SKILL.md` in full, then the steps below, at the absolute paths the brief gives. `<pstack>` is the folder that holds pstack's `skills/` and `docs/`. It is the deployed factory checkout's `skills/pstack`, never the job's worktree. Then read your harness adapter. Codex reads `skills/factory/harness/codex.md`. Claude reads `<pstack>/docs/harness/claude-code.md`.

This skill routes to pstack and never replaces it. Where it differs from pstack, the table at the bottom names the pstack line and the reason. The brief wins over this skill where the two differ.

## Steps

1. Read the criteria first. Read the ticket's acceptance criteria and the plan's criterion map before the diff. They are the intent.
2. Run `<pstack>/skills/interrogate/SKILL.md` over the PR diff at the head the brief names.
3. Run `<pstack>/skills/blast-radius/SKILL.md` on the same diff.
4. Run the verify skill when the brief names one. When it says `none`, say so.
5. Write the verdict. Block only on a failure you ran and showed. Give each blocker an id, `ac-<n>` for an unmet criterion or `f-<n>` for a finding, and reuse the ids of earlier rounds.

## Helpers

Helpers run on your own model and do research only. They read code, and they may run repros in a throwaway copy. They never edit the PR branch, push, or post. You review what they return and you alone write the verdict.

## Round 2 and later

Confirm that each earlier blocker is closed, by its id. Review the delta from the last reviewed head. A new finding in unchanged code is minor unless it fails a listed criterion. T15 will spell this rule out. Until then, follow these lines.

## Same problem twice

The same problem means the same acceptance criterion or the same finding as an earlier round's blocker. Do not loop. Write `verdict=REVISE` with the brief's other header keys, make `repeat=<id>` the first body line, ask Max what to do, and stop. `repeat` is not a header key, so it stays in the body. Example, with a made-up head:

```
<!-- orch:v1 kind=verdict from=codex-bot role=reviewer round=2/3 head=3f9c2ab ac=unmet verdict=REVISE risk=high -->
repeat=ac-2
```

## BLOCKED

Use `verdict=BLOCKED` only when you could not review at all, such as a missing head, an unreadable diff, or a check you could not run. Say what you need, tell Max, and stop. A failed criterion is REVISE, not BLOCKED.

## Where the factory differs from pstack

| pstack says | factory job does | why |
| --- | --- | --- |
| `<pstack>/skills/poteto-mode/SKILL.md`, "Contested design → the **interrogate** skill (multi-model adversarial) before shipping." | Run `interrogate` and `blast-radius` on every review | The review is the factory's only gate before ship. |
| `<pstack>/skills/interrogate/SKILL.md`, "Read the `interrogate reviewers` line in place of `arena runners`." | Adversaries run on your own model | Max, "Reviewer helpers allowed, same model, research only". Builder and reviewer are already different families. |
| `<pstack>/skills/blast-radius/SKILL.md`, "Ask more than one model the same question and merge the answers." | Ask helpers on your own model only | The same rule from Max. |
| `<pstack>/skills/interrogate/SKILL.md`, "Spawn each adversary as `generalPurpose`." | Helpers write only in a throwaway copy, never the branch | Research only. One writer per branch. |
| `<pstack>/skills/interrogate/SKILL.md`, "Ask the user when the intent is unclear." | Read the criteria first. If the intent is still unclear, return BLOCKED with what you need. | No live user. Max, "Blocked means couldn't review, stop and tell me". |
| `<pstack>/skills/interrogate/SKILL.md`, "Otherwise it is the branch diff from its merge base with the base branch, through the working tree." | From round 2, review the delta and confirm earlier blockers | Rounds converge. T15 will spell this out. |
| `<pstack>/skills/poteto-mode/SKILL.md`, "You own every subagent's work." | You alone write the verdict | Kept as written. Helpers never post. |
| `<pstack>/skills/poteto-mode/SKILL.md`, "The Principles section below grounds every trigger." | Apply three triggers: restate the intent, prove it with a run, unslop the verdict | A reviewer writes no code, so the build triggers do not apply. These three keep the verdict tied to the criteria and to a run. |
