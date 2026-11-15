# #198: base drift after APPROVE is already routine

#131 (issue #130) already does what #198 asks for, so #198 adds no dispatcher code. This note maps each part of the ticket to the code and the tests that do it, at `main` 02a7ffe.

- **Decision:** An approved PR that goes dirty against its base already gets one base-update job and one re-review that spends no round, and then merges with no human step. #198 changes no code. It records this and adds `tests/test_base_drift.py`, which pins the one part no test covered: a behavior-changing update gets a normal round.
- **When:** 2026-11-15 ET, by Factory decided (technical)
- **Source:** [#198 plan on PR #433](https://github.com/maxharris1/factory/pull/433#issuecomment-6900794978)

## Where #131 does each step

1. **One base-update job.** When the merge-queue head is `dirty` or `behind` and no update is out, `lib/dispatch/merge_queue.py` queues one `update-from-main` job (`_queue_update`). Its dedupe key holds the head, the failure count and the generation, so later polls queue nothing more. Two failed updates mark the ticket stuck (`update-failed-2`). Pinned by `test_a_conflicting_queue_head_gets_one_update_job` and `test_two_failed_update_jobs_stop_and_ask_max`.
2. **The base-update renderer.** `lib/brief/__init__.py` sends `update-from-main` to the `merge-only` brief (`lib/brief/merge_only.py`).
3. **A re-review that spends no round.** `_settle_update` keeps the approval when each conflict hunk takes exactly one side (`lib/update_check.py`), and the PR merges with no re-review. Any other update is re-reviewed at the approved round, per `130-update-rereview-round.md`. Pinned by `test_trivial_update_carries_the_approval_and_merges` and `test_non_trivial_update_is_re_reviewed_at_the_approved_round`.
4. **Then ships.** The re-review's APPROVE goes back to the merge queue and the poller merges it. Pinned by the same two tests and by `test_replay_an_approved_pr_conflicts_is_updated_and_merges_with_no_human`.
5. **A behavior-changing update gets a normal round.** No code can tell whether an update changed behavior, so the reviewer decides. A REVISE on the re-review starts the normal fix loop, and that fix and its review spend the next round. Pinned by `tests/test_base_drift.py`.

## What #198 leaves out

- **Decision:** The `merge-only` brief kind keeps its name, though the ticket's What says "renamed". No acceptance criterion asks for the rename. The kind is stored in queued job rows and pinned by brief snapshots, so a rename would break jobs queued before a deploy or need a reader for both names. It would also touch files that #196, #203, #206 and #207 change. Max could ask for the rename as its own ticket.
- **When:** 2026-11-15 ET, by Factory decided (technical)
- **Source:** [#198 plan on PR #433](https://github.com/maxharris1/factory/pull/433#issuecomment-6900794978), "Decisions this plan makes", 1
