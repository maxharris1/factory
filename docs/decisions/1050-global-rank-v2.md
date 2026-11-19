# #1050: global rank v2

Decisions from the approved plan for issue #1050. One rank orders both the feeder and each harness's claim order. Its keys, in order:

1. The effective tier. An untiered ticket sorts after every tier.
2. In the claim order only, a finishing job (`review`, `fix`, `update-from-main`, `qa`) before a starting job (`plan`, `build`).
3. Urgent, then a resumed paused step (claim order only), then bug, then the number of tickets it unblocks, then age.

Feeder room and the project share decide whether, and for which project, the feeder feeds next. The reviewer's round 1 asked for every criterion in this PR, so all four planned parts ship together.

- **Decision:** A ticket that carries `urgent` (an `urgent` or `priority:urgent` label, or Linear priority Urgent) and has no tier from `intake.priority_labels` or `priority_from_linear` is tier 0, in both the feeder order and the claim order. An explicit tier always wins, so an urgent `priority:p2` ticket stays tier 2. `urgent` stays the next key inside a tier, so an urgent ticket with no tier ties a `priority:p0` ticket on tier and goes first on urgent. Before this, an urgent ticket with no tier sorted after every tier.
- **When:** 2026-11-19, by Factory decided (technical), on Max's go for the split
- **Source:** https://github.com/maxharris1/factory/pull/1059#issuecomment-7016901140

- **Decision:** The rank uses a ticket's effective tier: the best (lowest) tier among its own tier and the own tiers of every candidate it unblocks through `Blocked by` or `Depends on`, directly or transitively. A blocker of a p0 ticket ranks as p0, so a p1 blocker of a p0 ticket goes ahead of an unrelated p1 ticket. The graph comes from the gate lines and feeder rows the poll already reads, with no extra tracker call. A blocker cycle ends at the first ticket seen twice. Every candidate the poll reads passes its tier on, including one already fed or whose feed failed, so a `Blocked by` line added after a ticket was fed still promotes its blocker. The unblocks count still counts only unstarted tickets. Shared `Touches:` paths count toward `unblocks` but not toward the effective tier, because only a blocker line says one ticket must finish first.
- **When:** 2026-11-19, by Factory decided (technical), on Max's go for the split
- **Source:** https://github.com/maxharris1/factory/pull/1059#issuecomment-7016901140

- **Decision:** The effective tier applies even with `critical_path_order: false`. That switch zeroes the `unblocks` tie-break. The effective tier fixes a priority inversion, which is a different problem. Max can ask for the tier to follow the switch instead.
- **When:** 2026-11-19, by Factory decided (technical), on Max's go for the split
- **Source:** https://github.com/maxharris1/factory/pull/1059#issuecomment-7016852764

- **Decision:** `ticket_priority` and the feeder table keep both values: `tier` is the effective tier, which the claim order reads, and `own_tier` is the ticket's own. `orch status` and the dashboard show `tier 0 (own 1)` when the two differ, `tier 0 (own none)` when the ticket has no tier of its own, and `tier 1` when they match. A database from before this change reads `own_tier` as unset and shows `tier N` alone.
- **When:** 2026-11-19, by Factory decided (technical), on Max's go for the split
- **Source:** https://github.com/maxharris1/factory/pull/1059#issuecomment-7016852764

- **Decision:** In the claim order, inside one effective tier, a `review`, `fix`, `update-from-main` or `qa` job goes before a `plan` or `build` job, so work that finishes a ticket in flight beats work that starts a new one. The tier stays the first key, so a p0 build still goes before a p2 fix, and a p2 fix goes before a p3 or untiered build. Every finishing action belongs to a ticket past intake, so the action alone decides. Other actions count as starting, as every job did before. The feeder order has no finishing key. The order stays per harness: two harnesses' waiters can still race for one shared slot, and the room rule below stops the feeder from adding to that race.
- **When:** 2026-11-19, by Factory decided (technical), on Max's go for the split
- **Source:** https://github.com/maxharris1/factory/pull/1059#issuecomment-7016852764

- **Decision:** Feeder room for a harness subtracts the queued jobs of every harness listed on a schedulable device that also lists the builder's harness, not only the builder's own. A queued codex review on a device shared with claude takes one claude slot from the room. The count is conservative: a codex job that could also run on a codex-only device still counts. Feeding one ticket too few is cheaper than starving a review.
- **When:** 2026-11-19, by Factory decided (technical), on Max's go for the split
- **Source:** https://github.com/maxharris1/factory/pull/1059#issuecomment-7016852764

- **Decision:** An open issue in a tracked repo that a candidate names in `Blocked by` or `Depends on`, and that is neither a candidate nor a fed ticket, is a critical-path blocker. The feeder raises one needs-attention flag per blocker, reason `critical-path-blocker`, detail `gates N candidates, best tier T`, from the statuses the gate read already makes. The flag clears when the blocker closes. A failed status read keeps it. `orch status` lists the flags under `Critical-path blockers`.
- **When:** 2026-11-19, by Factory decided (technical), on Max's go for the split
- **Source:** https://github.com/maxharris1/factory/pull/1059#issuecomment-7016852764

- **Decision:** A project may set `intake.capacity_share`, a fraction from 0 to 1. `check-config` refuses a value outside that range, a bool, and shares that sum above 1 across projects. While a project with a share holds fewer in-flight tickets than its share of the schedulable slots of its builder harness, the feeder feeds that project's best eligible ticket before any other project's. In-flight means a ticket with a queued or running harness job, fed in the last 10 minutes with no job yet, or fed earlier in the same poll. It does not count open PRs that wait on a merge OK, which would hold a project over its share for days. With no share set, the order is exactly the one before.
- **When:** 2026-11-19, by Factory decided (technical), on Max's go for the split
- **Source:** https://github.com/maxharris1/factory/pull/1059#issuecomment-7016852764
