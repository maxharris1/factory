# #205: stacked-branch holds and the `Depends on:` gate

Defaults the builder picked for issue #205, with no product question open. Each one says what Max could ask for instead.

- **Decision:** #156 parsed only `Blocked by`, not `Depends on:`, so a `Depends on` line is now read as a `Blocked by` line by the same #156 parser (`lib/feeder_gates.py`). It shares the blocker graph, the waits, the cycle, closed and missing flags, and the status read. There is no second gate. `Depends on: none` names no ref and gates nothing. Every ref on the line counts, prose included, as on a `Blocked by` line.
- **When:** 2026-11-14 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/332

- **Decision:** The stacked-branch hold checks a ticket's branch at intake when the branch already exists (a Linear gitBranchName someone pushed to, a branch made by hand) or a resumed intake reuses its recorded PR. A branch the feeder creates holds only its own start commit, so it is not read then. The check runs again before a builder's ready queues review, so a PR stacked during its build, as #131 was, waits there with the same one wake per set of PRs and is reviewed after the PR merges. The review-time check compares the base with the ready's pinned head commit, not a branch name, so a Linear ticket's suggested branch, a fork PR's same-named base branch or an unusual branch name cannot stand in for the reviewed commits. It runs only when the dispatcher has a feeder.
- **When:** 2026-11-15 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/332

- **Decision:** A branch carries another PR's commits when GitHub lists that PR as open for any commit the base branch lacks (`compare` plus `commits/<sha>/pulls`). The ticket's own PR on the branch never holds it. The hold ends when the PR is no longer open. A PR closed without merging also ends it, and its commits then show in the ticket's own diff for review.
- **When:** 2026-11-14 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/332

- **Decision:** One Grok `blocked` wake per ticket per set of stacked PRs, deduped in the job queue like the intake `refused` wake. The set is part of the wake's source id, so a changed set reaches Grok under a new delivery key. A held ticket is `waiting-blocked` with a reason that names the PRs and the branch (or, before review, the head commit), and it raises no needs-attention flag, because it clears on its own when the PR merges.
- **When:** 2026-11-14 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/332
