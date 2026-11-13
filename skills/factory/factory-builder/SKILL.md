---
name: factory-builder
description: Route a factory plan, build or fix job through pstack. Names the exact pstack files to read and how the job's handoffs, branch and gates map onto pstack's steps. Use when an orch brief starts a builder job.
---

# Factory builder

Read `<pstack>/skills/poteto-mode/SKILL.md` in full, then the playbook below, at the absolute paths the brief gives. `<pstack>` is the folder that holds pstack's `skills/` and `docs/`. Until the brief names it, it is this repo's `skills/pstack`. Then read your harness adapter. Claude reads `<pstack>/docs/harness/claude-code.md`. Codex reads `skills/factory/harness/codex.md`.

This skill routes to pstack and never replaces it. Where it differs from pstack, the table at the bottom names the pstack line and the reason. The brief wins over this skill where the two differ.

## Playbook

Always `<pstack>/skills/figure-it-out/SKILL.md`, for every plan, build and fix job (Max, "Careful playbook always"). Borrow the matching steps of `<pstack>/skills/poteto-mode/playbooks/feature.md` inside its phases.

## Human touchpoints

The job has no live operator. "The operator", `AskQuestion` and the Always-pause list all become a `questions` handoff on the PR, then stop. Max's gates are in the brief. Apply poteto-mode's classification first, and ask only for a product call no run can settle.

<!-- orch:v1 kind=questions from=claude-bot role=builder -->

Use your own bot in `from=`. Every other handoff header comes from the brief, with its placeholders filled.

## Helpers

Delegate code per `feature.md` step 4. Helpers run on the job's own harness, never Grok through Cursor, because Cursor jobs are off. Take model lines from the brief. Until the brief carries them (T10), Claude jobs and helpers use Opus 5.5 high, and Codex jobs use the current setting (Astra) with Sol helpers. Helpers never post to GitHub, never push and never open a PR. They return a diff or a commit. Only the main job pushes the ticket branch.

## Commits, PR and branch

Follow `<pstack>/skills/poteto-mode/playbooks/opening-a-pr.md` as written, with these changes:

- The PR already exists. Edit it with `gh pr edit`. Never create a second one.
- Keep the `#<issue>: ` title prefix (Max's rule, factory #115). Use pstack's body sections and keep the `Fixes #<issue>` line.
- Keep the PR ready (`gh pr ready`). The poller's draft step does nothing to a ready PR.
- Rebase into small, ordered commits any time on the ticket branch, and push with `--force-with-lease` only. Never rewrite the base. Today's brief still says no force-push, and the brief wins until T09 changes it.
- Skip only a step that needs a missing Cursor feature, with the adapter's own line (`Skipped in Claude Code: <step>. <reason>.` or `Skipped in Codex: <step>. <reason>.`). Cursor jobs stay off.

## Before ready

1. `<pstack>/skills/principle-prove-it-works/SKILL.md`. Run the real check and paste its output in the handoff.
2. `deslop`. Skipped until T07 vendors it. Say so in the handoff.
3. `<pstack>/skills/no-comments/SKILL.md`, which spawns `<pstack>/agents/comment-sicko.md` over the diff.

The handoff lists every SKILL.md you read, by path, under "Skills read:".

## Where the factory differs from pstack

| pstack says | factory job does | why |
| --- | --- | --- |
| `<pstack>/skills/poteto-mode/SKILL.md`, "Gates the operator named and the Always-pause list in Autonomy still need the operator." | Post a `questions` handoff and stop | No live operator. The handoff is the only channel. |
| `<pstack>/skills/poteto-mode/SKILL.md`, "Reversible work and external actions (team chat, ticket updates, kicking off evals) proceed without asking." | Post one handoff comment and message no other bot | The brief's rule. The poller reads one handoff per job. |
| `<pstack>/skills/poteto-mode/SKILL.md`, "force-push to shared branches" | Force-push the ticket branch with `--force-with-lease` when the brief allows it | The ticket branch has one writer. Max, "Rebase any time on the ticket branch". |
| `<pstack>/skills/poteto-mode/SKILL.md`, "Use **figure-it-out** whenever no bundled playbook fits." | Use `figure-it-out` even when Feature or Bug fix fits | Max, "Careful playbook always". |
| `<pstack>/skills/poteto-mode/playbooks/feature.md`, "using your configured feature model (default `grok-4.7-xhigh-fast`)" | Helpers use the job's harness and the brief's model lines | Cursor jobs are off. The model map is T10. |
| `<pstack>/skills/poteto-mode/playbooks/opening-a-pr.md`, "A subagent that opens a PR runs `/deslop` and `/no-comments`, and posts the URL." | Helpers never open a PR, post or push | One writer per branch. The main job owns the handoff. |
| `<pstack>/skills/poteto-mode/playbooks/opening-a-pr.md`, "Conventional Commits `type(scope): subject`" | Title starts `#<issue>: ` and edits the feeder's PR | Max's rule, factory #115. The feeder opened the PR. |
| `<pstack>/skills/poteto-mode/SKILL.md`, "Before commit → the `deslop` skill" | Name it and skip it | Not in the pin until T07. |
| `<pstack>/skills/figure-it-out/SKILL.md`, "commit the trail so the reviewer can read it in the PR" | Put the trail in the handoff and the PR body | No decisions folder until T04. |
