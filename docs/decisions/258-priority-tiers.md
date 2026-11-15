# #258: priority tiers and critical-path order

Defaults from the approved plan for issue #258, with no product question open.

- **Decision:** A project's `intake.priority_labels` sets build tiers: a ticket's tier is the index of the first listed label it carries, case-insensitive. A ticket with none of them sorts after every tier, and `urgent` orders tickets inside a tier, so a newer `priority:p0` ticket goes ahead of an older urgent one with no tier. A project with no list orders as before.
- **When:** 2026-11-14 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/issues/258#issuecomment-6887560696

- **Decision:** One rank serves the feeder, `orch status`, the dashboard and the claim order: tier (none last), then urgent, a resumed paused step (claim order only), bug, then how many open intake tickets the ticket unblocks, then the oldest. `priority_rank` is gone.
- **When:** 2026-11-14 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/issues/258#issuecomment-6887560696

- **Decision:** `priority_from_linear: true` maps Linear priority 1-4 to tiers 0-3 when no listed label matches. Linear priority 1 still counts as `urgent` whether or not the project opts in. `check-config` refuses the key on a project without `linear_project_id`.
- **When:** 2026-11-14 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/issues/258#issuecomment-6887560696

- **Decision:** A ticket unblocks the candidates whose `Blocked by` names it or its PR, the unstarted candidates in its repo that share a `Touches:` path with it (outside `shared_paths`), and every candidate blocked by one of those, transitively. The count comes from the gate lines and feeder rows the poll already read, with no extra tracker call. It is stored in `ticket_priority` too, so a fed ticket that unblocks many is also claimed first. `critical_path_order: false` makes every count 0.
- **When:** 2026-11-14 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/issues/258#issuecomment-6887560696
