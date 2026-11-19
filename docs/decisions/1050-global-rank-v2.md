# #1050: global rank v2

Decisions from the approved plan for issue #1050, built in four parts in order. Part 1 (#1050a) is the effective tier. Parts 2 to 4 (finish first and shared room, critical-path blockers, project share) add their bullets here when they land.

- **Decision:** A ticket that carries `urgent` (an `urgent` or `priority:urgent` label, or Linear priority Urgent) and has no tier from `intake.priority_labels` or `priority_from_linear` is tier 0, in both the feeder order and the claim order. An explicit tier always wins, so an urgent `priority:p2` ticket stays tier 2. `urgent` stays the next key inside a tier, so an urgent ticket with no tier ties a `priority:p0` ticket on tier and goes first on urgent. Before this, an urgent ticket with no tier sorted after every tier.
- **When:** 2026-11-19, by Factory decided (technical), on Max's go for the split
- **Source:** https://github.com/maxharris1/factory/pull/1059#issuecomment-7016901140

- **Decision:** The rank uses a ticket's effective tier: the best (lowest) tier among its own tier and the own tiers of every candidate it unblocks through `Blocked by` or `Depends on`, directly or transitively. A blocker of a p0 ticket ranks as p0, so a p1 blocker of a p0 ticket goes ahead of an unrelated p1 ticket. The graph comes from the gate lines and feeder rows the poll already reads, with no extra tracker call. A blocker cycle ends at the first ticket seen twice. Shared `Touches:` paths count toward `unblocks` but not toward the effective tier, because only a blocker line says one ticket must finish first.
- **When:** 2026-11-19, by Factory decided (technical), on Max's go for the split
- **Source:** https://github.com/maxharris1/factory/pull/1059#issuecomment-7016901140

- **Decision:** The effective tier applies even with `critical_path_order: false`. That switch zeroes the `unblocks` tie-break. The effective tier fixes a priority inversion, which is a different problem. Max can ask for the tier to follow the switch instead.
- **When:** 2026-11-19, by Factory decided (technical), on Max's go for the split
- **Source:** https://github.com/maxharris1/factory/pull/1059#issuecomment-7016852764

- **Decision:** `ticket_priority` and the feeder table keep both values: `tier` is the effective tier, which the claim order reads, and `own_tier` is the ticket's own. `orch status` and the dashboard show `tier 0 (own 1)` when the two differ, and `tier 1` when they match. A database from before this change reads `own_tier` as unset and shows `tier N` alone.
- **When:** 2026-11-19, by Factory decided (technical), on Max's go for the split
- **Source:** https://github.com/maxharris1/factory/pull/1059#issuecomment-7016852764
