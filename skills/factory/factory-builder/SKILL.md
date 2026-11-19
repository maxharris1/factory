---
name: factory-builder
description: Route a factory plan, build or fix job through pstack. The builder is the owner of one PR. Names the pstack files to read, the owner's duties, and the few places a headless job differs from pstack. Use when an orch brief starts a builder job.
---

# Factory builder

Read `<pstack>/skills/poteto-mode/SKILL.md` in full, then the playbook it picks, at the absolute paths the brief gives. `<pstack>` is the deployed factory checkout's `skills/pstack`, and `<kit>` is its `skills/cursor-team-kit`, never the job's worktree. Then read your harness adapter. Claude reads `<pstack>/docs/harness/claude-code.md`. Codex reads `skills/factory/harness/codex.md`.

This skill routes to pstack and never replaces it. The brief wins over this skill where the two differ.

## Role

The builder is the owner of one PR, as `<pstack>/skills/poteto-mode/playbooks/autopilot-full.md` step 2 defines it. The poller, not the owner, babysits CI and merges.

## Playbook

The brief names one playbook, picked by the ticket's `Type:` line. No line means Feature. Feature (`<pstack>/skills/poteto-mode/playbooks/feature.md`) is for new or changed behavior. Bug fix (`<pstack>/skills/poteto-mode/playbooks/bug-fix.md`) is for a defect. Refactoring (`<pstack>/skills/poteto-mode/playbooks/refactoring.md`) is for a change of structure. Perf issue (`<pstack>/skills/poteto-mode/playbooks/perf-issue.md`) is for a measured slowness. `<pstack>/skills/figure-it-out/SKILL.md` is for large or cross-cutting work. A plan job also runs `<pstack>/skills/how/SKILL.md` and `<pstack>/skills/architect/SKILL.md`, stops after the playbook's design steps and posts the plan. A build or fix job writes each test first with `<pstack>/skills/tdd/SKILL.md`. A fix or CI-fix job keeps the plan's criteria and runs the same playbook from its verify step.

## Owner duties

1. Delegate the code. Delegate code-writing to a helper as Feature step 4 and Bug fix step 3 say (Refactoring step 5 for mechanical edits). Helpers take their models from the brief's model lines. A helper returns a diff or a commit. It never posts, pushes or opens a PR.
2. Reach code-ready before `ready`, as Autopilot-full step 2 says, in this order:
   - Strip slop with `<kit>/deslop/SKILL.md` over the diff.
   - Run the comments pass with `<pstack>/skills/no-comments/SKILL.md`. It spawns `<pstack>/agents/comment-sicko.md`.
   - Rebase on the current base and push the ticket branch with `--force-with-lease` only. The ticket branch has one writer, so it is not a shared branch. Never rewrite the base.
   - Run the repo's checks for the touched paths on the committed head.
   - Prove the change with `<pstack>/skills/principle-prove-it-works/SKILL.md`, and paste the run in the handoff.
3. Post `ready` with the head you pushed. The handoff lists every SKILL.md you read, by path, under "Skills read:".

## Factory facts

- The PR title is `#<issue>: type(scope): subject`, a Conventional Commits title after the issue prefix. Edit the feeder's PR with `gh pr edit`, run as the brief says. Keep the `Fixes #<issue>` line in the body.
- Claude spawns `poteto-agent` and `comment-sicko` unprefixed, not as the adapter's `pstack:poteto-agent`, because `orch install-skills` links pstack's agents into `~/.claude/agents/`.
- figure-it-out's decision trail goes in `docs/decisions/<issue>-trail.tsv`, one file per ticket.

Unclear intent is a `questions` handoff. Classify the fork per poteto-mode first, and ask only for a product call no run can settle. Use your own bot in `from=`. Every other handoff header comes from the brief, with its placeholders filled.

<!-- orch:v1 kind=questions from=claude-bot role=builder -->

## Where the factory differs from pstack

Each row is a step a headless job cannot take.

| pstack says | factory job does | why |
| --- | --- | --- |
| `<pstack>/skills/poteto-mode/SKILL.md`, "Gates the operator named and the Always-pause list in Autonomy still need the operator." | Post a `questions` handoff and stop | No live operator. The handoff is the only channel. |
| `<pstack>/skills/poteto-mode/playbooks/opening-a-pr.md`, "Open every PR ready, never as a draft." | Edit the feeder's draft PR and never mark it ready | The feeder opens the PR before any work exists. The poller marks it ready on APPROVE. |
| `<pstack>/skills/poteto-mode/playbooks/opening-a-pr.md`, "Work from a git worktree off main." | Work in the job's worktree on the ticket branch. A helper that edits files gets its own worktree. | `orch job run` already made the checkout, and the branch has one writer. |
| `<pstack>/skills/poteto-mode/playbooks/autopilot-full.md`, "babysits to green per `playbooks/babysit.md`, and merges once steps 4 and 5 clear it." | Stop at the handoff. Never babysit, merge or wait on CI. | The poller watches CI, queues fix jobs and merges. |
| `<pstack>/skills/figure-it-out/SKILL.md`, "but a multi-hour run earns one checkpoint." | The plan handoff is the checkpoint. A build job goes on without another. | Max reads the plan before `go`, and the brief's wall-clock budget bounds the run. |
