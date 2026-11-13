# Plan for #108: ticket lifecycle

A ticket's lifecycle doesn't end cleanly today. A merged PR can stay stuck, paused or `handed-off` in `ticket_state`. Work done after a handoff happens outside the factory. And one lost state directory erases all dedupe history, so finished tickets get rebuilt.

This plan fixes all three in one PR (#113, branch `factory/issue-108`). It folds in #42 (N1, N3, N4, N5) and #66.

The rules it enforces:

- `merged` is absorbing. Every path that sees a merge goes through one transition, and that transition clears everything that would keep the ticket on a person's screen.
- A `handed-off` ticket is still watched, but slowly. A trusted ready re-enters review only inside the effective cap from #105.
- Local history is never the only thing between the factory and a duplicate build. The DB is backed up every cycle, `ensure` restores it, and the thread's own orch:v1 markers are checked before a job is queued.

## How to read this

One box is one unit of work. Each box names the evidence that checks it: a test, a log line or a SHA. Only check a box when that evidence exists.

This is a factory plan job. The build job for #108 does the work under `pstack/skills/poteto-mode/playbooks/feature.md`. There is one PR. Codex Bot reviews it (risk high, up to three rounds), and Grok Bot merges it on `ship: handoff`. This is one backend PR with no UI, so the poteto multi-phase skeleton's cloud lanes don't apply. Its verification rule does apply, adapted in **Verify** below.

Tests alone don't verify the PR. It is verified only when its unit, replay and CI boxes are all checked.

## Dependencies and base

- **#105 (PR #110) must merge first.** This plan builds on its `ticket_state.cap_extra`, `Dispatcher._effective_cap(ticket, connection)`, `_over_cap`, `_flag_ready_after_cap` (needs-attention `ready-after-cap`) and the job-fact `round=`/`risk=` headers. When this plan was written (2026-11-13), #110 was approved at round 2 and waiting on its round-3 re-review of a merge of main. The build's first step is a merge commit of `origin/main` into `factory/issue-108` after #110 lands. There is no rebase and no force-push. If #110 hasn't merged when the build job starts, the builder posts a `questions` handoff instead of building against an unmerged API.
- **#107 (PR #111)** also edits `deploy/poller-service.sh`, `deploy/install.sh` and the `poll_errors` write in `lib/dispatcher.py`. This PR changes only the DB-restore step in `poller-service.sh` (a new function called from `start` and `ensure`), plus one `mkdir -p` line in the profile that `install.sh` writes. If #111 merges first, merge main and keep both. If it merges second, its builder merges ours. The two edits don't overlap.

## The data shape

| Fact | Where it lives | Who writes it | Who reads it |
| --- | --- | --- | --- |
| A merged ticket | `ticket_state.workflow_state = 'merged'`, with `stuck = 0`, the `stuck_*` and `paused_*` columns NULL, no `needs_attention` rows, and no queued harness jobs | One function, `Dispatcher._enter_merged(connection, repo, ticket_id, reason)`, called by `record_terminal`, `_complete_ship_intent`, `_complete_redrive` and `_settle_external_ship` | `orch status`, the dashboard, `_tracked_refs` |
| A cancelled job | `jobs.state = 'cancelled'` (new value in `lib/queue.py` `STATES`), `finished_at` set, the dedupe key kept, plus a `job_events` row `cancelled` with the reason | `SQLiteJobQueue.cancel(job_id, reason, connection=None)`, called by `orch job cancel` and by `_enter_merged` | T6 (it never sees a cancelled job), status, dashboard |
| Re-entry round | `ticket_state.review_round` is set to the round of the APPROVE that shipped, so the next review is `review_round + 1` against `_effective_cap` | `_t2`'s handed-off branch, in one `BEGIN IMMEDIATE` transaction | `_queue_review`, the job detail `round=` |
| Stopped-state cadence | `Dispatcher._cycle` (an in-memory counter) and the constant `STOPPED_POLL_SECONDS = 300` | `poll_once` | `_tracked_refs` |
| State backup | `$ORCH_DB_BACKUP`, default `~/.local/share/factory/backup/orch.db`. That directory survived the #66 reboot, and the venv lives beside it | `lib/state_backup.py` `backup(db, dest)`, after every poll cycle | `orch restore-db`, which `deploy/poller-service.sh` `start` and `ensure` call when the DB is missing |

## Criterion-to-work map

| # | Acceptance criterion | Work that meets it | Tests that fail on revert |
| --- | --- | --- | --- |
| 1 | When a PR merges (by the factory or by hand), the ticket moves to `merged` and clears stuck, paused and needs-attention. The shipper treats an already-merged PR as success, not ship-failed (#42-N3). | Unit 2 (one merged transition), Unit 3 (every tracked ticket's merge is seen, including handed-off tickets and Linear-linked PRs), Unit 4 (shipper and handoff) | `test_merge_clears_stuck_paused_and_attention`, `test_merge_cancels_queued_harness_jobs_only`, `test_merged_is_absorbing`, `test_handed_off_ticket_moves_to_merged_when_merged_by_hand`, `test_linear_ticket_with_merged_pr_moves_to_merged`, `test_fresh_read_of_a_merged_pr_records_nothing`, `test_review_shipper_reads_a_merged_pr`, `test_handoff_of_an_already_merged_pr_is_merge_confirmed`, `test_handoff_redrive_of_an_already_merged_pr_is_merge_confirmed` |
| 2 | A trusted ready after `handed-off` re-enters review within the effective cap from #105. Past the cap, it marks needs-attention and queues nothing. | Unit 3 (handed-off tickets are polled), Unit 5 (re-entry) | `test_ready_after_handoff_reenters_review_at_the_next_round`, `test_ready_after_handoff_past_the_cap_flags_and_queues_nothing`, `test_ready_after_handoff_with_an_extra_round_reenters`, `test_ready_at_the_shipped_head_is_a_no_op`, `test_untrusted_ready_after_handoff_is_rejected`, `test_verdict_after_handoff_without_reentry_is_silent`, `test_go_after_handoff_changes_nothing` |
| 3 | #66: discovery checks GitHub or the tracker for an existing PR or orch:v1 marker before queuing a build. The state DB is backed up every cycle and restored by `ensure` when missing. `orch job cancel <id>` exists. | Unit 6 (marker check before queuing), Unit 7 (backup and restore), Unit 1 (`orch job cancel`) | `test_go_with_a_later_trusted_ready_queues_no_build`, `test_fresh_db_replay_queues_no_build_for_a_finished_ticket` (the AUT-2284 and AUT-2286 replay), `test_untrusted_later_ready_does_not_suppress_the_build`, `test_ready_with_a_later_verdict_queues_no_review`, `test_revise_with_a_later_ready_queues_no_fix`, `test_poll_cycle_writes_a_backup`, `test_backup_failure_is_a_poll_error_not_a_crash`, `test_restore_db_restores_a_missing_db`, `test_restore_db_refuses_an_existing_db`, `test_ensure_restores_a_missing_db_from_backup`, `test_ensure_inits_and_warns_with_no_backup`, `test_job_cancel_cancels_a_queued_job`, `test_job_cancel_refuses_a_running_job`, `test_cancelled_job_is_never_retried` |
| 4 | Update the remaining #42 items (N1 and N4 request budgets, N5 config validation), or split them out explicitly. | Unit 8. N1 and N5 are already on main: verify each fix bullet against a named test and record it. N4: build the stopped-state cadence (Unit 3) and the GitHub request budget in SPEC §7. Split the comment-page ETag, plus #42's N6, #40-N1/N2 and #43 m-a..m-d, into one new follow-up issue linked from #42 | `test_stopped_states_are_read_every_300_seconds`, `test_active_states_are_read_every_cycle`, `test_stopped_reads_are_spread_across_cycles`. N1 and N5 cite their existing tests in the #42 note |
| 5 | Tests that fail on revert, and SPEC rows updated. | Every unit names its tests. Unit 9 updates SPEC §4.1 (chart), §4.2 (T2, T4, T6 and a new merge-observed row), §4.3 (invariants), §7 (budgets, backup path), §9 (state loss), the decision log and the TLA+ model | `env -u ORCH_DB pytest`, `bash model/check.sh`, CI |

Every criterion is mapped.

## Work units

Each unit is one commit that ends green under `env -u ORCH_DB pytest`. Write the test first in every unit. The order puts each unit on a verified base: Unit 2 uses Unit 1's cancel, and Units 4 and 5 use Unit 2's transition and Unit 3's polling.

### Unit 0. Base

- [x] After #110 merges, run `git merge origin/main` into `factory/issue-108` (a merge commit, never a rebase). Evidence: the merge SHA, and `env -u ORCH_DB pytest` green on it.

### Unit 1. `orch job cancel <id>` (AC 3)

**Files.** `lib/queue.py`, `lib/cli.py`, `lib/status.py` and `lib/dashboard.py` (state display only), `tests/test_queue.py`, `tests/test_cli.py`.

**Build.**

- [x] Add `cancelled` to `queue.STATES`. Add `SQLiteJobQueue.cancel(job_id, reason, *, connection=None)`. It moves a `queued` job to `cancelled` with `finished_at`, and writes a `job_events` row (`event='cancelled'`, `detail=reason`), in one transaction. It raises `QueueError` for a job that is running (claimed, or started), done, failed or missing. The dedupe key stays, so the comment that queued the job can't queue it again.
- [x] `orch job cancel <job_id> [--db] [--reason TEXT]` (the default reason is `operator`). It prints `cancelled <id> (<repo>#<ticket> <action> -> <recipient>)` and exits 0. A running job exits 1 with `job is running; stop its worker first`, and a missing job exits 2. It is not bot-owned: it is an operator command, so it doesn't go through `owned_job`. Update `USAGE` and the README CLI table.
- [x] `orch status` and the dashboard count `cancelled` as finished.

**Verify.** Run `env -u ORCH_DB pytest tests/test_queue.py tests/test_cli.py -k cancel`.

- [x] `test_job_cancel_cancels_a_queued_job`: state, event row, and exit 0.
- [x] `test_job_cancel_refuses_a_running_job`: exit 1, and the row is unchanged.
- [x] `test_cancelled_job_is_never_retried`: `unhandled_failures()` and `outstanding()` both skip it, and re-enqueueing the same dedupe key returns the cancelled job.

### Unit 2. One merged transition (AC 1)

**Files.** `lib/dispatcher.py`, `tests/test_dispatcher.py`.

**Build.**

- [x] Add `_enter_merged(connection, repo, ticket_id, reason)`. Inside the caller's transaction it:
  - sets `workflow_state = 'merged'`;
  - sets `stuck = 0` and NULLs `stuck_reason`, `stuck_source_comment_id`, `stuck_wake_job_id` and every `paused_*` column;
  - deletes the ticket's `needs_attention` rows;
  - cancels the ticket's queued harness jobs (claude, codex and cursor) through `queue.cancel(..., reason=f"merged:{reason}", connection=connection)`.
  
  Grok wakes are left alone, so a `done` wake queued by the ship still goes out. Running jobs are left alone too. Their handoffs land on a ticket that is no longer polled.
- [x] `record_terminal(repo, ticket_id, "merged")`, `_complete_ship_intent(..., "merged")`, `_complete_redrive(..., "merged")` and `_settle_external_ship` all call it. `record_terminal` keeps its `INSERT ... ON CONFLICT` upsert for an untracked row.
- [x] `merged` is absorbing. `_set_workflow_state`, `_complete_ship_intent`, `_complete_redrive` and the T3 and T5 state writes add `AND COALESCE(workflow_state, '') NOT IN ('merged', 'done')`. A ship-intent replay on a ticket that is already merged writes the `merged` label, not the intent's own state.
- [x] The merge-observed transition writes `orch:state/merged` on GitHub-issue repos, best effort. A failed label write is a `poll_errors` row, and it never undoes the state.

**You see.** After a hand merge, `orch status` shows nothing for the ticket under Paused or needs-attention. `ticket_state` reads `merged|0|NULL`.

**Verify.** Run `env -u ORCH_DB pytest tests/test_dispatcher.py -k "merge or merged"`.

- [x] `test_merge_clears_stuck_paused_and_attention`: a `stuck=1` ticket with a `ci-red-3` reason, one `waiting-max` ticket with `paused_*` set and one `ship-failed` flag. Each gets `record_terminal(..., "merged")`. This is the #87, #88 and #98 evidence.
- [x] `test_merge_cancels_queued_harness_jobs_only`: a queued review is cancelled, a running fix is untouched and a queued Grok `done` wake is untouched.
- [x] `test_merged_is_absorbing`: a replayed completed `handoff` intent on a merged ticket leaves it `merged`.

### Unit 3. See every tracked ticket's merge, and read stopped states slowly (AC 1, AC 2, #42 N4)

**Files.** `lib/dispatcher.py` (`_tracked_refs`, `poll_once`), `lib/poll_source.py` (`_github_ticket`, `_linear_ticket`, `fresh_ticket`), `tests/test_dispatcher.py`, `tests/test_live_poll.py`.

**Build.**

- [x] `_tracked_refs` returns `handed-off` tickets too, not only those with a pending ship-intent replay. `allow_merged` is 1 only for that replay case.
- [x] Stopped states are `handed-off`, `capped`, `blocked`, `refused` and any `stuck = 1` ticket. These are read once every `STOPPED_POLL_SECONDS = 300`, which is 10 cycles at `poll_seconds: 30`. Each one is read on the cycle where `(cycle + crc32(repo#ticket)) % every == 0`, so the reads spread out instead of bursting. Every other state is read every cycle, as today. `awaiting-ok`, `waiting-max` and `waiting-external` stay at full speed because a person is waiting on them.
- [x] In `_github_ticket`, a merged pull always calls `terminal_sink(repo, number, "merged")`, then raises `_TerminalTicket` unless `allow_merged`. With `fresh=True` it never calls `terminal_sink` (#42 N3: the fresh read is the shipper's, mid-ship). It returns the ticket with a new `TrackedTicket.merged = True` instead, and the caller decides.
- [x] `_linear_ticket` does the same for its linked PR: merged calls `terminal_sink(repo, identifier, "merged")` and skips the ticket. Aut0's hand merges then end cleanly too.

**Verify.** Run `env -u ORCH_DB pytest tests/test_dispatcher.py tests/test_live_poll.py -k "stopped or merged or handed_off"`.

- [x] `test_handed_off_ticket_moves_to_merged_when_merged_by_hand`: this is #89, #95 and #97. Revert the `_tracked_refs` change and the ticket stays `handed-off`.
- [x] `test_linear_ticket_with_merged_pr_moves_to_merged`.
- [x] `test_fresh_read_of_a_merged_pr_records_nothing`.
- [x] `test_stopped_states_are_read_every_300_seconds`, `test_active_states_are_read_every_cycle`, `test_stopped_reads_are_spread_across_cycles`.

### Unit 4. The shipper treats an already-merged PR as success (AC 1, #42 N3)

**Files.** `lib/dispatcher.py` (`ReviewShipper._ticket`, T4 `handoff` branch, `_redrive_handoffs`), `lib/github_actions.py` (`_mark_ready`), `tests/test_dispatcher.py`, `tests/test_github_actions.py`, `tests/test_hooks.py`.

**Build.**

- [x] `ReviewShipper._ticket` builds its `TicketRef` with `allow_merged=True`. A merged ticket is then read, not raised as `_TerminalTicket`, and the existing `is_merged(ticket, head)` branch confirms it.
- [x] `_mark_ready` raises a typed `AlreadyMergedError(GitHubAdapterError)` in place of the plain "cannot hand off a merged pull request".
- [x] T4's `ship == "handoff"` branch and `_redrive_handoffs` catch `AlreadyMergedError`. They complete the intent as `merged` through Unit 2's transition, write the label, wake Grok `done` with the note `merged before handoff`, and return `merge-confirmed`. This is no `ship-failed` wake and no needs-attention flag. This was the 7:13 PM and 9:32 PM 2026-11-12 wakes on #74, #81 and #82.

**Verify.** Run `env -u ORCH_DB pytest -k "already_merged or merged_pr or shipper"`.

- [x] `test_review_shipper_reads_a_merged_pr`.
- [x] `test_handoff_of_an_already_merged_pr_is_merge_confirmed`: the outcome is `merge-confirmed`, the state is `merged`, there is one `done` wake and zero `ship-failed` wakes. Revert the catch and the wake is `ship raised GitHubAdapterError`.
- [x] `test_handoff_redrive_of_an_already_merged_pr_is_merge_confirmed`.

### Unit 5. Re-entry after handoff (AC 2)

**Files.** `lib/dispatcher.py` (`_t2`, `_t1`, `_t3`, `_t4`), `tests/test_dispatcher.py` or `tests/test_rounds_and_caps.py` (added by #105).

**Build.**

- [x] In `_t2`, after the trust check, a `handed-off` ticket takes a new branch:
  1. Let `r` be the round of the APPROVE whose ship intent completed (`_latest_review_verdict`). If it has no round, use `review_round + 1`.
  2. If the ready's head matches the shipped head, return `ready-already-shipped` and do nothing.
  3. If `r + 1 > _effective_cap(ticket)`, call `_flag_ready_after_cap(...)`, return `ready-after-cap`, queue nothing and leave the state as `handed-off`.
  4. Otherwise, apply the usual head match (fresh re-read) and CI checks. `ci-pending` returns False before any write.
  5. Then, in one `BEGIN IMMEDIATE`, set `review_round = r` (the approved round counts as completed) and fall through to the normal green path (`_queue_review`, state `review`) or red path (CI fix at round `r + 1`). The old completed ship intent stays. The next APPROVE is a new intent keyed on its own comment.
- [x] A trusted `go` on a `handed-off` ticket returns `go-after-handoff` and changes nothing: no job and no state write. Today it sets `building` and drops its build into dedupe, which would skip the cap check above. In this repo's flow (`ready_starts_review`), Grok's go is followed by the builder's ready, and that ready is what re-enters.
- [x] A `verdict` on a `handed-off` ticket that hasn't re-entered is marked handled `after-handoff`, with no wake. Polling handed-off tickets would otherwise wake Grok once for every stray post-handoff verdict already sitting on open PRs.
- [x] **Known limit.** Max can't raise the cap on a `handed-off` ticket through the factory, because #105's resume only acts on `capped`. The needs-attention flag tells him. A queued fix from a post-handoff go, for queue-built projects, goes in the follow-up issue (Unit 8).

**You see.** On #110's shape (APPROVE at 2/3, handed off, then a trusted ready at a new head with green CI), a review job is queued with `"round":"3/3"`, the state is `review` and the label is `orch:state/review`. A second re-entry after an APPROVE at 3/3 gives `ready-after-cap` under needs-attention, and no job.

**Verify.** Run `env -u ORCH_DB pytest -k "after_handoff or already_shipped"`.

- [x] `test_ready_after_handoff_reenters_review_at_the_next_round`. Revert Unit 3's `_tracked_refs` change or this branch, and no review is queued.
- [x] `test_ready_after_handoff_past_the_cap_flags_and_queues_nothing`.
- [x] `test_ready_after_handoff_with_an_extra_round_reenters` (`cap_extra = 1`).
- [x] `test_ready_at_the_shipped_head_is_a_no_op`.
- [x] `test_untrusted_ready_after_handoff_is_rejected`.
- [x] `test_verdict_after_handoff_without_reentry_is_silent`.
- [x] `test_go_after_handoff_changes_nothing`.

### Unit 6. Check the thread's markers before queuing (AC 3, #66 fix 1)

**Files.** `lib/dispatcher.py`, `tests/test_dispatcher.py`.

**Build.**

- [x] Add `_answered_by(ticket, comment, kind, bot, role, head=None)`. It returns the first later comment (by `_comment_order`) that parses as orch:v1 `kind`, comes from a login trusted for `bot` and `role` (`_trusted`), and, when `head` is given, names a matching head. Its handled outcome must not be in `_REJECTED_OUTCOMES`.
- [x] Before a job is queued:
  - T1 and T1P build or plan: skip when a later trusted builder `ready` or `plan` exists.
  - T2 review: skip when a later trusted reviewer `verdict` names this head.
  - T3 fix: skip when a later trusted builder `ready` exists.
  
  In each case, the state write still happens and the job is not queued. The outcome is `answered-<action>`, and the log says `skipped <action>: answered by <comment id>`. The thread is the record of what already happened, so a replay rebuilds state without rebuilding work.
- [x] A PR existing is not evidence of a build: the feeder opens the PR before anything is built (see this PR's own start commit). The check reads markers only. It works for GitHub tickets and Linear tickets alike, because `TrackedTicket.comments` already merges a Linear ticket's comments with its linked PR's.
- [x] With an intact DB the check is a no-op: comments are handled as they arrive, so no later answer exists yet. It changes behaviour only on a replay (DB lost, or restored from a backup up to one cycle old), or when the poller was down while both comments landed. In both cases skipping is right.

**Verify.** Run `env -u ORCH_DB pytest tests/test_dispatcher.py -k "answered or replay or later_trusted"`.

- [x] `test_go_with_a_later_trusted_ready_queues_no_build`.
- [x] `test_fresh_db_replay_queues_no_build_for_a_finished_ticket`: an empty DB polls a thread of go, ready, APPROVE and handoff (AUT-2284 and AUT-2286). There are zero build jobs, zero review jobs and one handoff, and the state ends `handed-off`. Revert the check and a build is queued.
- [x] `test_untrusted_later_ready_does_not_suppress_the_build`.
- [x] `test_ready_with_a_later_verdict_queues_no_review`, `test_revise_with_a_later_ready_queues_no_fix`.

### Unit 7. Back up every cycle and restore on `ensure` (AC 3, #66 fixes 2 and 3)

**Files.** New `lib/state_backup.py`, `lib/cli.py` (`orch poll`, new `orch backup-db` and `orch restore-db`), `deploy/poller-service.sh`, `deploy/install.sh` (one `mkdir -p` in the profile it writes), `tests/test_state_backup.py`, `tests/test_poller_service.py`, `tests/test_deploy.py`.

**Build.**

- [x] `state_backup.backup(db, dest)`: the sqlite3 online backup API (`source.backup(target)`) into `dest.tmp`, then fsync and `os.replace`. It is consistent under WAL and never blocks the poller for long. `default_backup_path()` is `$ORCH_DB_BACKUP`, else `~/.local/share/factory/backup/orch.db`. It refuses a backup path inside the DB's own directory, because that directory is what was lost.
- [x] `orch poll` backs up after every cycle (`after_poll`, chained before the dashboard push). A failure is one `poll_errors` row (`backup:<ErrorClass>`), and polling goes on.
- [x] `state_backup.restore(backup, db)` refuses when `db` exists. It runs `PRAGMA integrity_check` on the backup and copies it with the backup API into `db.tmp`, then renames it into place. `orch restore-db [--db] [--backup]` exits 0 and prints `restored <db> from <backup> (backup taken <UTC time>)`. It exits 3 when there is no backup, and 1 when the DB exists or the backup is corrupt. `orch backup-db` runs a backup by hand.
- [x] `poller-service.sh`: a new `ensure_db` runs before `launch` in `start` and `ensure`. It runs `mkdir -p "$STATE"`. If `${ORCH_DB:-$STATE/orch.db}` is missing, it runs `orch restore-db`. On exit 0 it logs `RESTORED orch.db from backup` to stderr and the service log. On exit 3 it runs `orch init-db` and logs `WARNING orch.db was missing and no backup exists; created an empty database (dedupe now relies on thread markers)`. On any other exit it doesn't start the poller.
- [x] The profile hook that `install.sh` writes runs `mkdir -p "$(dirname "$DB")"` before it redirects into that directory (#66 fix 3).

**You see.** Delete `orch.db`, then run `deploy/poller-service.sh ensure`. You get the `RESTORED` line, and `orch status` shows the same tickets as before.

**Verify.** Run `env -u ORCH_DB pytest tests/test_state_backup.py tests/test_poller_service.py tests/test_deploy.py`.

- [x] `test_poll_cycle_writes_a_backup`, `test_backup_failure_is_a_poll_error_not_a_crash`.
- [x] `test_restore_db_restores_a_missing_db`, `test_restore_db_refuses_an_existing_db`, `test_restore_db_rejects_a_corrupt_backup`.
- [x] `test_ensure_restores_a_missing_db_from_backup`, `test_ensure_inits_and_warns_with_no_backup` (a fake `orch` on PATH, as the existing poller-service tests do).
- [x] `test_profile_makes_the_state_dir_before_logging`.

### Unit 8. Close out #42 (AC 4)

- [x] **N1 (Linear budget).** It is already on main. For each fix bullet, cite the test that covers it: the `exclude` skip of tracked hits, the per-cycle `_linear_issue_cache` reuse, the 429 and `RATELIMITED` backoff, and SPEC §7's budget. Add a test only where a bullet has none.
- [x] **N5 (config validation).** It is already on main: `ci.yml` runs `check-config --orch orch.yml --allow-placeholders`, and the README says check-config fails until the install values are filled in. Cite both.
- [x] **N4 (GitHub budget).** The stopped-state cadence is Unit 3. Comment and CI reads already retry and back off through `_adapter_runner`. Add a GitHub request budget to SPEC §7: reads per tracked ticket per cycle, the hourly totals at 30 s for active tickets and at 300 s for stopped ones, and the 5,000/hour `gh` limit.
- [x] **Split out explicitly.** File one follow-up issue, "#42 leftovers", for:
  - the conditional (ETag) first comments page;
  - N6 (deferred GitHub issue discovery ticket; `poll_once` errors not counted toward `--once`'s exit);
  - #40-N1 (wrap `enqueue` in the project-bot guard fixture) and #40-N2;
  - #43 m-a..m-d;
  - a closed, unmerged PR leaving tracking;
  - a queued fix from a post-handoff go (Unit 5's known limit).
  
  Then comment on #42 with where each item went. The PR body gets `Closes #42` and `Closes #66` only after that comment exists.

### Unit 9. SPEC and model (AC 5)

**Files.** `docs/SPEC.md`, `model/Orchestrator.tla`, `model/*.cfg`, `model/chart.mmd`, `docs/state-chart.svg` if `model/check.sh` regenerates it.

- [x] §4.1 chart: add `HandedOff --> Review: trusted ready at a new head, within the effective cap` and `HandedOff --> Merged: PR merged`. Add a line saying a merge seen in any state moves the ticket to Merged and clears stuck, paused and needs-attention.
- [x] §4.2 changes:
  - T2 gets the handed-off branch and the `answered-review` skip.
  - T1 gets `answered-build`/`answered-plan` and `go-after-handoff`.
  - T3 gets `answered-fix`.
  - T4 says an already-merged PR is `merge-confirmed`.
  - A new row records the merge observed by the poll.
  - Add the stopped-state cadence sentence.
- [x] §4.3 gets two new invariants: merged is absorbing, and a job is queued only for an unanswered handoff.
- [x] §7 gets the GitHub budget and `ORCH_DB_BACKUP`. §9 gets state loss: backup, restore, init with a warning, marker check, and `orch job cancel`.
- [x] A decision-log row for each design call above, marked "builder's design, in review".
- [x] TLA+: add `ReenterAfterHandoff` (guarded on `round < cap`) and `MergeObserved` (any non-terminal state, plus handed-off, to merged). Remove `handed-off` from `Terminal`. `bash model/check.sh` must pass, and must still fail with T1 or T1P left out.

## Verify

- [x] **Unit:** `env -u ORCH_DB pytest` is green on the final head, and each named test fails when its unit is reverted. Spot-check Units 2, 4, 5 and 6 by reverting just the production change: `git stash push -m u<N>-revert -- lib/dispatcher.py`, run the named tests, then `git stash apply` that entry and drop it. Record the failing output.
- [x] **Replay:** one fixture-driven test (`tests/test_lifecycle_replay.py`) builds the real evidence threads from fixtures: #89 (APPROVE, handed-off, 4 readies and 4 verdicts after the handoff, then a hand merge) and AUT-2284 on an empty DB. It runs `poll_once` cycles and asserts the final `ticket_state`, the jobs and the wakes.
- [x] **Config and model:** the CI `check-config` steps and `bash model/check.sh` pass locally.
- [ ] **CI:** green on the ready head.

## Out of scope

- Raising the cap on a `handed-off` ticket, and a queued fix from a post-handoff go. Both go to the follow-up issue.
- Moving the state dir itself off `~/.local/state`. The backup path covers the loss, and #107 owns host layout.
- Anything in the runner files (`lib/job_run.py`, PR #100) or the self-heal timer (#107).

## Build notes (build job, 2026-11-13)

- **Base.** `origin/main` with #105 merged in as `1ea9230`. #105's final API matched the plan, so nothing changed.
- **Unit 6 deviation.** A step that's already answered isn't left unqueued. Its job is recorded and then cancelled in the same transaction (`answered-by:<comment id>`). T2's trust check (`_job_builder_harness`) and T4's head check (`_latest_review_head`) read those job rows. Without them, a replayed ready would be rejected, and a replayed APPROVE would be stale. The AUT-2284 replay therefore shows one cancelled build and one cancelled review, not zero rows.
- **Merged is absorbing at dispatch too.** On a merged or done ticket, any comment except a ship replay is handled as `after-merge`.
- **Attention on handed-off tickets.** `orch status` and the dashboard used to hide needs-attention flags on handed-off tickets. They now show them, so a `ready-after-cap` after handoff is visible.
- **Revert checks.** These ran on a throwaway copy with each unit's production change mutated out, not with `git stash` (the stash stack is shared). Results: Unit 2 (2 failing), Unit 3 (2), Unit 4 (2), Unit 5 (3), Unit 6 (3). All pass restored.
- **Unit 8.** Follow-up #118. #42 note: https://github.com/maxharris1/factory/issues/42#issuecomment-6861825907
- **Not done.** `docs/state-chart.svg` wasn't re-rendered, because `model/check.sh` doesn't produce it. `model/chart.mmd` is regenerated and checked.
