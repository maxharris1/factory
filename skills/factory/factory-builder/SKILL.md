---
name: factory-builder
description: Route a factory plan, build or fix job through pstack. Names the exact pstack files to read and how the job's handoffs, branch and gates map onto pstack's steps. Use when an orch brief starts a builder job.
---

# Factory builder

Read `<pstack>/skills/poteto-mode/SKILL.md` in full, then the playbook below, at the absolute paths the brief gives. `<pstack>` is the folder that holds pstack's `skills/` and `docs/`. It is the deployed factory checkout's `skills/pstack`, never the job's worktree. Then read your harness adapter. Claude reads `<pstack>/docs/harness/claude-code.md`. Codex reads `skills/factory/harness/codex.md`.

This skill routes to pstack and never replaces it. Where it differs from pstack, the table at the bottom names the pstack line and the reason. The brief wins over this skill where the two differ.

## Playbook

Always `<pstack>/skills/figure-it-out/SKILL.md`, for every plan, build and fix job (Max, "Careful playbook always"). Fix and CI-fix jobs take the existing plan and acceptance criteria as Phase A and run at the lowest rigor. Borrow the matching steps of `<pstack>/skills/poteto-mode/playbooks/feature.md` inside its phases.

## Human touchpoints

The job has no live operator. "The operator", `AskQuestion` and the Always-pause list all become a `questions` handoff on the PR, then stop. Max's gates are in the brief. Apply poteto-mode's classification first, and ask only for a product call no run can settle.

<!-- orch:v1 kind=questions from=claude-bot role=builder -->

Use your own bot in `from=`. Every other handoff header comes from the brief, with its placeholders filled.

## Helpers

Delegate code per `feature.md` step 4. Helpers run on the job's own harness, never Grok through Cursor, because Cursor jobs are off. Take model lines from the brief. Helpers never post to GitHub, never push and never open a PR. They return a diff or a commit. Only the main job pushes the ticket branch.

## Commits, PR and branch

Follow `<pstack>/skills/poteto-mode/playbooks/opening-a-pr.md` as written, with these changes:

- The PR already exists. Edit it with `gh pr edit`. Never create a second one.
- Title per the table below. Use pstack's body sections and keep the `Fixes #<issue>` line.
- Rebase into small, ordered commits any time on the ticket branch, and push with `--force-with-lease` only. Never rewrite the base. Today's brief still says no force-push, and the brief wins until T09 changes it.
- Skip only a step that needs a missing Cursor feature, with the adapter's own line (`Skipped in Claude Code: <step>. <reason>.` or `Skipped in Codex: <step>. <reason>.`). Cursor jobs stay off.

## Before ready

1. `<pstack>/skills/principle-prove-it-works/SKILL.md`. Run the real check and paste its output in the handoff.
2. `deslop`: read `skills/cursor-team-kit/deslop/SKILL.md`, beside `<pstack>` in the deployed factory checkout, and follow it over the diff.
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
| `<pstack>/skills/poteto-mode/playbooks/opening-a-pr.md`, "Conventional Commits `type(scope): subject`" | Title is `#<issue>: type(scope): subject`, for example `#115: fix(titles): …`. Edit the feeder's PR. | Max's rule, factory #115. The `#<issue>: ` prefix goes before the Conventional Commits title and does not replace it. The feeder opened the PR. |
| `<pstack>/skills/poteto-mode/playbooks/opening-a-pr.md`, "Open every PR ready, never as a draft." | Leave the PR draft and never mark it ready yourself. The poller marks it ready on APPROVE. | pstack opens a PR when the work is done. The feeder opens it before any work exists (`lib/feeder.py:576`). On Aut0, marking ready is the handoff to people. |
| `<pstack>/skills/poteto-mode/SKILL.md`, "Before commit → the `deslop` skill" | Read `skills/cursor-team-kit/deslop/SKILL.md` and follow it | pstack names `deslop` but does not ship it. The factory vendors it from cursor-team-kit (MIT), hash-pinned in `skills/skills.lock`. |
| `<pstack>/docs/harness/claude-code.md`, "`pstack:poteto-agent`, `pstack:comment-sicko`" | Claude spawns `poteto-agent` and `comment-sicko`, unprefixed | `orch install-skills` links pstack's agents into `~/.claude/agents/` instead of loading pstack with `--plugin-dir`, so the names carry no plugin prefix (`docs/decisions/178-plugin-dir-and-deslop.md`). |
| `<pstack>/skills/figure-it-out/SKILL.md`, "commit the trail so the reviewer can read it in the PR" | Commit the trail to `docs/decisions/<issue>-trail.tsv` | One file per ticket, so two PRs never edit the same lines (`docs/decisions/README.md`). |
