- **Decision:** The poller, never the builder, decides whether an update from base keeps the approval, with git plumbing on the waiter's bare clone (`lib/update_check.py`), and any error, missing check or doubt counts as non-trivial and goes to a cross-family re-review.
- **When:** 2026-11-13 4:36 PM ET, by Factory decided (technical)
- **Source:** [#130 plan on PR #131](https://github.com/maxharris1/factory/pull/131), "Decisions this plan makes", 5

Trivial means: the new head is a two-parent merge whose first parent is the approved head and whose second parent is on base; every path other than the conflicted ones equals `git merge-tree --write-tree`'s result exactly; every conflicted path is a text content conflict (both sides present, the same regular-file mode, no binary); and its resolved content is `git merge-file`'s merged text with each conflict hunk replaced by exactly its ours side or exactly its theirs side. The builder's own claim is never trusted for this.
