- **Decision:** The cross-family re-review after a non-trivial update from base runs at the approved round, not a new one, so it uses up no extra review round; a REVISE then follows the normal fix loop and cap.
- **When:** 2026-11-13 4:36 PM ET, by Factory decided (technical)
- **Source:** [#130 plan on PR #131](https://github.com/maxharris1/factory/pull/131), "Decisions this plan makes", 3

After T4, `review_round` is the approved round minus one, so the re-review is `review_round + 1`, the same number as the approval it replaces. This matches the rule that gate retries are not review rounds: bringing base in is not new work the reviewer asked for.
