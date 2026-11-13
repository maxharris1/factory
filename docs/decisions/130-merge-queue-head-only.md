- **Decision:** The merge queue acts only on the head of each repo's queue (the oldest recorded approval, then ticket id): at most one merge per repo per poll, and only the head gets an update-from-main job, while every other approved ticket waits.
- **When:** 2026-11-13 4:36 PM ET, by Factory decided (technical)
- **Source:** [#130 plan on PR #131](https://github.com/maxharris1/factory/pull/131), "Decisions this plan makes", 4

Two approved PRs updated at once keep re-conflicting each other; one at a time does not. The cost is head-of-line blocking: a head waiting on CI or an approver's `ok` holds its repo's queue. A head that goes stuck (`update-failed-2`, `merge-failed`) leaves the queue, so the next one moves up. A carried approval keeps its `approved_at`, so an update does not lose the ticket its place.
