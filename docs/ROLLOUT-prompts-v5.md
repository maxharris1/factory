# Prompts v5 rollout: the watch

This runbook covers the watch after the prompt refactor (T00 to T27). It does not describe a launch. There is nothing left to switch on (D34).

## What is already live

- Each brief ticket went live for every project when its PR merged and deployed. No per-project switch or staged rollout exists.
- T09 (#180, merged 2026-11-14) is the day job behavior changed. From that deploy, every builder and reviewer brief names the router and the pstack files by absolute path.
- There is no version stamp. No `prompt_version`, no deployed-commit field, and no stamp in `~/.claude/rules/pstack-models.md`. To learn which prompt a job ran, read the brief on its PR and the merged history of `main`.

## The watch

`bin/skill-probe` and `orch doctor` prove only that the skills load. They do not prove that a job follows them end to end. So Factory watches real work.

1. **First cycle per project.** For each of factory, Cleffy, Tracy and Aut0, watch the first ticket that goes through a full plan, build, review and ship cycle after T09 deployed. Check each handoff on the PR:
   - The builder `ready` and the reviewer verdict each end with a `Skills read:` line.
   - The builder leaves the PR draft. Only the poller marks it ready.
   - No job posts more than one handoff, and no job messages another bot.
   - The ship lands. Aut0 counts as a success at Ready for Review.
2. **T25 metrics against O4's baseline.** Run the T25 report (#209) on a backed-up copy of `orch.db`, once one day after T09 deployed and once three days after:

   ```sh
   bin/orch eval-report --db ~/.local/share/factory/backup/orch.db --baseline o4-orch.db
   ```

   Both databases open read-only. The baseline is O4's database snapshot, not a commit. Compare each metric with the baseline beside it. Look hardest at first-round REVISE rate, rounds per PR, PRs capped, `ac=unmet` with APPROVE, duplicate handoffs, and helper count per job. Helper count has no cap (D25), so this watch is its only check.

A first cycle that goes wrong, or a metric that is clearly worse than the baseline at either check, starts a rollback.

## Rollback

Roll back by reverting the PR of the ticket that caused the problem, through a normal PR to `main`. There is no switch to flip and no version to pin back.

One revert has an extra step (SF-11). Reverting #178 (T07c) must also remove the rules file it installs. On each device and for each bot, run `orch install-skills --remove-claude-extras` from the deployed checkout **before** the revert deploys, because the reverted code has no such flag. `docs/decisions/178-plugin-dir-and-deslop.md` gives the manual steps for when the revert has already deployed.

## Not a gate

The approval switch (T02b, T02c) is a separate switch. This watch does not wait on it, and it does not gate the switch.

## Risk: `main` is unprotected

`main` on `maxharris1/factory` has no branch protection (D24). CI is the only gate before a merge, and nothing stops a direct push or a force-push to `main`. A revert therefore lands as fast as any other PR. A bad merge also lands with nothing in front of it except CI and the reviewer's verdict.
