# Plan for #105, rounds, caps and retries

Max approves extra review rounds, and the factory loses track of them. Verdict headers like `round=4/3` fail to parse, `review_round` sticks at 3, a second extra round is deduped away, and a trusted `ready` after the cap queues a round nobody approved. Separately, T6 retries fix jobs that already posted their `ready`, and Claude print mode kills a run whose tests ran in the background.

This plan fixes both in one PR (#110, branch `factory/issue-105`). The rule it enforces is that the round cap is a recorded fact on the ticket and every builder handoff copies it from the job. Nothing a bot writes into a header can move it. Max's extra round, relayed by the trusted Grok go, is the only thing that raises it.

Folds in #102, #61, #53, #67, #68 and #42 N2.

## How to read this

One box is one unit of work. Every box names the evidence that checks it. Check a box only when its evidence exists, a test run, a log line or a SHA.

This is a factory plan job, so the execution is the factory build job for #105 under `pstack/skills/poteto-mode/playbooks/feature.md`, not an autopilot program. There is one PR. Codex Bot reviews it (risk high, up to three rounds). Grok Bot merges it on `ship: handoff`. The poteto multi-phase skeleton's ten cloud lanes do not apply to a single backend PR with no UI. Its verification rule does, adapted in **Verify** below.

Tests alone are not sufficient verification. The PR is verified only when its unit, replay and CI boxes are all checked.

## The data shape

The change hangs on three facts. The builder names them before writing code.

| Fact | Where it lives | Who writes it | Who reads it |
| --- | --- | --- | --- |
| `cap_extra` | New `ticket_state.cap_extra INTEGER NOT NULL DEFAULT 0`, added by the existing `PRAGMA table_info` migration in `Dispatcher._initialize` | T1, in the same transaction that resumes a capped ticket | `Dispatcher._effective_cap(ticket, connection=None)`, which returns `rounds_low` or `rounds_high` plus `cap_extra` |
| Next round | Job `detail["round"]` as `"<n>/<effective cap>"`. For a review job, `n` is the round under review. For a build or fix job, `n` is the review round its `ready` will start | `Dispatcher._job_detail`, which takes the effective cap instead of recomputing the config cap | `lib/brief.py` `_facts`, the ready and verdict headers, `_posted_verdict` |
| Trusted risk | `TrackedTicket.risk`, as today | `lib/poll_source.py` `_risk`, now reading only comments whose author is trusted for the `from` bot | Every cap computation |

`review_round` keeps its meaning, the number of REVISE rounds completed. It already advances in T3. It stopped at 3 only because the round-4 verdict never parsed.

## Criterion-to-work map

| # | Acceptance criterion | Work that meets it | Tests that fail on revert |
| --- | --- | --- | --- |
| 1 | Build and fix ready headers carry `round=<next>/<cap>` and `risk=<risk>` from the job facts. The builder never invents a denominator. A trusted ready naming a newer head re-reads the PR head uncached before calling it stale. | Unit 4 | `test_build_and_fix_jobs_carry_next_round_and_cap`, `test_ready_header_carries_round_and_risk_from_job_facts`, `test_ready_header_has_no_round_when_the_job_has_none`, `test_ready_naming_a_newer_head_rereads_the_pr_head` |
| 2 | Extra round is a recorded fact raising the cap by 1 (`cap_extra`), set by the trusted Grok go on a capped ticket. `round=4/4` parses. Over-cap verdict rejected, not unparsed. `review_round` advances every round. REVISE at the effective cap moves to `capped`. | Units 1 and 2 | `test_extra_round_raises_the_cap_once_per_go`, `test_round_over_its_denominator_parses`, `test_verdict_over_the_effective_cap_is_rejected_over_cap`, `test_untrusted_go_on_a_capped_ticket_records_no_extra_round` |
| 3 | A ready after the cap, with no recorded extra round, queues nothing and marks the ticket needs-attention (#53, #67). | Unit 3 | `test_ready_after_the_cap_queues_nothing_and_flags_attention`, `test_ready_after_an_extra_round_queues_the_next_review` |
| 4 | `risk=` is read only from trusted authors (#68). | Unit 5 | `test_untrusted_risk_comment_leaves_the_ticket_low`, `test_later_trusted_risk_still_applies` |
| 5 | The resume-fix dedupe key includes the go comment id, so every extra round queues exactly one fix (#42 N2). | Unit 1 | `test_extra_round_raises_the_cap_once_per_go` (two caps, two gos, two fixes), `test_replayed_resume_go_queues_no_second_fix` |
| 6 | Before T6 retries a failed fix or build, a trusted builder ready at a new head posted after the job started marks the job done (`done-from-ready`) and routes the ready. No duplicate fix. | Unit 6 | `test_failed_fix_with_a_posted_ready_is_done_from_ready`, `test_failed_build_with_a_posted_ready_is_done_from_ready`, `test_ready_at_the_job_head_does_not_stop_the_retry`, `test_untrusted_ready_does_not_stop_the_retry` |
| 7 | The brief says to run long test suites in the foreground. The runner sets `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0` for Claude print mode. A run that ends with unpushed commits has its kept worktree reused by the T6 retry. | Unit 7 | `test_builder_briefs_say_run_tests_in_the_foreground`, `test_claude_print_mode_gets_the_bg_wait_ceiling`, `test_other_harnesses_get_no_bg_wait_ceiling`, `test_retry_adopts_the_kept_worktree_with_unpushed_commits`, `test_retry_does_not_adopt_a_clean_or_live_worktree` |
| 8 | Tests for each item that fail on revert. SPEC updated (T1, T2, T6, cap rows). | Every unit names its tests. Unit 8 updates SPEC and the TLA+ model | `pytest`, `bash model/check.sh` |

Every criterion is mapped.

## Work units

Each unit is one commit that ends green under `env -u ORCH_DB pytest`. The order lets each unit build on a verified base. Test first in every unit.

### Unit 1. Record the extra round and key the resume fix on the go (AC 2, AC 5)

**Files.**

- [ ] Edit `lib/dispatcher.py`.
- [ ] Edit `tests/test_dispatcher.py`.

**Build.**

- [ ] Add `cap_extra` to the `ticket_state` DDL and to the column migration list in `Dispatcher._initialize`.
- [ ] Add `Dispatcher._effective_cap(ticket, connection=None)`. It returns the config cap for `ticket.risk` plus `cap_extra`. Replace the three inline cap computations in `_t3`, `_job_detail` and anything else that reads `rounds_low` or `rounds_high` directly.
- [ ] Rework the capped branch of `_t1` into one `BEGIN IMMEDIATE` transaction, the way `_t3` works. Read `workflow_state`. When it is `capped`, increment `cap_extra`, set `workflow_state = 'fixing'` and `enqueue_in_transaction` the fix with dedupe key `t1:{scope}:fix:{comment.id}`. The fix detail carries `round = review_round + 1` over the new effective cap and `resume = True`. The non-capped go path keeps its `g{generation}` key.
- [ ] Trust is unchanged. `_t1` already returns `rejected-go-author` before any write, so an untrusted go never touches `cap_extra`.

**You see.**

- [ ] After three REVISE verdicts and one trusted go, `ticket_state` reads `review_round=3, cap_extra=1, workflow_state='fixing'`, and the queued fix's detail has `"round":"4/4"`.

**Verify, unit.** Run `env -u ORCH_DB pytest tests/test_dispatcher.py -k "extra_round or resume_go or capped"`.

- [ ] `test_extra_round_raises_the_cap_once_per_go` drives cap 3 to capped, go, ready, REVISE `4/4`, capped again, a second go, ready, REVISE `5/5`. It asserts `review_round` reads 4 then 5, `cap_extra` reads 1 then 2, two distinct fix jobs exist, and the state ends `capped`. With the old `g{generation}` key the second fix is dropped and the test fails.
- [ ] `test_replayed_resume_go_queues_no_second_fix` processes the same go twice (handled-comments row deleted between passes). One fix job, `cap_extra` stays 1.
- [ ] `test_untrusted_go_on_a_capped_ticket_records_no_extra_round`.
- [ ] Update `test_capped_ticket_accepts_trusted_go_as_one_more_fix` for the new key and detail.

### Unit 2. Parse any round and judge it against the effective cap (AC 2)

**Files.**

- [ ] Edit `lib/handoff.py`.
- [ ] Edit `lib/dispatcher.py`.
- [ ] Edit `tests/test_handoff.py` and `tests/test_dispatcher.py`.

**Build.**

- [ ] In `handoff._check_value`, keep the `N` or `N/CAP` form check and drop the `N > CAP` rejection. The header is syntax. The cap is the dispatcher's fact.
- [ ] In `_t3` and `_t4`, before the stale check, return `rejected-over-cap` when the reported round is above `_effective_cap`. The `rejected-` prefix already wakes the project bot through `_wake_comment_problem`, and `_REJECTED_OUTCOMES` already keeps it from counting as a job's handoff.
- [ ] `_t3` caps at `round_number >= _effective_cap(ticket, connection)`, read inside its transaction.
- [ ] `_t_plan` keeps checking the plan round against `plan_gap_count`, so a `round=3/2` plan, which now parses, is still stale. Add a test that says so.

**You see.**

- [ ] `parse_comment("<!-- orch:v1 kind=verdict ... round=4/3 ... -->")` returns fields instead of raising `bad round '4/3'`. The dispatcher records it as `rejected-over-cap` when no extra round is recorded.

**Verify, unit.** Run `env -u ORCH_DB pytest tests/test_handoff.py tests/test_dispatcher.py -k "round or cap"`.

- [ ] `test_round_over_its_denominator_parses` covers `4/4`, `4/3` and `round=4`.
- [ ] `test_verdict_over_the_effective_cap_is_rejected_over_cap` covers REVISE and APPROVE at `4/3` with `cap_extra=0`. Outcome `rejected-over-cap`, no job, one wake.
- [ ] `test_plan_round_over_two_is_still_stale`.

### Unit 3. Hold the cap at review intake (AC 3)

**Files.**

- [ ] Edit `lib/dispatcher.py`.
- [ ] Edit `tests/test_dispatcher.py`.

**Build.**

- [ ] In `_t2`, after the trust and planning checks, when `workflow_state` is `capped`, return `capped-ready` and call `flag_attention(repo, ticket, "ready-after-cap", ...)` naming the ready comment and head. Queue nothing and leave the state alone. This also closes the second-AC-unmet cap, where `review_round` is below the cap but the ticket is `capped`.
- [ ] `_queue_review` returns whether it queued. When the next round is above `_effective_cap`, it queues nothing and flags `ready-after-cap`. `_t2` and `_queue_patch_review` set `review` state only when it queued. This is the backstop for any other path into a review.
- [ ] Clear `ready-after-cap` in Unit 1's resume transaction, so the flag does not outlive Max's OK.

**You see.**

- [ ] `orch status` shows the ticket under needs-attention with reason `ready-after-cap`. `jobs` has no new review row.

**Verify, unit.** Run `env -u ORCH_DB pytest tests/test_dispatcher.py -k "after_the_cap or after_an_extra_round"`.

- [ ] `test_ready_after_the_cap_queues_nothing_and_flags_attention` covers both a REVISE at the cap and a capped state with `review_round` under the cap.
- [ ] `test_ready_after_an_extra_round_queues_the_next_review` checks the review detail reads `"round":"4/4"`.

### Unit 4. Builder handoffs copy the round and risk from the job (AC 1)

**Files.**

- [ ] Edit `lib/dispatcher.py`.
- [ ] Edit `lib/brief.py`.
- [ ] Edit `tests/test_dispatcher.py`, `tests/test_brief.py`, `tests/test_live_poll.py` or `tests/test_dispatcher.py` for the fresh read.

**Build.**

- [ ] `_job_detail` fills `round` for `build` and `fix` jobs too. T1 build and T1P build carry `1/<cap>`. T2 CI fix carries `review_round + 1` (a gate retry is not a round). T3 fix carries `round_number + 1`. The Unit 1 resume fix carries `review_round + 1` over the raised cap. Plan jobs keep no review round, since their header uses the plan round.
- [ ] In `lib/brief.py` `_handoff_section`, the ready header for implement, fix and merge-only becomes `kind=ready from=<bot> role=builder round=<facts.round> head=<pushed-head-sha> risk=<facts.risk>`, in the canonical key order. When the job has no round or no risk, the key is left out. The brief never writes a `<n>/<cap>` placeholder for a builder, so the builder has nothing to invent.
- [ ] In `_t2`, when the reported head does not match `ticket.current_head`, read the ticket again uncached before returning `stale-ready-head`. `poll_once` keeps `getattr(source, "fresh_ticket", None)` on the dispatcher for the cycle. `_t2` builds the `TicketRef` from `_tracker(repo, ticket_id)`. When the fresh head matches, `_t2` continues with the fresh ticket and its CI, and stores it in `_polled`. When the fresh read raises, `_t2` returns `(False, "pr-head-unavailable")` so the next poll tries again instead of dropping the ready for good.

**You see.**

- [ ] A fix brief for round 4 of a high-risk ticket with one extra round ends with `<!-- orch:v1 kind=ready from=claude-bot role=builder round=4/4 head=<pushed-head-sha> risk=high -->`.

**Verify, unit.** Run `env -u ORCH_DB pytest tests/test_brief.py tests/test_dispatcher.py -k "round or ready_header or newer_head"`.

- [ ] `test_build_and_fix_jobs_carry_next_round_and_cap` covers T1 build, T1P build, T2 CI fix, T3 fix and the resume fix.
- [ ] `test_ready_header_carries_round_and_risk_from_job_facts` for build and fix.
- [ ] `test_ready_header_has_no_round_when_the_job_has_none`.
- [ ] `test_ready_naming_a_newer_head_rereads_the_pr_head`. `FakeSource.fresh_ticket` returns the new head with green CI. Outcome `queued-review`. Without the fresh read the outcome is `stale-ready-head` and the test fails.

### Unit 5. Read risk only from trusted authors (AC 4)

**Files.**

- [ ] Edit `lib/poll_source.py`.
- [ ] Edit `lib/dispatcher.py` if the trust lookup moves to a shared helper.
- [ ] Edit `tests/test_live_poll.py`.

**Build.**

- [ ] `_risk(comments, trusted)` skips a comment unless its `from` bot is in the set its author is trusted for. The author lookup is the dispatcher's own rule (`Dispatcher._trusted_bots`), with GitHub logins from `config.trusted_logins` and Linear authors from `config.trusted_linear_users`. Move that lookup into one module-level function both files call, so the two rules cannot drift.
- [ ] Both `_github_ticket` and `_linear_ticket` pass the trust map.

**Verify, unit.** Run `env -u ORCH_DB pytest tests/test_live_poll.py -k risk`.

- [ ] `test_untrusted_risk_comment_leaves_the_ticket_low`. A trusted go says `risk=low`, then an untrusted comment says `risk=high`. Ticket risk is `low`.
- [ ] `test_later_trusted_risk_still_applies`.

### Unit 6. Do not retry a builder job whose ready is already posted (AC 6)

**Files.**

- [ ] Edit `lib/dispatcher.py`.
- [ ] Edit `tests/test_liveness_requeue.py` or `tests/test_dispatcher.py`, next to the existing `done-from-verdict` tests.

**Build.**

- [ ] Generalize `_posted_verdict` and `_done_from_verdict` into `_posted_handoff(job)` and `_done_from_handoff(job, comment, event)`. One lookup, one done-marking path, two callers. Migrate the review caller in the same commit.
- [ ] For a failed `fix` or `build` job, `_posted_handoff` returns the first comment from the polled ticket that is a `kind=ready` from `<recipient>-bot` with `role=builder` by a trusted login, posted at or after `job.created_at`, whose head differs from `job.request.head` (any head for a build job with none), and whose handled outcome is not in `_REJECTED_OUTCOMES`.
- [ ] `_t6` checks it before the attempt-1 retry. A hit marks the job `done` with that comment as `handoff_comment_id` and records the `done-from-ready` job event. The ready routes through T2 as any ready does. It was either handled already or is picked up in the next comment pass, so T6 queues nothing itself.

**You see.**

- [ ] `job_events` shows `done-from-ready` with the ready comment id. `jobs` has no `t6-retry:<id>` row.

**Verify, unit.** Run `env -u ORCH_DB pytest -k "done_from_ready or done_from_verdict"`.

- [ ] `test_failed_fix_with_a_posted_ready_is_done_from_ready`, the #99 dac3fbfe case.
- [ ] `test_failed_build_with_a_posted_ready_is_done_from_ready`.
- [ ] `test_ready_at_the_job_head_does_not_stop_the_retry`.
- [ ] `test_untrusted_ready_does_not_stop_the_retry`.
- [ ] The existing `done-from-verdict` tests still pass unchanged.

### Unit 7. Keep a builder's work across print-mode exits and retries (AC 7)

**Files.**

- [ ] Edit `lib/brief.py`.
- [ ] Edit `lib/job_run.py`, the env helper only, to stay clear of PR #100 and PR #104.
- [ ] Edit `lib/cli.py` (`orch job run`).
- [ ] Edit `lib/worktrees.py`.
- [ ] Edit `lib/dispatcher.py` (`_t6` retry detail).
- [ ] Edit `tests/test_brief.py`, `tests/test_job_run.py` or `tests/test_cli.py`, `tests/test_worktrees.py`, `tests/test_dispatcher.py`.

**Build.**

- [ ] Brief. Fix and implement steps say to run the test suite in the foreground and never put it in the background, because print mode stops background tasks after the last turn. On attempt 2, they say the worktree may hold attempt 1's unpushed commits, so check `git status` and `git log @{u}..` before starting.
- [ ] Env. Add `harness_env(command)` to `lib/job_run.py`. It returns `{"CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "0"}` when `command[0]`'s basename is `claude` and the argv has `-p` or `--print`, else `{}`. `orch job run` merges it into the box `environment` and into `remote_env`, so both box and remote runs get it. `remote.start_script` already accepts any plain name.
- [ ] Retry detail. `_t6` adds `"retry_of": <failed job id>` to the retry's detail JSON.
- [ ] Worktree adoption. `worktrees.prepare(..., adopt=<job id or None>)`. Under the clone lock, when the new job has no worktree yet and `jobs/<adopt>` is a worktree of the same clone whose owner is not alive and whose `keep_reason` is not None, run `git worktree move` to `jobs/<new id>`, rewrite the record, and log `reusing kept worktree of job <adopt> (<reason>)`. Otherwise prepare as today. `orch job run` passes `retry_of` from the job detail.
- [ ] Remote autostart runs are started without `--worktree` (`waiter._start` passes `worktree=not remote`), so no kept worktree exists there to adopt. Record that in SPEC and leave remote dirs alone.

**Verify, unit.** Run `env -u ORCH_DB pytest tests/test_brief.py tests/test_job_run.py tests/test_cli.py tests/test_worktrees.py -k "foreground or bg_wait or adopt or retry_of"`.

- [ ] `test_builder_briefs_say_run_tests_in_the_foreground`.
- [ ] `test_claude_print_mode_gets_the_bg_wait_ceiling` checks both the box env and the remote start script.
- [ ] `test_other_harnesses_get_no_bg_wait_ceiling` covers codex and claude without `-p`.
- [ ] `test_t6_retry_detail_names_the_failed_job`.
- [ ] `test_retry_adopts_the_kept_worktree_with_unpushed_commits` uses a real git clone in `tmp_path`, as `tests/test_worktrees.py` does. It asserts the commit is present in the new worktree.
- [ ] `test_retry_does_not_adopt_a_clean_or_live_worktree`.

### Unit 8. SPEC and model (AC 8)

**Files.**

- [ ] Edit `docs/SPEC.md`.
- [ ] Edit `model/Orchestrator.tla` and its `.cfg` files if the invariant needs a bound.

**Build.**

- [ ] Add a decision-log row dated Nov 13 for #105.
- [ ] Update the section 4.2 rows. T1 records `cap_extra` on a go to a capped ticket and keys the fix on the go comment. T2 re-reads a newer head uncached and turns a ready on a capped ticket into needs-attention with no job. T3 caps at the effective cap and rejects an over-cap round as `rejected-over-cap`. T6 adds `done-from-ready` and the kept-worktree retry.
- [ ] Section 4.2 brief paragraph. Builder readies carry `round` and `risk` from the job.
- [ ] Section 5. `round=<n>/<cap>` names the effective cap. A round over its denominator parses, and the dispatcher judges it.
- [ ] Section 8 and the section 9 cap row. Max's OK is recorded as `cap_extra`, one per trusted go, and nothing else raises the cap. `risk=` comes only from trusted authors.
- [ ] Model. `MaxOkAnotherRound` raises an `extra` counter (bounded at 1 for TLC). T3 compares to `cap + extra`. Add the invariant `counts.round <= cap + extra`. Run `bash model/check.sh`.

**Verify, unit.**

- [ ] `bash model/check.sh` passes locally or in the `statechart` CI job.

## Verify

Tests alone are not sufficient verification. The PR is verified only when its unit, replay and CI boxes are all checked.

- [ ] Unit. `env -u ORCH_DB pytest` passes at the PR head, in the foreground.
- [ ] Unit, revert check. For each test named in the map, revert its unit's production change on a scratch branch and record that the test fails. Note the result per criterion in the ready comment.
- [ ] Replay. A scratch script, kept out of the PR, feeds the real comment bodies from PR #100 (rounds 3 to 5) and the #99 fix job through `Dispatcher` with `FakeSource` and a fresh `orch.db`. Pass when the round-4 and round-5 verdicts parse, `review_round` reaches 5, the state ends `capped` with no hand edit, the second extra round queues one fix, and the #99 failed fix is `done-from-ready` with no retry. Paste the script's final `ticket_state` row and `job_events` into the ready comment.
- [ ] CI. `.github/workflows/ci.yml` is green at the head, covering `pytest`, the `check-config` steps, `eval-report` and `statechart`.
- [ ] Perf. None. The change adds one uncached PR read per non-matching ready (each ready is handled once) and one SQLite column. There is no metric to move.
- [ ] Review gate. None. #105 changes no interaction. Codex Bot's round-1 review on the ready head is the gate.

## Close

- [ ] Every box above is checked with its evidence.
- [ ] Post the ready handoff with the pushed head, the per-criterion test list, the revert results and the replay output.

## Appendix A. Questions settled without a prototype

No product question blocks this. Each call below follows from the ticket's own words or from existing code.

- Which go raises the cap? The ticket says the trusted Grok go on a capped ticket. `_t1` already turns that go into a resume fix, so the record goes in the same transaction.
- Should the parser still reject `N > CAP`? No. The ticket asks for over-cap rejection, not "left unparsed". Only the dispatcher knows the effective cap.
- Ready on a capped ticket with rounds left (second AC-unmet)? It is "a ready after the cap" by state, so Unit 3 checks the state as well as the number.
- Remote worktree reuse? Remote autostart runs have no `--worktree`, so there is nothing kept to reuse. Box worktrees are the ones `worktrees.finish` keeps.

## Appendix B. Alternatives rejected

- Raising the cap from the header denominator (`round=4/4` means cap 4). Rejected because any trusted bot could then raise the cap, which is the #53 bypass in another form.
- Reading `risk=` only from Grok's orchestrator comments. Safer still, but it changes which comments set risk on `ready_starts_review` repos that have no Grok brief. The ticket asks for trusted authors, so that is the rule. The default stays `high`.
- Retrying a ready whose head is not yet visible by leaving it unhandled. Rejected because a truly stale ready would then be retried forever. One uncached read settles it.
- Pushing unpushed commits at the end of a failed run. Rejected because the run may have stopped mid-change, and pushing starts CI and review on half-done work.

## Appendix C. Risks

- `_t1` capped path moves into a transaction (Unit 1). A crash between the enqueue and `_mark_handled` replays the go. The comment-id dedupe key and the state check make the replay a no-op. The replay test covers it.
- `harness_env` and `orch job run` edits sit beside PR #100 and PR #104, which also touch `lib/cli.py` and `lib/job_run.py`. Keep the edit to one helper and two merge lines. Bring `main` in by a merge commit if either lands first.
- Adopting a worktree must never take one a live run still owns. `prepare` checks the owner pid and start time under the clone lock, the same check `gc` uses.
- #108 (ticket lifecycle) touches the same dispatcher code and waits on this PR.

## Appendix D. Reading list

- `docs/SPEC.md` sections 4.2, 5, 8 and 9.
- `lib/dispatcher.py` `_t1`, `_t2`, `_t3`, `_t4`, `_queue_review`, `_job_detail`, `_t6`, `_posted_verdict`, `_done_from_verdict`, `_initialize`.
- `lib/handoff.py` `_check_value`.
- `lib/poll_source.py` `_risk`, `_github_ticket`, `_linear_ticket`, `fresh_ticket`.
- `lib/brief.py` `_facts`, `_task_section`, `_handoff_section`.
- `lib/worktrees.py` `prepare`, `finish`, `keep_reason`. `lib/cli.py` `orch job run`.
- Issues #102, #61, #53, #67, #68 and #42 (N2).
