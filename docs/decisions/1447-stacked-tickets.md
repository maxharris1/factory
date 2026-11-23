# #1447: target branches and stacked tickets

Max's product calls and the factory's technical defaults for the `Target branch:` field and the stacked-ticket lifecycle in SPEC section 4.4. Each technical entry says what Max could ask for instead.

- **Decision:** A ticket can name its own non-default base branch on a `Target branch:` intake line, so a child ticket can be built on its parent PR's branch.
- **When:** 2026-11-23 3:44 PM ET, by Max
- **Source:** Factory chat, 2026-11-23; filed as https://github.com/maxharris1/factory/issues/1447

- **Decision:** Review never re-reviews a diff the child's branch alone did not change, so no tokens are spent on the parent's commits: review covers only the child's own diff, `git diff <base>...<head>`, and a parent push never queues a child review.
- **When:** 2026-11-23 3:17 PM ET, by Max
- **Source:** Factory chat, 2026-11-23; filed as https://github.com/maxharris1/factory/issues/1447

- **Decision:** A stacked or feature-branch base uses local checks (`ci.local_gate`). GitHub CI spend starts once the PR points at an established branch in `ci.branches`.
- **When:** 2026-11-23 3:59 PM ET, by Max
- **Source:** Factory chat, 2026-11-23; filed as https://github.com/maxharris1/factory/issues/1447

- **Decision:** The design is project-agnostic. Every per-project setting lives in each repo's `factory.md`.
- **When:** 2026-11-23 4:18 PM ET, by Max
- **Source:** Factory chat, 2026-11-23; filed as https://github.com/maxharris1/factory/issues/1447

- **Decision:** Go. The factory may retarget a stacked child, only to the branch its parent merged into and only after GitHub reports the parent merged. A `ship: handoff` stacked child is handed off only after that retarget and green GitHub CI on the new base. After the retarget rebase the approval carries with no review when the child's stable patch id is unchanged, CI is green and no file in its diff overlaps the parent's final delta (migrations and generated types always overlap). Otherwise one scoped `git range-diff <old-base>..<old-head> <new-base>..<new-head>` review runs over the child's own commits only, and it spends no round.
- **When:** 2026-11-23 4:21 PM ET, by Max
- **Source:** Factory chat, 2026-11-23; filed as https://github.com/maxharris1/factory/issues/1447

- **Decision:** When a retarget rebase pushes no new head, because the child's head after the rebase equals its head before (as when the parent landed by a fast-forward to the old base), the factory closes the PR and reopens it at once, so GitHub fires `pull_request: reopened` and CI runs on the new base. The factory records the close as its own, with the step and the head, so nothing reads it as an abandoned PR. An empty commit was rejected because no person wrote it and it moves the head the approval pins. `workflow_dispatch` was rejected because it needs a per-project workflow trigger. Max could ask for an empty commit instead, or for the factory to wait for a person to start CI.
- **When:** 2026-11-23 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/1458

- **Decision:** `ci.local_gate.timeout_seconds` is capped at 1800, twice `review.pre_ready`'s 900, because the gate stands in for a whole CI run. Max could ask for the 900 cap `pre_ready` uses.
- **When:** 2026-11-23 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/1458

- **Decision:** `bad-target-branch` and `stack-without-depends` refuse intake (`refused-intake`, `intake-refused`, one Grok wake per defect set) rather than warn in the brief, because a wrong base builds the whole ticket on the wrong commits. Max could ask for `stack-without-depends` to only warn, with the stack parent added as a blocker by the feeder.
- **When:** 2026-11-23 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/1458

- **Decision:** A stacked child whose parent PR closes without merging raises `stack-parent-closed`, starts no job, and waits for a person. The factory never retargets it on its own, because the parent's commits may or may not belong in the child. Max could ask for an automatic retarget to the default branch, with the parent's commits then showing in the child's diff for review.
- **When:** 2026-11-23 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/1458

- **Decision:** The statechart rows for the retarget (the SPEC section 4.2 T-table and `model/`) are left to the implementing ticket (T9). This ticket writes only the contract, and a row with no code behind it would put the docs and the model out of step. Max could ask for the rows to be drafted here.
- **When:** 2026-11-23 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/1458

- **Decision:** The #205 stacked-branch hold compares a stacked child's branch against its recorded base, the parent's branch, so the parent's own commits never hold the child, while a third open PR's commits still do. Max could ask for the hold to compare against the default branch and treat the parent as an allowed exception instead.
- **When:** 2026-11-23 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/1458

- **Decision:** A target branch missing on GitHub holds the ticket in `waiting-blocked` with the reason "target branch X does not exist", raises `target-branch-missing`, wakes Grok once per defect set, and feeds on the first poll the branch exists. It is a hold, not a refusal, because the branch is often pushed after the ticket is filed. Max could ask for a missing branch to refuse intake instead.
- **When:** 2026-11-23 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/1458

- **Decision:** The retarget bounds the child's own commits by an explicit old base, proven against every head the parent's branch held while the child was stacked. Those heads are `stack_parent_head_at_feed`, the parent's head at merge, and the `beforeCommit` of each force-push event in the parent PR's GitHub timeline. GitHub records every force-push, and any other push keeps the earlier head as an ancestor, so the set reaches every parent commit the child could have taken in, including a parent update the child merged before the parent was rewritten. The child's own commits are the old head's commits that no parent head reaches. The old base is a merge base of the old head and a parent head whose two-dot range lists exactly those commits. With no such merge base, a force-push event with no before commit, or a timeline the factory cannot read in full, the retarget fails closed as `stack-retarget-failed`. Feed-time ancestry alone was rejected, because a child that merged a parent update holds parent commits above the feed head, and the review would read them. The rebase replays and the scoped review reads only `<old-base>..<old-head>`, never a three-dot revision range, because a three-dot range takes the parent's commits too. Max could ask for the factory to record the parent heads it polls instead of reading the timeline, but a head pushed and force-pushed away between two polls would escape that record.
- **When:** 2026-11-23 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/1458
