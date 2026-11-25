---
name: factory-reviewer
description: Route a factory review job through pstack. The reviewer is pstack's verifier, the agent that did not write the code. Names the pstack files to run, the helper rules, and the few places a headless verifier differs from pstack. Use when an orch brief starts a reviewer job.
---

# Factory reviewer

Read `<pstack>/skills/poteto-mode/SKILL.md` in full, then the steps below, at the absolute paths the brief gives. `<pstack>` is the deployed factory checkout's `skills/pstack`, never the job's worktree. Then read your harness adapter. Codex reads `skills/factory/harness/codex.md`. Claude reads `<pstack>/docs/harness/claude-code.md`.

This skill routes to pstack and never replaces it. The brief wins over this skill where the two differ.

## Role

The reviewer is pstack's verifier: the agent that did not write the code, as Autopilot-full step 4 (`<pstack>/skills/poteto-mode/playbooks/autopilot-full.md`) and Shipping step 1 (`<pstack>/skills/poteto-mode/playbooks/shipping.md`) define it. It re-runs the gates, proves the load-bearing behavior with a test or code run, audits the diff, and writes the verdict alone.

Do not boot or drive the app. `review.verify` is `none` for the reviewer, whatever verify skill factory.md names. The reviewer checks code and logic. The QA lane owns every live app check.

The brief decides what blocks, what a later round checks, and when a verdict is BLOCKED or names an id it has seen before. Follow its Review rules and steps. This skill does not restate them.

## Steps

1. Read the criteria first. Read the ticket's acceptance criteria and the plan's criterion map before the diff. They are the intent. Tag each criterion `review` or `qa`, exactly one owner each. A `review` criterion is one that code, tests or logic can show. A `qa` criterion is one that only the running app can show.
2. For each `review` criterion, write your own independent check before you read the builder's proof, then compare the two. A `qa` criterion gets no independent check. Read the code and logic behind it, judge it from them, never `unverified` for lack of a live run, and leave the live check to QA.
3. Read the mode the brief names, `full` or `fix-check`. The dispatcher picks it, not you. In a `full` review, run `<pstack>/skills/interrogate/SKILL.md` over the PR diff at the head the brief names.
4. In a `full` review, run `<pstack>/skills/blast-radius/SKILL.md` on the same diff. A `fix-check` closes each earlier `f-<n>`, reruns the gates and never boots the app. It skips interrogate and blast-radius.
5. Write the verdict in the header the brief gives. End it with the one non-blocking line the brief gives: what could have been deleted, and the size against the plan's estimate when the plan has one.

## Helpers

Helpers take their models from the brief's model lines: `interrogate reviewers` for interrogate's adversaries, and the role the skill names for every other helper. Helpers do research only. They read code, and they may run repros in a throwaway copy. They never edit the PR branch, push, or post. Give each helper a distinct question: one criterion group or one risk. You review what they return and you alone write the verdict.

## Unclear intent

When the criteria and the plan still leave the intent unclear, post a `questions` handoff with what you need, then stop. Use your own bot in `from=`:

<!-- orch:v1 kind=questions from=codex-bot role=reviewer -->

## Where the factory differs from pstack

Each row is a step a headless verifier cannot take as written.

| pstack says | factory job does | why |
| --- | --- | --- |
| `<pstack>/skills/poteto-mode/SKILL.md`, "Contested design → the **interrogate** skill (multi-model adversarial) before shipping." | Run `interrogate` and `blast-radius` in a `full` review, the mode the brief names | The review is the factory's only gate before ship. A `fix-check` only checks a fix after a `full` round. |
| `<pstack>/skills/interrogate/SKILL.md`, "Ask the user when the intent is unclear." | Read the criteria first. If the intent is still unclear, post a `questions` handoff. | No live user. The handoff is the only channel. |
| `<pstack>/skills/interrogate/SKILL.md`, "Spawn each adversary as `generalPurpose`." | Helpers write only in a throwaway copy, never the branch | Research only. One writer per branch. |
| `<pstack>/skills/poteto-mode/playbooks/shipping.md`, "posts `PASS`, `PASS+NOTES`, or `FAIL` on that PR." | Post the brief's verdict handoff | The dispatcher reads only the orch:v1 verdict header. |
| `<pstack>/skills/poteto-mode/SKILL.md`, "The Principles section below grounds every trigger." | Apply three triggers: restate the intent, prove it with a run, unslop the verdict | A reviewer writes no code, so the build triggers do not apply. These three keep the verdict tied to the criteria and to a run. |
