# Plan for #109, test-gap sweep of the early-phase review follow-ups

Seventeen follow-up issues (#10 to #37) hold about 110 small items from the Phase 1 reviews. Most are missing tests or surviving mutants. A few are small fixes. Some are already fixed or obsolete. They clutter the backlog, and nobody can tell which still matter. This plan clears all of them in one PR (#114, branch `factory/issue-109`). For each item, the PR either adds a test that fails on revert, adds a fix plus its test, or states why the item is obsolete or already fixed.

Every item below was checked against `main` at 26e2266 before this plan was written. The checks read the current code and ran about 60 mutants and probes in throwaway copies of the repo. The status column records what was found. Line numbers are current as of 26e2266.

## How to read this

One unit is one commit that ends green under `env -u ORCH_DB pytest`, run in the foreground. Every unit names its tests. Check a box only when its evidence exists, a test run, a mutant result or a SHA.

This is a factory plan job. The build runs as the factory build job for #109 under `skills/pstack/skills/poteto-mode/playbooks/feature.md`. There is one PR. Codex Bot reviews it (risk high, up to three rounds). Grok Bot merges it on `ship: handoff`. The poteto multi-phase skeleton's cloud lanes do not apply to a single backend PR. Its verification rule does, in **Verify** below.

## Sequencing and dependencies

The ticket says to run after #105 and #107 merge. Both are still open, as PR #110 and PR #111. The units split into three phases so the build can start now without colliding with them.

- **Phase A, no dependency.** Units 1 to 8. They touch `lib/pstack_pin.py`, `lib/usage.py`, `lib/factory_md.py`, `lib/orchestrate.py`, `grok/orchestrate-project/SKILL.md`, `lib/eval_report.py`, `lib/queue.py`, `lib/wait.py`, `lib/github_actions.py`, README and tests. None of these files is in #110 or #111, except that #111 adds `overlay=` to `load_orch_yml`, which Unit 6 picks up in Phase B.
- **Phase B, after PR #110 merges.** Units 9 to 13. They touch `lib/dispatcher.py`, `lib/hook_t8.py`, `lib/poll_source.py` and `model/Orchestrator.tla`, which #110 rewrites. Bring `main` in with a merge commit first, never a rebase.
- **Phase C, after PR #111 merges.** Units 14 and 15. They touch `lib/status.py`, `lib/cli.py` USAGE and the poll_errors write, which #111 changes. Merge `main` in first.

If #110 or #111 is still open when Phase A is done, push Phase A and stop with a `ready` only after all phases are in. A partial PR does not get a `ready`.

## The data shape

The sweep's one data structure is the **item ledger** in Appendix A. Each row is one follow-up item, keyed `#<issue> <item id>`. It records the status found (APPLIES, ALREADY-FIXED, OBSOLETE, NOTE), the unit that handles it, and the test that fails on revert or the reason no test applies. The ready comment and the PR body carry the same ledger with the revert results filled in. The ticket closes 17 issues, and the ledger is how a reviewer checks that every item in each of them was handled.

## Criterion-to-work map

The ticket has one acceptance criterion, applied to every item.

| # | Acceptance criterion | Work that meets it | Evidence |
| --- | --- | --- | --- |
| 1 | For each item, add the missing test or fix (tests that fail on revert), or state in the PR why it's obsolete. | Units 1 to 15 handle every APPLIES item with a named test, plus a fix where the item is a defect. Unit 16 writes the ledger into the PR body, with the reason for every ALREADY-FIXED, OBSOLETE and NOTE row. | Appendix A lists every item with its unit and test. The **Verify** revert check records, for each new test, the revert or mutant that makes it fail. |
| 1a | Ticket note: #13 F1.5 (atomic next-seq) is obsolete. | Ledger row with evidence: `lib/dispatcher_queue.py` is gone (`tests/test_wait.py:611` pins that), and ordering is `claim_order()` over SQLite (`lib/queue.py:549-574`). | Appendix A |
| 1b | Ticket note: #29 FU-2 (wire the Shipper) is done. FU-3 stays a Phase 2 note. | Ledger rows. `lib/cli.py:664` sets `t8.shipper`. FU-3 is recorded as a note. | Appendix A |
| 1c | Ticket note: #32 "no real GitHub adapter yet" is obsolete. Keep the poll_errors escalation, cross-referenced with #107. | Ledger row for the adapter (`lib/github_actions.py` `GitHubReviewActions`). Unit 15 adds the escalation after #111. | Appendix A, Unit 15 |
| 1d | Ticket note: #35 FU-2 (issue-to-PR mapping) and "nothing constructs a Dispatcher" are obsolete. | Ledger rows. `lib/feeder.py:559-584` maps the issue to its PR. `lib/cli.py:645` builds the Dispatcher. | Appendix A |
| 1e | Ticket note: #11 F1.3 and F1.4 match #105's recorded extra rounds. | Unit 6 reads an authorized extra round as the `capped` to `fixing` transition, which under #105 only the trusted go can cause. | Unit 6 tests |
| 1f | Ticket note: #23 FU-2 and FU-3 checked against #99's stuck handling. | Both were reproduced on 26e2266, after #99. #99 did not change `_ensure_stuck_wake_in_transaction`. Unit 10 fixes both. | Unit 10 tests |
| 1g | Ticket note: #37 m4 and m6 coordinate with #107's status changes. | Units 14 and 15 run in Phase C, after #111 merges. | Phase C |

Every criterion is mapped.

## Work units

### Unit 1. Canary prompt and token (#21)

**Files.** `lib/pstack_pin.py`, `tests/test_pstack_pin.py`.

- [x] #21.1. Change the prompt at `lib/pstack_pin.py:211` to "exactly as written but without the ** markers, on one line". Keep the strict check in `_score_responses`.
- [x] #21.2. `_canary_token` (`:485-495`) stops using `_BOLD.search`. It finds the first `**` and the next `**`, and raises `ValueError("pinned poteto-mode first bold passage is not on one line")` when there is no closer or the span holds a newline. Today a wrapped passage silently returns the text between two passages.

**Tests.**

- [x] Extend `test_canary_asks_for_poteto_mode_and_a_token_from_the_skill` with `"without the ** markers" in prompt`.
- [x] `test_doctor_fails_when_reply_keeps_the_bold_markers`. A reply of `token: **<token>**` gets rc 1. This pins the strict side.
- [x] `test_bump_pin_refuses_a_wrapped_first_bold_passage`. `**token\none** then **two**` in `orchestrate.md` makes `bump_pin` raise and `verify_pin` report it.

### Unit 2. Install swap, crash recovery and rollback (#14 FU-8, FU-9, FU-10)

**Files.** `lib/pstack_pin.py` `_swap_in` and the `install_skills` preflight, `tests/test_pstack_pin.py`.

- [x] FU-8. `os.replace` only when the staged path and the dest are both non-directories. Otherwise take the backup and rename path. Today a cloud install over an owned local symlink fails with ENOTDIR.
- [x] FU-9. Put the backup under the staging root (`<stage_root>/backup/<name>`), two levels deep, where no harness loads it. In the preflight, for each skills dir, rename a leftover `.orch-backup-<name>-*` back when the dest is missing, and discard owned `.orch-backup-*` and `.orch-staging-*` leftovers otherwise. Match the exact prefixes only.
- [x] FU-10. Rollback gets a real test.

**Tests.**

- [x] `test_cloud_install_replaces_owned_local_links`. Local install, then cloud install returns `[]`, then `doctor(..., "cloud")` gives rc 0.
- [x] `test_crash_between_renames_is_recovered_on_next_install`. Patch `lib.pstack_pin.os.rename` to raise `KeyboardInterrupt` on the staged rename, re-install, and assert `[]` and no `.orch-*` entries.
- [x] `test_failed_publish_rolls_back_the_old_copy`. Patch the staged rename to raise `OSError("publish failed")`. The old `SKILL.md` survives and no `.orch-*` entry is left. The audit found that deleting the rollback `os.rename(backup, dest)` keeps all 26 current tests passing, and this test kills that mutant.

### Unit 3. Ownership and doctor (#14 FU-5, FU-6; #10 FU-1, FU-3, FU-4)

**Files.** `lib/pstack_pin.py`, `lib/cli.py` `_install_skills`, `tests/test_pstack_pin.py`.

- [x] FU-5. `_write_marker` records each file's sha256. `_installer_owns` only owns a dir whose files, minus the marker, exactly match the list. An edited copy is refused as unowned, and the user's file survives. A legacy two-line marker counts as owned only when the contents match the current lock.
- [x] FU-6. In cloud mode, `_check_dest` rejects a symlink at any path component from the project anchor down to the dest, including `.claude`, `.agents` and `.cursor` themselves.
- [x] #10 FU-1. `_score_responses` requires exactly one `started-by:`, `skill:` and `token:` line, each with the expected value. Other prose lines stay allowed, so a chatty harness doesn't fail.
- [x] #10 FU-3, in two parts. In `_symlink_into_pstack`, a dangling link whose target ends in `skills/pstack/skills/<name>` counts as owned and is replaced, since replacing a symlink loses no data. And `orch install-skills` in local mode refuses when the checkout is a linked git worktree (`.git` is a file) or under the temp dir, with "install from a stable checkout". The check goes in the CLI wrapper, so `install_skills` tests that install from `tmp_path` keep working.
- [x] #10 FU-4. The cloud install test compares every pinned skill file, not two.

**Tests.**

- [x] `test_install_refuses_an_edited_cloud_copy` and `test_install_refuses_a_forged_marker`.
- [x] `test_doctor_cloud_rejects_a_symlinked_harness_dir`.
- [x] `test_doctor_fails_when_a_reply_repeats_or_conflicts_a_key`, parametrized over the three keys, both a conflicting and an identical duplicate line.
- [x] `test_install_replaces_a_dangling_owned_link` and `test_cli_local_install_refuses_a_worktree_checkout`.
- [x] `test_install_vendors_each_skill_into_the_cloud_project_paths` loops over `load_pin(ROOT).hashes()` and asserts the copied file set is exactly the pinned set plus each `.orch-owned`.

### Unit 4. Model family, routing and the go comment (#15, #17, #19.1 to #19.3, #19 FU1, FU5, FU6)

**Files.** `lib/usage.py`, `lib/factory_md.py`, `lib/orchestrate.py`, `examples/tracy/factory.md`, `grok/orchestrate-project/SKILL.md` line 32, README line 1424, `tests/test_usage.py`, `tests/test_factory_md.py`, `tests/test_orchestrate.py`.

- [x] #15.1. `fit_table` raises `ValueError` for a preferred harness not in `HARNESSES` or a `None` family. A hand-built config that check-config would refuse no longer routes silently.
- [x] #15.2. Test only. `UsageReport` rejects NaN.
- [x] #15.3. Rename `_resolved_family` and `_family_findings` to `resolved_family` and `family_findings`. Update the imports in `lib/usage.py` and `lib/orchestrate.py`.
- [x] #15 built_by. `route_job(..., built_by=(harness, model))` builds the builder assignment from what actually built, and picks the reviewer outside that family. It raises on an unknown harness or a `None` family. `RouteDecision` gets an optional `built_by` field.
- [x] #17.1. The family word needs a boundary. `^(?:claude|sonnet|opus|haiku)(?=$|[-._:0-9])` and `^(?:gpt|codex)(?=$|[-._:0-9])`, plus a deny-prefix list `opus-mt`, `gpt-j`, `gpt-neo` that returns `None` and fails closed. That covers the issue's own examples, which the boundary alone does not. `^o[0-9]+(?=$|[-._:])` closes `o3x`.
- [x] #17.3. Test only. `tests/test_factory_md.py:181` and `:205` assert the full family message. The cursor case asserts the two "not established" messages instead.
- [x] #17 tracy. `examples/tracy/factory.md:21` says `ok head=<sha>`.
- [x] #19.1. `choose_route` calls `assert_cross_family` on the config pair before `route_job`. Rename the test at `tests/test_orchestrate.py:324` so its name matches what it asserts (a refusal).
- [x] #19.2. `RouteDecision` gets `builder_using_reserve`, `reviewer_using_reserve`, `builder_fallback` and `reviewer_fallback`. `choose_route` sets them. `_go_body` renders a **Routing** paragraph only when one is true, so the exact-body test stays as it is. `SKILL.md:32` lists the new fields. The go header keys do not change.
- [x] #19.3. One `route_job` call, and the unreachable guard at `orchestrate.py:337` goes.
- [x] #19 FU1. `go_comment` rechecks the family pair with `assert_cross_family`, checks `job == job_for_file_count(...)`, and checks that the reviewer harness is known.
- [x] #19 FU5. `RouteRequest` refuses a `str` for `likely_files`.
- [x] #19 FU6. A question stays one line. An answer may span lines (stripped, not empty). `parse_comment` reads only the first line, so the header is safe.

**Tests.**

- [x] `test_route_refuses_a_preference_check_config_refuses`, parametrized over an unknown harness, cursor with no hint, and a cursor reviewer with `gemini-3`, for both `fit_table` and `route_job`.
- [x] `test_usage_report_rejects_a_bad_fraction` gains the NaN case.
- [x] `test_lib_modules_import_no_private_factory_md_names`, an AST check over `lib/usage.py` and `lib/orchestrate.py`.
- [x] `test_later_round_reviewer_excludes_the_family_that_built`.
- [x] `test_family_word_needs_a_boundary`, parametrized over `xsonnet`, `magnum-opus`, `opusmt`, `haikus`, `claudette`, `opus-mt-en-de`, `codexes` and `gpt-j`. `xsonnet` and `magnum-opus` kill the substring mutant. `opusmt` and `haikus` kill a revert of the boundary.
- [x] The tracy phrase list gains `ok head=<sha>`.
- [x] `test_same_family_reviewer_is_refused` asserts `RouteError` with the family message, plus cases for a cursor builder with no hint and claude with a `gpt-5` hint.
- [x] `test_choose_route_records_reserve_and_fallback` covers non-urgent fallback, urgent reserve, and the all-false case with no **Routing** paragraph.
- [x] `test_go_comment_rechecks_family_and_job`, `test_route_request_refuses_a_string_for_likely_files`, `test_multi_line_answer_round_trips`.

**Note on #19.2.** The dispatcher runs factory.md's pair, not the go's (SPEC section 4.2, T1). The new fields therefore report a fallback the dispatcher does not act on. The **Routing** paragraph says "for information; the dispatcher runs factory.md's pair". Making T1 honour the go's `builder=` is a protocol change, outside a test sweep.

### Unit 5. Grok orchestrate skill text (#19 FU2, FU3, FU7)

**Files.** `grok/orchestrate-project/SKILL.md`, its phrase test in `tests/test_orchestrate.py`.

- [x] FU2. Before `post_go`, the skill reads the ticket's comments and does not post when a trusted `grok-bot` `kind=go` for the same action already exists.
- [x] FU3. Replace "set `scope_changed` on any later RouteRequest" at `:165` with the actual step. A scope-changing answer gets one new `choose_route` with `scope_changed=True` and one go with `job=plan`. A `plan` action has its own T1 dedupe key, so T1 queues it.
- [x] FU7. `:22` and `:70` resolve `factory.md` the way the poller does, from `orch.yml` `projects[].factory_ref`, and refuse on a mismatch (`lib/poll_source.py:985-994`).

**Tests.**

- [x] `test_orchestrate_skill_names_each_step`, with phrases for each of the three steps (`factory_ref`, `scope_changed=True` with `job=plan`, the existing-go check).

### Unit 6. Eval report (#11)

**Files.** `lib/eval_report.py`, `tests/test_eval_report.py`.

- [x] F1.1. Run the `_inside` containment check on `orch.yml`, `dispatcher.log` and `model-findings.txt`, as comments already get. In Phase B, after #111 adds overlays, call `load_orch_yml(..., overlay=False)` so `orch.local.yml` is never read from the input dir.
- [x] F1.2 and the open enter. Measure dwell over every event, then clip each dwell to `[since, until]`. Count wakes, verdicts and stuck rows only when the event is in the window. Keep handled and entered history from before the window, so a repeat across `--since` is seen. With `--until`, an open `enter` is measured to it. Without `--until`, the state is listed under `open states`.
- [x] F1.3. Fail closed. Take risk from the latest `kind=go` with `risk=` in the run, then from the verdict. Report `cap-unverified` when neither has one, and `risk-mismatch` when they disagree. A verdict with no `round` takes its 1-based position among the run's verdicts.
- [x] F1.4. The rounds allowed at a verdict are the config cap plus the `capped` to `fixing` transitions before it. Under #105 only the trusted go makes that transition. A round inside that allowance is reported as `round-extra-authorized`. A round above it is still `round-over-cap`. The denominator in the verdict header is not trusted, since the reviewer writes it.
- [x] F1.5. Rows take a required `ticket=<id>` (Linear ids allowed) and an optional `pr=`. Runs are keyed by `(repo, ticket)`. `model-findings.txt` uses `ticket`. The two snapshot fixtures change.
- [x] F1.7. `_parse_time` turns a `ValueError` into `EvalError("bad timestamp ...")`, so an impossible date prints a message and exits 1.
- [x] model-findings. A missing file renders `none supplied`, distinct from an empty file. The docstring drops "F1-13 must write this format". The model checks the spec, not runs, so nothing produces run findings. The ledger row says so.

**Tests.**

- [x] `test_top_level_input_symlinked_outside_is_refused`, parametrized over the three files. The error never contains the target's text.
- [x] `test_window_clips_state_straddling_both_ends`, `test_window_clips_state_straddling_since`, `test_repeat_handled_across_since_is_wasted`, `test_transition_across_since_is_checked`.
- [x] Replace `test_verdict_without_risk_skips_the_round_cap` with `test_verdict_without_risk_is_cap_unverified`. Add `test_unnumbered_verdicts_take_risk_from_go`.
- [x] `test_extra_round_after_capped_is_authorized` and `test_round_two_without_capped_is_over_cap`. The second includes a review to fixing step, which kills a mutant that counts any fixing entry.
- [x] `test_ticket_without_pr_is_reported_under_ticket_id`.
- [x] F1.6 gaps. `test_wasted_flag_alone_counts`, `test_reason_nobody_alone_counts`, `test_high_risk_round_over_cap`, `test_rounds_fall_back_to_verdict_count`. The audit ran each of the four mutants the issue names, and all 23 current tests passed against each one.
- [x] `test_impossible_date_is_bad_timestamp`, through the CLI, over the log, `--since`, `--until` and `model-findings.txt`.
- [x] Rewrite `test_open_state_without_end_adds_no_seconds` to check both the `--until` and the `open states` cases.
- [x] `test_missing_model_findings_is_none_supplied`.

### Unit 7. Queue and wait (#13, #27, #28)

**Files.** `lib/queue.py`, `lib/wait.py`, `README.md` queue section, `tests/test_queue.py`, `tests/test_wait.py`, `tests/test_cli.py`.

- [x] #27 L1. Test only. A second `finish` with a different handoff id is refused, and the first id stays.
- [x] #27 L2. Test only. `heartbeat` refuses the wrong owner, and `last_activity_at` does not move.
- [x] #27 L3. Test only. `release`, `heartbeat` and `finish` refuse an owner not in `RECIPIENTS`.
- [x] #27 N1. Add a comment to `_InterleavingConnection`. Add a threaded backstop that races `finish` against `mark_timed_out` plus `reconcile_jobs` with a `threading.Barrier` for about 50 rounds. It must never see `done` beside an attempt-2 retry.
- [x] #27 I1. Test only. `--failed` after a T6 timeout exits 0 with "already recorded" and leaves exactly one attempt-2 row. That is the intended behaviour, pinned.
- [x] #27 F3. A one-line comment in `_t6` saying it relies on the single-poller flock in `_poll_lock`. Done in Unit 9 with the other dispatcher edits.
- [x] #27 F4. README "Queue interface" documents `owner=` on `release`, `heartbeat` and `finish`, the refusals, and the idempotent repeat `finish`. It also corrects "claim takes the oldest" to the #85 priority order.
- [x] #28 F3.1. `wait_for_job` saves the caller's signal mask and handlers and restores them on leave. It sets `SIG_IGN` while signals are still blocked, which drops pending signals, then restores the mask, then the handlers. A `keep_ignored` flag keeps today's CLI behaviour (exit 0 after delivery).
- [x] #28 F3.2. Docs only. The `wait.py` docstring and README say a signal may wait up to the 5-second busy timeout or until stdout drains, and that SIGKILL always works. The 30-second delay in the issue was cut to 5 in d5f65be.
- [x] #28 F3.3. `open_queue` renders the path with `!r`, so a newline in the path keeps the error on one line.
- [x] #28 F3.4. The two-waiter test forces overlap. It holds the write lock, enqueues inside it, waits about 2 polls so both waiters sit in the busy handler, then commits.
- [x] #28 M.1. Parametrize the broken-stdout delivery test over claude-bot, codex-bot and cursor-bot, and assert no `device_slots` row is left.
- [x] #18 claim rowcount. `claim` raises `QueueError` when the update's rowcount is not 1. It is a defence; no mutant can show it today.
- [x] #18 queued compare-and-set. Test only.

**Tests.**

- [x] `test_finish_with_a_different_handoff_is_refused`, `test_heartbeat_refuses_the_wrong_owner`, `test_queue_ops_refuse_an_unknown_owner` (parametrized), `test_finish_races_t6_timeout_threaded`, `test_failed_after_a_t6_timeout_is_a_no_op`.
- [x] `test_wait_for_job_restores_the_callers_mask_and_handlers`, run without the `restore_signals` fixture. `test_a_signal_during_cli_delivery_exits_zero` stays green.
- [x] `test_a_db_path_with_a_newline_is_one_line`.
- [x] `test_two_waiters_one_job_exactly_one_claims_it` forces the overlap.
- [x] The broken-stdout test is parametrized over three bots. This kills a hard-coded `owner="claude"` in `deliver()`, and a deleted `pool.release(...)`. Both survive today.
- [x] `test_queued_timeout_fails_closed_after_a_same_tick_claim`. It kills dropping `state='queued'` from the compare-and-set at `queue.py:410`.

### Unit 8. GitHub CI status and adapter notes (#35)

**Files.** `lib/github_actions.py` (docstring only), `tests/test_github_actions.py`, `tests/fixtures/github/`, README.

- [x] R2-M1. Test only. Four new `ci_status` cases: a failing check run with a success combined status gives failure; a success run with a non-empty pending status gives pending; pages whose `total_count` values disagree give pending; more runs than `total_count` give pending. New fixtures are `status-pending.json` (count 1) and two page-count fixtures.
- [x] Short-read nit. Document in `ci_status` why a short read with a failing run reports pending. A rerun on an unread page can supersede the visible failure, so pending is the safe answer. The cost is one poll.
- [x] FU-1. Already fixed for the per-ticket try. Add the missing pins. A source whose `github_candidates` or `tracked_tickets` raises writes a `poll_errors` row, and `reconcile_jobs` still runs.
- [x] FU-3. Add sanitized real `gh api` transcripts for check-runs pagination and a 403, 409 and 422 error, captured with `env -u GH_TOKEN gh` from this repo. Ids and URLs are kept. Tokens, emails and node ids are scrubbed. One test runs `ci_status` and the error mapping over them.
- [x] Branch protection. README states that a target repo's branch protection can reject the fast-forward ref update, and that this fails closed as ship-failed.
- [x] UnavailableGrokDelivery (#37 carry-over). A comment at `tests/test_github_actions.py:955` explains why the fixture must fail.

**Tests.**

- [x] The four `ci_status` cases. Two mutants survive today ("clear failed on status success" and "pending status counts as not pending"), and these cases kill them. The two page-count cases are expected to kill "keep last count only" and "reject only shortages". The revert check confirms that.
- [x] `test_poll_records_an_error_when_listing_candidates_raises`, parametrized over the two calls.
- [x] `test_ci_status_over_recorded_gh_transcripts`.

### Unit 9. Dispatcher pins and trust tests (#18, #23 I-1, FU-1, FU-4) (Phase B)

**Files.** `lib/dispatcher.py`, `lib/poll_source.py`, README, `tests/test_dispatcher.py`, `tests/test_queue.py`, `tests/test_cli.py`.

- [x] #18 T1 `from` and `role`. Test only. Both mutants survive today.
- [x] #18 login equals self-asserted `from`. Test only. The mutant `or comment.author == fields["from"]` in `_trusted` survives today.
- [x] #18 Q1 tracker filter. Test only. The mutant at `dispatcher.py:3256` survives today.
- [x] #18 7-character minimum head. Test only, re-pinned against #110's uncached head re-read.
- [x] #18 short `current_head`. `poll_source._github_ticket` and the Linear path pass the head through `_sha()`, so a short head fails at the source.
- [x] #18 failed stuck job retried. Test only. A failed stuck job gets no attempt-2 stuck job.
- [x] #18 db without dispatcher tables. Test only. `orch poll --once` on a valid empty SQLite file exits 1 with "no jobs table". The rest of this item was fixed by `open_queue`.
- [x] #18 README heartbeat. README states that a heartbeat error stops the heartbeat and does not abort the command, and that T6 retries the job. Making `job_run` kill the command touches every run and is not a test gap.
- [x] #18 T6 retries old heads. Re-check after #110. #105 AC6 adds the `done-from-ready` guard for fix and build jobs. If a gap is left (a failed fix whose head moved with no ready), add the test. Otherwise the ledger row cites #110's tests.
- [x] #23 I-1. `test_dispatcher.py:2293` also asserts no job for `go-after-stuck` and the ledger outcome `ticket-stuck`.
- [x] #23 FU-1. `_t1` returns `deduped-{action}` instead of `queued-{action}` when the returned job came from another comment. Check against #110's go-keyed resume fix first.
- [x] #23 FU-4. Tests for `enqueue_retry`'s `handled != 1` guard, its `BEGIN IMMEDIATE`, and the `stuck_wake_job_id` migration on an old schema. All three survive today.
- [x] #27 F3 comment in `_t6`.

**Tests.**

- [x] `test_t1_rejects_a_go_whose_from_or_role_is_wrong` (parametrized), `test_t2_refuses_wrong_sender_and_spoofed_from_field` gains the `claude-bot` author case, `test_discover_ignores_a_ticket_from_the_wrong_tracker`, `test_t2_ignores_a_six_char_ready_head`, `test_live_source_refuses_a_short_head`, `test_failed_stuck_job_is_not_retried`, `test_poll_refuses_a_sqlite_file_without_a_jobs_table`, `test_t1_same_action_twice_is_deduped`, `test_enqueue_retry_refuses_an_already_handled_failure`, `test_enqueue_retry_runs_in_one_write_transaction`, `test_stuck_wake_column_migrates_an_old_schema`.

### Unit 10. Stuck wake limit and recovery (#23 FU-2, FU-3; #29 FU-1) (Phase B)

**Files.** `lib/dispatcher.py` `_ensure_stuck_wake_in_transaction`, `tests/test_dispatcher.py`.

- [x] FU-2 and #29 FU-1. Cap regeneration at 2 failed stuck wakes per stuck episode, then `flag_attention("stuck-wake-failing")` and stop. The audit reproduced the bug on 26e2266: 29 failed stuck rows over 29 simulated hours. T7 stuck and the third decline both go through `_mark_stuck_and_wake`, so one fix covers them.
- [x] FU-3. A missing `stuck_wake_job_id` row is treated like a failed wake, so it is regenerated, within the same cap.

**Tests.**

- [x] `test_failing_stuck_wake_stops_after_two_and_flags_attention`. `RaisingGrokDelivery`, three reds, polls across five timeout windows. At most two stuck rows plus the attention row.
- [x] `test_missing_stuck_wake_row_is_regenerated`. Delete the stuck row and reconcile. One new stuck job.

### Unit 11. Shipper, T4 and the model (#32, #29 N2 and nit, #20) (Phase B)

**Files.** `lib/dispatcher.py`, `lib/hook_t8.py`, `model/Orchestrator.tla`, `model/check.sh`, `model/chart.mmd`, `model/view.mjs`, the `.cfg` files, `docs/SPEC.md` sections 4.1 and 4.3, `tests/test_hooks.py`, `tests/test_eval_report.py`, `tests/test_statechart_check.py`.

- [x] #32 FU-2. Each `ALTER` in `_initialize` runs inside `BEGIN IMMEDIATE` and re-reads `PRAGMA table_info` first, the way `_migrate_ship_intent_redrive` does.
- [x] #32 FU-3. `_replay_ship_intent` writes `merged` instead of `waiting-external` when the after-intent is complete and its external ship intent is settled.
- [x] #32 FU-4. Test only. An older verdict edited from APPROVE to REVISE after a newer APPROVE. Preview and ship use the newer verdict.
- [x] #32 n3. The head-moved gate case gives HEAD_B green CI as well, so the test pins the head check and not the CI check.
- [x] #32 n4. The `ReviewShipper` docstring says "3 attempts, not 3 polls". The M1 test asserts the ship-failed wake after poll 2.
- [x] #32 approve-ci-red. `_t4` wakes Grok once (`blocked`, with the head) when an APPROVE lands on red CI, so the ticket no longer stalls in silence.
- [x] #29 N2. Ledger states it is accepted. The single-poller flock (`lib/cli.py:731`) rules out the two-dispatcher race in practice, and `test_t8_two_dispatchers_cannot_both_ship` pins one wake.
- [x] #29 nit. Rewrap `lib/hook_t8.py:26` and the long lines at 139 and 156.
- [x] #20.1. Add `Fixing --> Fixing: CI red, fix` to section 4.1 and `CHART_TRANSITIONS`. `check.sh` injects the fixing self-loop as well as the building one, with a comment that the injected loops are checked against the spec, not the model. Section 4.1 says T6 retry self-loops are not drawn. Regenerate `chart.mmd`.
- [x] #20.2. The chart tests fail on any `-->` line the regex does not match, other than `[*]` edges, and on duplicate pairs in `chart.mmd`.
- [x] #20.3. Reword the post-merge comment at `Orchestrator.tla:23`, with the T9 TODO.
- [x] #20 r1 FU1. `T3Revise` and `T8Decline` clear `covered`. A `HeadMoves` step in ship and awaiting-ok clears it too. A fixture constant drops one guard, and `check.sh` expects `## ShipPinnedToCoveredHead breaks`. Keep the chart slice under the 500-state limit with #110's `extra` counter.
- [x] #20 r1 FU2. Section 4.3 states that no liveness property is checked and that the capped to fixing to review loop and the ship to review loop are unbounded by design.
- [x] #20 r1 FU3. Delete `OrchDoesNotExec` and the `HarnessExec` constant from the module and all six cfgs. `tests/test_waiter.py:918` already pins the no-exec rule.
- [x] #20 r1 FU4. The `check.sh` greps at `:191` and `:197` match the last state line of the named break section, not any line containing the word.
- [x] #20 r1 FU5. Delete `KindGo` and its two guards. Delete the unreachable AC-unmet label branch in `view.mjs`, since `ChartSlice` keeps `acUnmet` at 0.

**Tests.**

- [x] `test_initialize_migrations_are_race_safe` (two threads on an old schema), `test_t4_replay_after_settle_keeps_merged`, `test_edited_older_verdict_does_not_override_a_newer_approve`, `test_approve_on_red_ci_wakes_grok_once`.
- [x] `test_t2_self_loops_are_charted` parses the `T2CiRed` state set from the `.tla` file.
- [x] `test_chart_edges_fail_on_an_unparsed_line`.
- [x] `test_check_sh_fails_when_the_break_trace_ends_elsewhere`, with a fake `STATECHART` whose trace mentions "questions" but ends in another state.
- [x] `bash model/check.sh` passes, including the new `ShipPinnedToCoveredHead breaks` fixture.

### Unit 12. Handoff integrity and the queued timeout (#18, #19 FU4) (Phase B)

**Files.** `lib/dispatcher.py`, `lib/poll_source.py` (comment `updated_at`), `docs/SPEC.md`, `tests/test_dispatcher.py`.

- [x] #18 `finish(handoff_comment_id)` verified. On the next poll, for each job finished `done` with a handoff id, the dispatcher checks that the comment exists on that ticket, carries an `orch:v1` header, and is from a login trusted for the job's recipient. Otherwise it flags `unverified-handoff` attention on the ticket. The job state stays `done`. This is trust-relevant, so it fails toward a person looking, not toward a silent retry.
- [x] #18 edited comments. `Comment` gets `updated_at` from the live source. An unhandled `orch:v1` comment edited after it was posted is handled as `rejected-edited` and flags attention. A comment already handled is never re-read, as today.
- [x] #18 queued timeout counts backlog. A queued job does not time out while its recipient has no free slot, so a busy bot's backlog no longer marks tickets stuck. A queued job whose recipient has a free slot times out as today.
- [x] #19 FU4. A trusted `grok-bot` `kind=answers state=abandoned` comment sets `workflow_state='abandoned'`. The existing readers (`dispatcher.py:3346`, `lib/linear_webhook.py:229`) already stop work on that state. The Grok skill's line 183 changes so that abandoned becomes a comment, not a chat-only report.

**Tests.**

- [x] `test_done_with_an_untrusted_handoff_flags_attention`, `test_done_with_a_missing_handoff_flags_attention`, `test_done_with_a_trusted_handoff_is_quiet`.
- [x] `test_edited_orch_comment_is_rejected`.
- [x] `test_queued_job_waits_without_timing_out_while_the_bot_is_busy` and `test_queued_job_times_out_when_a_slot_is_free`.
- [x] `test_answers_abandoned_from_grok_ends_the_ticket` and `test_answers_abandoned_from_an_untrusted_login_is_ignored`.

### Unit 13. Phase B leftovers from Phase A

- [ ] Unit 6 F1.1. `load_orch_yml(..., overlay=False)` in eval_report once #111's overlay exists, and `orch.local.yml` joins the containment test. If #111 has not merged by Phase B, this moves to Phase C.

### Unit 14. Orch status (#37) (Phase C)

**Files.** `lib/status.py`, `lib/cli.py`, `bin/orch`, `tests/test_status.py`, `tests/test_cli.py`.

- [x] m1. Replace the tautological test with `test_status_connections_cannot_write`, on a rollback-journal DB and a WAL DB. It asserts `PRAGMA query_only` is 1 and that a write raises `sqlite3.OperationalError`.
- [x] m2. The subprocess read-only test snapshots recursively, directories included. The DB sits in a subdirectory so its parent is snapshotted. HOME and TMPDIR point inside `tmp_path`. A `sitecustomize` audit hook logs write-mode opens, `mkdir`, `remove`, `rename` and `sqlite3.connect` URIs. The test asserts no writes and `mode=ro` on every connect.
- [x] m3. Already fixed. Every reader closes in `finally`. N4's test pins it.
- [x] m4. `cli.main` routes `status`, USAGE lists it, and `bin/orch` calls only `cli_main`. Merge with #111's USAGE edits.
- [x] m5. `_rows` checks for `tracked_tickets`, `ticket_state` and `jobs`, and renders the empty state when they are missing.
- [x] m6. Natural sort key, `(0, int(t))` for digits, else `(1, t.casefold())`, in the running and paused keys.
- [x] N1. Comment on the priming read in `test_status_reads_uncheckpointed_wal_without_changing_files`. No `readonly_shm=1`.
- [x] N2 and N3. Accepted, with a comment at the `-wal` check in `status.py`. Ledger records both as harmless.
- [x] N4. `test_status_closes_every_connection_and_reads_rows_once`.

**Tests.**

- [x] The tests named above, plus `test_help_lists_status`, `test_status_on_a_queue_only_db_shows_the_empty_state`, `test_status_sorts_ticket_ids_naturally`.

### Unit 15. Poll error escalation (#32 carried note, with #107) (Phase C)

**Files.** `lib/dispatcher.py` (after the poll_errors write #111 changes), `tests/test_dispatcher.py`.

- [x] After 3 consecutive poll errors for one ticket, `flag_attention("poll-errors")` once. A clean poll for that ticket clears the count. #111's self-heal works per component, not per ticket, so this does not overlap.

**Tests.**

- [x] `test_repeated_poll_errors_flag_attention_once` and `test_a_clean_poll_resets_the_error_count`.

### Unit 16. Ledger and docs

- [x] SPEC. Decision-log row dated on the build day for #109. Section 4.1 and 4.3 edits from Unit 11. T1 and T5 rows for `abandoned` (Unit 12). The T6 note on the queued timeout. The trust note on handoff verification and edited comments.
- [x] PR body. Appendix A's ledger with the revert column filled in, and `Closes` lines for all 17 issues.
- [x] New follow-up issue, opened by the builder, for the three items Appendix B splits out.

## Verify

Tests alone are not sufficient verification. The PR is verified only when the unit, revert and CI boxes are all checked.

- [x] Unit. `env -u ORCH_DB pytest` passes at the PR head, run in the foreground.
- [x] Revert check. A scratch mutant runner, kept out of the PR, applies each item's revert or named mutant to a throwaway copy, runs the item's test, and records pass or fail. Every new test must fail on its revert. Paste the table into the ready comment. The audit already has a runner of this shape for 30 of the mutants.
- [x] Model. `bash model/check.sh` passes locally or in the `statechart` CI job.
- [ ] CI. `.github/workflows/ci.yml` is green at the head, covering `pytest`, the `check-config` steps, `eval-report` and `statechart`.
- [ ] Merges. `main` came in by merge commits only, after #110 and after #111.
- [x] Perf. None. Unit 12 adds one comment lookup per finished job per poll, from comments the poll already fetched.

## Close

- [ ] Every box above is checked with its evidence.
- [ ] Post the ready handoff with the pushed head, the ledger, the revert table and the CI link.

## Appendix A. Item ledger

Status is as found on 26e2266. "Pinned by" names the existing test for an ALREADY-FIXED row.

| Item | Status | Unit | Test or reason |
| --- | --- | --- | --- |
| #10 FU-1 doctor duplicate or conflicting lines | APPLIES | 3 | `test_doctor_fails_when_a_reply_repeats_or_conflicts_a_key` |
| #10 FU-3 dangling install links | APPLIES | 3 | `test_install_replaces_a_dangling_owned_link`, `test_cli_local_install_refuses_a_worktree_checkout` |
| #10 FU-4 cloud test compares 2 skills | APPLIES | 3 | the full-lock loop in the cloud install test |
| #11 F1.1 symlinked top-level inputs | APPLIES | 6, 13 | `test_top_level_input_symlinked_outside_is_refused` |
| #11 F1.2 window drops events | APPLIES | 6 | the four window tests |
| #11 F1.3 no risk or round skips cap | APPLIES | 6 | `test_verdict_without_risk_is_cap_unverified`, `test_unnumbered_verdicts_take_risk_from_go` |
| #11 F1.4 extra round reported as over cap | APPLIES | 6 | `test_extra_round_after_capped_is_authorized`, `test_round_two_without_capped_is_over_cap` |
| #11 F1.5 rows require `pr=` | APPLIES | 6 | `test_ticket_without_pr_is_reported_under_ticket_id` |
| #11 F1.6 four test gaps | APPLIES | 6 | the four F1.6 tests |
| #11 F1.7 impossible date | APPLIES | 6 | `test_impossible_date_is_bad_timestamp` |
| #11 open enter | APPLIES | 6 | the rewritten open-state test |
| #11 model-findings none supplied | APPLIES | 6 | `test_missing_model_findings_is_none_supplied`. The "F1-13 must write this format" half is obsolete, since the model checks the spec, not runs |
| #11 SPEC 4.1 Stuck edges | ALREADY-FIXED | none | Fixed by #12. SPEC.md:126, :135, :139 and `CHART_TRANSITIONS` |
| #13 F1.2 no-harness test | ALREADY-FIXED | none | Pinned by `test_wait_and_job_never_exec_a_harness` (`tests/test_wait.py:447`) |
| #13 F1.5 atomic next-seq | OBSOLETE | none | No next-seq counter. `tests/test_wait.py:611` pins that `lib/dispatcher_queue.py` is gone |
| #13 F1.7 two-process claim race | ALREADY-FIXED | 7 | Pinned by `test_two_waiters_one_job_exactly_one_claims_it` and `tests/test_device_pool.py:153`, `:177`. Unit 7 forces the overlap (#28 F3.4) |
| #14 FU-5 marker not bound to contents | APPLIES | 3 | `test_install_refuses_an_edited_cloud_copy`, `test_install_refuses_a_forged_marker` |
| #14 FU-6 symlinked harness dir | APPLIES | 3 | `test_doctor_cloud_rejects_a_symlinked_harness_dir` |
| #14 FU-7 "first bold sentence" | ALREADY-FIXED | 1 | Fixed by #16. Pinned by `test_doctor_fails_when_reply_quotes_only_first_sentence`. The remaining wording is #21.1 |
| #14 FU-8 ENOTDIR over owned links | APPLIES | 2 | `test_cloud_install_replaces_owned_local_links` |
| #14 FU-9 death between renames | APPLIES | 2 | `test_crash_between_renames_is_recovered_on_next_install` |
| #14 FU-10 rollback untested | APPLIES | 2 | `test_failed_publish_rolls_back_the_old_copy` |
| #15.1 fit_table guard | APPLIES | 4 | `test_route_refuses_a_preference_check_config_refuses` |
| #15.2 NaN test | APPLIES | 4 | NaN case in `test_usage_report_rejects_a_bad_fraction` |
| #15.3 public family helper | APPLIES | 4 | `test_lib_modules_import_no_private_factory_md_names` |
| #15 built_by on later rounds | APPLIES | 4 | `test_later_round_reviewer_excludes_the_family_that_built` |
| #17.1 family word boundary | APPLIES | 4 | `test_family_word_needs_a_boundary` |
| #17.2 negative tests | APPLIES | 4 | same test |
| #17.3 full family message | APPLIES | 4 | the rewritten asserts at `test_factory_md.py:181`, `:205` |
| #17.4 unreachable line | NOTE | none | Info only in the issue. It becomes reachable through #15.1's guard, so it stays |
| #17 tracy wording | APPLIES | 4 | the tracy phrase list |
| #18 db without dispatcher tables | ALREADY-FIXED in part | 9 | `open_queue` refuses a db with no `jobs` table. `test_poll_refuses_a_sqlite_file_without_a_jobs_table` pins it |
| #18 README heartbeat abort | APPLIES | 9 | README fix, no behaviour change |
| #18 claim rowcount | APPLIES | 7 | Defence only. No mutant can show it under `BEGIN IMMEDIATE` |
| #18 finish handoff not verified | APPLIES | 12 | the three handoff-verification tests |
| #18 edited comments | APPLIES | 12 | `test_edited_orch_comment_is_rejected` |
| #18 queued timeout counts backlog | APPLIES | 12 | the two queued-timeout tests |
| #18 short `current_head` | APPLIES | 9 | `test_live_source_refuses_a_short_head` |
| #18 POLL_SECONDS hard-coded | ALREADY-FIXED | none | `PollLoop` reads `config.poll_seconds`. Pinned by `tests/test_poll_30s.py` |
| #18 CI recorded once per head | ALREADY-FIXED | none | Fixed by #84. Pinned by `tests/test_ship_ci_fixes.py:307` |
| #18 T6 retries old heads | APPLIES | 9 | #105 AC6 on PR #110 adds the guard. Re-checked after the merge |
| #18 mutant T1 `from`, `role` | APPLIES | 9 | `test_t1_rejects_a_go_whose_from_or_role_is_wrong` |
| #18 mutant T2 `role` | ALREADY-FIXED | none | Killed by `test_the_flag_still_needs_the_factory_md_builder_and_a_trusted_login[wrong-role]` |
| #18 mutant T2 `from` | ALREADY-FIXED | none | Killed by `tests/test_dispatcher.py:525` |
| #18 mutant login equals `from` | APPLIES | 9 | the `claude-bot` author case |
| #18 mutant comment ledger | ALREADY-FIXED | none | Killed by `test_plan_queues_build_once_for_the_same_builder` |
| #18 mutant heartbeat guard | ALREADY-FIXED | none | Killed by `test_cli_job_run_stops_heartbeating_when_the_command_finishes_the_job` |
| #18 mutant finish guard | ALREADY-FIXED | none | Killed by `test_heartbeat_and_successful_finish_record_the_handoff` |
| #18 mutant queued compare-and-set | APPLIES | 7 | `test_queued_timeout_fails_closed_after_a_same_tick_claim` |
| #18 mutant 7-char head | APPLIES | 9 | `test_t2_ignores_a_six_char_ready_head` |
| #18 mutant Q1 tracker filter | APPLIES | 9 | `test_discover_ignores_a_ticket_from_the_wrong_tracker` |
| #18 mutant failed stuck job retried | APPLIES | 9 | `test_failed_stuck_job_is_not_retried` |
| #19.1 cross-family before route_job | APPLIES | 4 | `test_same_family_reviewer_is_refused` |
| #19.2 using_reserve and fallback | APPLIES | 4 | `test_choose_route_records_reserve_and_fallback` |
| #19.3 unreachable guard | APPLIES | 4 | No behaviour change, so no test. The guard is deleted |
| #19 FU1 go_comment recheck | APPLIES | 4 | `test_go_comment_rechecks_family_and_job` |
| #19 FU2 in-memory ledger | APPLIES | 5 | skill phrase test |
| #19 FU3 scope change to Planning | APPLIES | 5 | skill phrase test |
| #19 FU4 abandoned record | APPLIES | 12 | the two `answers abandoned` tests |
| #19 FU5 string likely_files | APPLIES | 4 | `test_route_request_refuses_a_string_for_likely_files` |
| #19 FU6 multi-line answers | APPLIES | 4 | `test_multi_line_answer_round_trips` |
| #19 FU7 base_branch read | APPLIES | 5 | skill phrase test |
| #19 record built_by on the route | APPLIES | 4 | same as #15 built_by |
| #20.1 fixing self-loop | APPLIES | 11 | `test_t2_self_loops_are_charted` |
| #20.2 chart tests skip lines | APPLIES | 11 | `test_chart_edges_fail_on_an_unparsed_line` |
| #20.3 post-merge comment | APPLIES | 11 | Comment only. No test |
| #20 r1 FU1 ShipPinnedToCoveredHead vacuous | APPLIES | 11 | the new `check.sh` break fixture |
| #20 r1 FU2 no liveness | APPLIES | 11 | SPEC 4.3 states it. No test |
| #20 r1 FU3 restated guards, OrchDoesNotExec | APPLIES | 11 | `bash model/check.sh` after the deletion |
| #20 r1 FU4 loose grep | APPLIES | 11 | `test_check_sh_fails_when_the_break_trace_ends_elsewhere` |
| #20 r1 FU5 KindGo, AC-unmet label | APPLIES | 11 | `bash model/check.sh` and the regenerated chart |
| #21.1 "exactly as written" | APPLIES | 1 | the prompt assertion and `test_doctor_fails_when_reply_keeps_the_bold_markers` |
| #21.2 `_BOLD` across a newline | APPLIES | 1 | `test_bump_pin_refuses_a_wrapped_first_bold_passage` |
| #23 I-1 stuck short-circuit | ALREADY-FIXED in part | 9 | The `if False:` mutant is killed by `tests/test_hooks.py:1097`. Unit 9 adds the go assertion |
| #23 FU-1 same action twice | APPLIES | 9 | `test_t1_same_action_twice_is_deduped` |
| #23 FU-2 stuck wake without limit | APPLIES | 10 | `test_failing_stuck_wake_stops_after_two_and_flags_attention` |
| #23 FU-3 missing stuck wake row | APPLIES | 10 | `test_missing_stuck_wake_row_is_regenerated` |
| #23 FU-4 enqueue_retry and migration | APPLIES | 9 | the three FU-4 tests |
| #27 L1 different handoff | APPLIES | 7 | `test_finish_with_a_different_handoff_is_refused` |
| #27 L2 heartbeat predicates | APPLIES | 7 | `test_heartbeat_refuses_the_wrong_owner` |
| #27 L3 unknown owner | APPLIES | 7 | `test_queue_ops_refuse_an_unknown_owner` |
| #27 N1 SQL-text coupling | APPLIES | 7 | `test_finish_races_t6_timeout_threaded` |
| #27 I1 `--failed` after T6 exits 0 | APPLIES | 7 | `test_failed_after_a_t6_timeout_is_a_no_op` pins the intended behaviour |
| #27 F1 owner is not a claim token | NOTE, split out | none | See Appendix B |
| #27 F2 refused finish duplicates work | ALREADY-FIXED in part | none | Review jobs use the posted verdict (#91). Build and fix jobs get #105 AC6 on PR #110 |
| #27 F3 `_t6` assumes one dispatcher | OBSOLETE | 9 | The `_poll_lock` flock allows one poller (`tests/test_cli.py:192`). Unit 9 adds a comment |
| #27 F4 README owner docs | APPLIES | 7 | README fix |
| #27 M1 merge-order note | ALREADY-FIXED | none | `lib/cli.py:836`, `:846` and `lib/wait.py:220`, `:222` pass `owner=` |
| #28 F3.1 signal mask restore | APPLIES | 7 | `test_wait_for_job_restores_the_callers_mask_and_handlers` |
| #28 F3.2 lock-wait signal delay | ALREADY-FIXED in part | 7 | Busy timeout cut from 30 s to 5 s in d5f65be. Unit 7 documents what remains |
| #28 F3.3 newline in DB path | APPLIES | 7 | `test_a_db_path_with_a_newline_is_one_line` |
| #28 F3.4 two-waiter overlap | APPLIES | 7 | the forced-overlap test |
| #28 M.1 parametrize bots | APPLIES | 7 | the three-bot broken-stdout test |
| #29 FU-1 stuck wake regeneration | APPLIES | 10 | same as #23 FU-2 |
| #29 FU-2 wire the Shipper | ALREADY-FIXED | none | `lib/cli.py:664` sets `t8.shipper` |
| #29 FU-3 login-to-identity map | NOTE | none | Phase 2, per the ticket. `trusted_logins` is now a login to identities map (`lib/orch_yml.py:687`). The check-config warning stays a note |
| #29 N2 false ship-failed wake | OBSOLETE in practice | none | The `_poll_lock` flock rules out two dispatchers. `test_t8_two_dispatchers_cannot_both_ship` pins one wake |
| #29 hook_t8 docstring nit | APPLIES | 11 | Rewrap. No test |
| #32 FU-2 migration race | APPLIES | 11 | `test_initialize_migrations_are_race_safe` |
| #32 FU-3 T4 replay flips label | APPLIES | 11 | `test_t4_replay_after_settle_keeps_merged` |
| #32 FU-4 older comment edited | APPLIES | 11 | `test_edited_older_verdict_does_not_override_a_newer_approve` |
| #32 n2 later untrusted REVISE | ALREADY-FIXED | none | Killed by `test_a_later_revise_blocks_the_ship_only_from_a_trusted_reviewer[untrusted-revise-is-ignored]` |
| #32 n3 HEAD_B green CI | APPLIES | 11 | the tightened gate case |
| #32 n4 3 failures in under 3 polls | APPLIES | 11 | the M1 test asserts after poll 2, and the docstring says it |
| #32 no real GitHub adapter | OBSOLETE | none | `lib/github_actions.py` `GitHubReviewActions` |
| #32 approve-ci-red has no wake | APPLIES | 11 | `test_approve_on_red_ci_wakes_grok_once` |
| #32 poll_errors never escalates | APPLIES | 15 | `test_repeated_poll_errors_flag_attention_once` |
| #35 R2-M1 four ci_status cases | APPLIES | 8 | the four `ci_status` cases |
| #35 short-read nit | APPLIES | 8 | Documented as the safe answer. No behaviour change |
| #35 FU-1 read errors outside the try | ALREADY-FIXED in part | 8 | Fixed in fbfef64 (#38). `test_poll_records_an_error_when_listing_candidates_raises` pins the two unpinned calls |
| #35 FU-2 issue-to-PR mapping | OBSOLETE | none | `lib/feeder.py:559-584` |
| #35 FU-3 real gh fixtures | APPLIES | 8 | `test_ci_status_over_recorded_gh_transcripts` |
| #35 stable_patch_id raises | NOTE, split out | none | See Appendix B. `handoff` is built (`lib/github_actions.py:298`), so only `stable_patch_id` is left |
| #35 branch protection | APPLIES | 8 | README note. It already fails closed as ship-failed |
| #35 nothing constructs a Dispatcher | OBSOLETE | none | `lib/cli.py:645` |
| #37 m1 tautological test | APPLIES | 14 | `test_status_connections_cannot_write` |
| #37 m2 subprocess blind spots | APPLIES | 14 | the recursive snapshot and audit hook |
| #37 m3 connections never closed | ALREADY-FIXED | 14 | `status.py:170`, `:206`, `:222`, `:274` close in `finally`. N4's test pins it |
| #37 m4 status missing from help | APPLIES | 14 | `test_help_lists_status` |
| #37 m5 DB without tables errors | APPLIES | 14 | `test_status_on_a_queue_only_db_shows_the_empty_state` |
| #37 m6 ids sort as strings | APPLIES | 14 | `test_status_sorts_ticket_ids_naturally` |
| #37 N1 `-shm` read-mark | APPLIES | 14 | Comment only |
| #37 N2 `-wal` check race | NOTE | 14 | Harmless, accepted with a comment |
| #37 N3 `-wal` without `-shm` | NOTE | none | Harmless, accepted |
| #37 N4 connection count and close | APPLIES | 14 | `test_status_closes_every_connection_and_reads_rows_once` |
| #37 UnavailableGrokDelivery comment | APPLIES | 8 | Comment only |

## Appendix B. Calls made without asking

No product question blocks this plan. Each call below follows from the ticket's wording, the code, or a fail-closed default. Max can overrule any of them in Max's own words.

- **One PR, three phases.** The ticket allows one PR per batch. The factory tracks one PR per ticket, so the batches are commits on #114, ordered so Phase A starts now and Phases B and C wait for #110 and #111.
- **Three items split out, not done here.** Each is a design change, not a test gap or a small fix. The builder opens one new issue for them and links it in the PR.
  - #27 F1. A claim token on every `orch wait` claim, not only autostart runs. That changes the trust model of `orch job done`.
  - #29 FU-3. The login-to-identity check-config warning. The ticket already calls it Phase 2.
  - #35 `stable_patch_id`. Patch-id comparison so a rebased head can skip re-review. It is a ship feature.
- **#19.2 shows the fallback and does not act on it.** The ticket asks to carry `using_reserve` and the fallback onto `RouteDecision`. The go body labels them "for information", because T1 runs factory.md's pair. Making T1 honour the go is a protocol change.
- **#18 edited comments are refused.** An `orch:v1` comment edited before it was handled is rejected and flags attention, rather than read as edited. A trusted author can post a new comment. Reading the edit would let a header change after a reviewer read it.
- **#18 handoff verification flags, it does not fail the job.** A `done` whose handoff comment does not check out flags attention. Failing the job would start a T6 retry and duplicate work on GitHub.
- **#18 the heartbeat does not kill the command.** The README is corrected to match the code. Killing a running harness on a heartbeat error touches every job run, and #106 (runner resilience, PR #112) is reworking that path.
- **#23 FU-2 caps at 2 failed stuck wakes.** Then the ticket flags attention, so a person sees a Grok delivery that keeps failing.
- **#11 F1.4 reads authorization from state transitions.** Only the trusted go moves `capped` to `fixing` under #105, so eval_report needs no new log key. A `cap_extra=` log key can be added later as a cross-check.

## Appendix C. Risks

- **High-risk surface.** Units 9 to 12 touch trust (`_trusted`, T1, handoff verification, edited comments) and ship (T4 replay, approve-ci-red, migrations). Each change is fail-closed and has a test, and the reviewer should read those units first.
- **Merge conflicts.** Phase B lands on #110's rewrite of `_t1`, `_t2`, `_t6` and the cap code. Phase C lands on #111's `status.py` and USAGE. Merge `main` in before each phase, never rebase, and re-run the full suite.
- **Install changes write into bot homes.** Unit 2's recovery renames and deletes only entries with the exact `.orch-backup-` and `.orch-staging-` prefixes.
- **Model size.** Unit 11's `HeadMoves` step and #110's `extra` counter both grow the state space. Keep the chart slice under 500 states. If it doesn't fit, bound `HeadMoves` to one occurrence.
- **Eval report format change.** F1.5 changes the log format and both snapshot fixtures. Nothing else writes the format today.
- **PR size.** About 110 items in one PR is a lot to review. Each unit is one commit, and the ledger maps every item to its commit, so the reviewer can go unit by unit.

## Appendix D. Reading list

- Issues #10, #11, #13, #14, #15, #17, #18, #19, #20, #21, #23, #27, #28, #29, #32, #35, #37.
- The review reports they cite, at `/workspace/factory-*-report.md` and `/workspace/factory-*-review-*.md` on the bot box.
- PR #110 `plan.md` (#105) and PR #111 (#107) for the overlapping code.
- `docs/SPEC.md` sections 4.1, 4.2, 4.3, 8 and 9.
