# lib/dispatch

The dispatcher's code, one module per area. `lib/dispatcher.py` is only the
facade: it re-exports every name it has always exported and composes
`Dispatcher` from the mixins below, so `from lib.dispatcher import X` keeps
working. In a ticket's `Touches:` line, name the module here, not
`lib/dispatcher.py`. A ticket that also changes shared code adds `core.py`,
`helpers.py`, `constants.py`, `types.py` or `transitions.py`.

No module here imports `lib.dispatcher` at run time, and no mixin imports
another mixin: shared code lives in `core.py`, `helpers.py`, `constants.py`, `decisions.py`,
`types.py` and `errors_text.py`.

The caps rules live in the `caps/` package, one module per rule. For a change
to one rule, name its module in `Touches:`, for example
`lib/dispatch/caps/effective.py`. Never name the directory `lib/dispatch/caps/`:
the feeder treats a directory as overlapping every file under it, so tickets
would queue one at a time again. A ticket that changes how `CapsMixin` is
composed names `lib/dispatch/caps/__init__.py`. Inside `caps/`, only
`__init__.py` imports the rule modules.

The verdict rules live in the `verdict/` package the same way, one module per
rule. For a change to one rule, name its module in `Touches:`: `verdict/t3.py`
for a REVISE, `verdict/repeat.py` for a REVISE that blocks on the same rule again, `verdict/t4.py` for an APPROVE, `verdict/blocked.py` for a
BLOCKED, `verdict/record.py` for how an
approval is recorded, `verdict/coverage.py` for whether an approval covers a
head, `verdict/pending.py` for a ready at a head an APPROVE left on red CI
covers, `verdict/shipped.py`, `verdict/preview.py` or `verdict/latest.py` for
those lookups. Write the full path, for example `lib/dispatch/verdict/t4.py`.
Never name the directory `lib/dispatch/verdict/`. A ticket that changes how
`VerdictMixin` is composed names `lib/dispatch/verdict/__init__.py`. Inside
`verdict/`, only `__init__.py` imports the rule modules. `verdict/convergence.py` is
not a rule mixin: it holds `converge`, a pure function over each round's
blocking findings, with no caller yet (#1035).

| Area | Module | Main contents |
|---|---|---|
| Data types | `types.py` | `Comment`, `CIResult`, `FailedCheck`, `TrackedTicket`, `TicketRef`, the Protocols (`PollSource`, `ReviewActions`, `GrokDelivery`, `Hook`, `DeviceChecks`, `Feeder`), ship errors, `ShipRequest`, `LINEAR_STATE_PHASES` |
| Constants | `constants.py` | state sets, grace periods, wake reasons, poll and ship error limits |
| Error text | `errors_text.py` | `_redact*`, `poll_error_detail`, `poll_error_text`, `safe_ship_error`, `scrub_detail` |
| Shared functions | `helpers.py` | `trusted_bots`, `_spread`, `_scope`, rounds, `_matching_head`, `_holds`, `ship_mode`, `_comment_order`, questions text, state names |
| Decision record | `decisions.py` | `Decision`, `record_decisions`, `chosen_words`: the F-04 (#1017) record of what an answer chose |
| State writes | `transitions.py` | `write_workflow_state`: the one writer of `ticket_state.workflow_state`, which appends a `ticket_transitions` row in the same transaction (#150) |
| Shared methods | `core.py` (`DispatcherBase`) | `__init__`, `_trusted*`, `_app_may_post`, `_review_actions`, `_tracker`, state labels, `_generation`, stuck checks, `_run_hooks`, `_fresh_ticket`, `_latest_step` |
| Comments | `comments.py` (`CommentsMixin`) | `process`, `_process_comments`, `_slash_go` (Max's plain `/go`), `_after_finish`, `_reject_edited`, `_remember_links`, handled-comment bookkeeping |
| Ready (T2) | `ready.py` (`ReadyMixin`) | `_t2`, `_queue_review`, `_verdict_after`, `_reenter_after_handoff`, `_flag_ready_after_cap`, `_record_ci_result`, `_review_context`, `_ci_fix_evidence` (builds a CI fix's evidence from each failed job's log), `_settled_red` (#853: every path that charges a red head first requests one rerun of its failed Actions jobs, recorded in `ci_reruns`, and waits on it; it returns the red to charge, classified from its fetched logs, or the wait reason `ci-rerun` while the rerun runs; #955: a red the PR base's latest completed run also fails is held as `ci-base-red` with no charge and no rerun, and the rerun runs once the base no longer explains it), `_request_fresh_ci` (#1287: a stale green head after the build, or in `building` once a trusted ready names it, gets one full rerun of its stale Actions workflow runs per head and set of changes, recorded in `ci_fresh_runs`; a stale unit with no Actions run, such as a newly required check that never reported, or a refused rerun is recorded `refused`, flagged `ci-rerun-refused` and wakes Grok once, and the head stays pending; a base workflow change is left to the merge queue's update), `_watch_ci_stale` (#955: one `ci-stale` needs-attention flag and Grok wake per ticket, CI state and head after `CI_STALE_SECONDS`), `_charge_unannounced_red` (#853: a red current head after the build that no ready or verdict names gets that rerun, then the charge and a CI fix), `_review_unannounced_green` (#986: a green current head in `building`, `fixing` or `review` that no trusted ready or verdict names and no build started from, with no review there and no open harness job, gets the next review round; past the cap it flags `unannounced-head-after-cap` and wakes Grok once), `_resume_unannounced_review` (#1003: when that state write failed, the next poll rewrites `review` before it reads comments, while that review is queued, running or finished with its round still open and no `ticket_transitions` row or other harness job came after it) |
| QA runs | `qa.py` (`QaMixin`) | `decide_scope`, the pure scope decision over `ScopeInput` (git facts from the injected `qa_diff`); `_queue_qa`, called where a review is queued, which writes the head's `qa_runs` rows and queues the `qa` jobs; `_qa_record`, a trusted `kind=qa` comment on its run; `_qa_follow`, once per poll, which cancels open runs at an old head, follows each run's job and wakes Grok once with `qa-failed` for a blocking primary FAIL at an approved head unless `qa.gate` is `block`; `_qa_job_failed`, T6 for a `qa` job; under `qa.gate: block`, `_qa_gate` reads the head's newest primary run with `_qa_status` and T4, T3, BLOCKED and the merge queue do what the pure `decide_gate` table says: ship only on a covering result, spend the review round on a FAIL, retry an environment failure once with `_retry_qa` and then park with `_park_qa`, and cancel open runs on BLOCKED with `_cancel_open_qa` (issue #765). The only writer of `qa_runs` except the QA worker, which sets `evidence_dir` when it accepts a lane's result (issue #874), the runner when a lane moves, and `stale` through `record_stale` when the PR head moved before the run posted, and a no-op when `factory.md` has no `qa` block (issue #763) |
| Verdicts: composition | `verdict/__init__.py` (`VerdictMixin`) | composes the ten rule mixins below and re-exports the old `verdict.py` names |
| Verdicts: REVISE (T3) | `verdict/t3.py` (`T3Mixin`) | `ReviseCIGate`, `_t3`, `_revise_ci_gate` |
| Verdicts: repeat block (T3) | `verdict/repeat.py` (`RepeatBlockMixin`) | `_convergence`, `_accepted_revises`, `_t3_repeat`: decide whether a REVISE spends a round or parks at waiting-max because it blocks on the same rule again (under factory.md `review.stop_on: convergence`, through `converge()` in `verdict/convergence.py`; under `rounds`, the default, it blocks on any carried id as before). The reviewer brief side of the key lives in `lib/brief/review.py` (the `repeat=` step) and `lib/brief/rules.py` (the high-risk rounds line). `_stop_question`, `_stop_decision`, `_held_stop`: the question a repeat, capped or unsat stop asks, and the option its answer chose. `_capped_stop`: a `capped` ticket whose review `converge()` called `Stalled` asks the stalled question (blocking findings did not shrink), read from the `stop` column T3 writes on its `review_events` row, so an edited verdict cannot change it. `_stop_history`: each capped, stalled or repeat stop with its finding ids and who released it (approver or engine); `_repeat_stops` counts from it. With factory.md `rule_engine: on`, `_t3_repeat` asks `_rule_on_repeat` inside the transaction that parks the ticket: a first repeat stop (`stops=1`) on a finding with no earlier extra round resumes the paused fix there through `_resume_step` (adding the cap round at the cap) and enqueues no Grok `clarify` job; every other repeat stop wakes Grok as before (issue #1309) |
| Verdicts: APPROVE (T4) | `verdict/t4.py` (`T4Mixin`) | `_t4`, `_approve_ci_red_fix`, `_held_ci_red`, `_held_ci_fix_out`, `_held_approve_covers`, `_latest_fix_answer`, `_ship_approval`: T4's ship tail, which T2 also runs for a covered ready |
| Verdicts: BLOCKED | `verdict/blocked.py` (`BlockedMixin`) | `_t_blocked`: stop the ticket and wake Grok once. `_blocked_by_another`: whether a verdict other than this comment blocked the ticket |
| Verdicts: approval recording | `verdict/record.py` (`ApprovalRecordMixin`) | `_record_approval`: the covering APPROVE the merge queue orders and gates on |
| Verdicts: approval coverage | `verdict/coverage.py` (`ApprovalCoverageMixin`) | `_approval_covers`: whether an APPROVE covers a head, by T4's rule |
| Verdicts: pending approval | `verdict/pending.py` (`PendingApprovalMixin`) | `_red_ci_approval_covering`: the APPROVE T4 left on red CI that covers a ready's head (T2, #493) |
| Verdicts: shipped approval | `verdict/shipped.py` (`ShippedApprovalMixin`) | `_shipped_approval`: the APPROVE whose ship intent completed last (T2) |
| Verdicts: previewed head | `verdict/preview.py` (`PreviewedHeadMixin`) | `_previewed_head`: the head an ok of an `after:<approver>` APPROVE may ship |
| Verdicts: latest verdict | `verdict/latest.py` (`LatestVerdictMixin`) | `_latest_review_verdict` |
| Verdicts: convergence policy | `verdict/convergence.py` (pure, no mixin) | `converge`: whether a REVISE round continues, repeats a rule or stalls, from each round's blocking ids. No caller yet (#1035) |
| Caps: composition | `caps/__init__.py` (`CapsMixin`) | composes the five rule mixins below and re-exports the old `caps.py` names |
| Caps: go (T1) | `caps/t1.py` (`T1Mixin`) | `_t1`: route a go, resume a capped ticket, clarify, queue the plan or build; `_go_once`: the go's dedupe key, keyed on the go after a finished build (issue #808) |
| Caps: resume after a cap or BLOCKED | `caps/resume.py` (`ResumeMixin`) | `_overrule_stop`: an approver's go that names `Overrule the finding` at a repeat, capped or stalled stop records the decision and `overruled-finding`, and queues nothing (issue #1328). `_resume_after_cap`: raise `cap_extra` and queue the fix, on a `capped` ticket or a `handed-off` one at the cap (issue #118); inside the cap a building go on a `ship: handoff` ticket reopens it with a `kind=reopen` fix and `reopen_hold`, without `cap_extra` (issue #1197). `_apply_round_grant` runs that transaction for a `RoundGrant` (an approver's go or, later, the rule engine), keyed on `grant.key` (issue #1307). `_reopen_on_reviews`: a trusted CHANGES_REQUESTED PR review after the approval reopens the same way through `_apply_round_grant`, or at the cap flags `changes-requested-after-cap` once (issue #1198). `_resume_after_blocked`: queue a fresh review at the same round |
| Caps: effective cap | `caps/effective.py` (`EffectiveCapMixin`) | `_over_cap`, `_config_cap`, `_effective_cap` |
| Caps: capped wake | `caps/wake.py` (`CappedWakeMixin`) | `_wake_capped`: with factory.md `rule_engine: on` it asks `_rule_on_cap` first, and a granted cap queues the fix without waking Grok; every other cap stop wakes Grok (issue #1308) |
| Caps: auto extra round | `caps/auto_extra.py` (`AutoExtraMixin`) | `_auto_granted_cap_extra`, `_record_auto_extra` (T3), `_drain_auto_extra`: the poller grants factory.md `review.auto_extra_rounds` rounds at the cap, or under `review.stop_on: convergence` one round per REVISE that `converge()` lets continue, and posts one comment per grant (issues #263, #1110). `review.stop_on` is `rounds` (the default, unchanged) or `convergence`; this repo sets `convergence` (issue #1112) |
| Merge queue | `merge_queue.py` (`MergeQueueMixin`) | `_enter_merge_queue`, `_advance_merge_queue(s)`, base updates, `_settle_update`, `_classify_update`, `_update_failed` |
| Holds | `holds.py` (`HoldsMixin`) | `_hold_gate`, `_hold_decision`, `_held`, `_hold_in_play`, `_may_approve*`, `_approver_go`, `_ship_head` |
| Ship | `ship.py` (`ShipMixin`, `ReviewShipper`) | ship intents, handoff redrive, external ships |
| Questions (T5) | `questions.py` (`QuestionsMixin`) | `_t5_questions`, `_t5_answers`, `_resume_paused` (records `decisions_of` the answer; `_resume_step` writes the resume in the caller's transaction), `_decisions`, `_answered_by`, cancel and requeue of answered jobs |
| Plan | `plan.py` (`PlanMixin`) | `_t_plan`, `_latest_plan_step` |
| Retry (TR) | `retry.py` (`RetryMixin`) | `_t_retry`, `_retry_step` (the resume table), `ticket_retries` rows |
| Jobs (T6) | `jobs.py` (`JobsMixin`) | `_t6`, `reconcile_jobs`, abandoned and dead jobs, devices, failure waits, stuck wakes |
| Follow-ups | `followups.py` (`FollowupsMixin`) | `_record_followups` (T3, T4), `_drain_followups`: park a verdict's `[follow-up]` findings once on the PR's one unlabelled follow-up issue and link them from the PR (issues #259, #492) |
| Job detail and Grok | `detail.py` (`DetailMixin`) | `_job_detail`, `_enqueue_step`, `_enqueue_or_flag`, `_project_wake_detail`, `wake_grok_once`, `_skips_wake`, `_drain_skipped_wakes`, `_grok_request`, `_linked_wake_detail`, `_wake_link`, `_deliver_grok_jobs`: a reason in `GROK_WAKE_SKIP` posts one PR notice instead of a Grok wake (issue #543) |
| Driver inbox | `inbox.py` (`InboxMixin`) | `_queue_inbox_post`, `_inbox_body`, `_drain_driver_inbox`: a copy of each Grok wake on a project's `driver_inbox` issue, once per job id, posted after Grok delivery (issue #737) |
| Tracking | `tracking.py` (`TrackingMixin`) | `_feed`, `_discover`, `_starts_tracking`, `_tracked_*`, `active_job_tickets`, `record_terminal`, `_enter_merged` |
| Linear states | `linear_states.py` (`LinearStatesMixin`) | `linear_move`, `_read_linear_states`, `_apply_linear_moves`, `_reopen_linear`: store each tracked Linear ticket's state, follow moves made in Linear (#749), and move a reopened ticket's issue back to In Progress (#1191) |
| Rule engine log | `rule_engine.py` (`RuleEngineMixin`) | `_log_ruling`: one `engine_decisions` row and one poller.log line per ruling; a replay writes nothing, and a changed outcome for the same subject updates the row (#1320). `_rule_on_cap` rules once per cap verdict, and `_apply_engine_grants` applies each pending cap grant through `_apply_round_grant` and posts one `orch-engine` notice quoting the validated go header, again at the next poll if a crash cut it short (issue #1308). `_rule_on_repeat` rules on a repeat stop in the parking transaction; its grant is already applied there, so `_apply_engine_grants` only posts its notice (issue #1309) |
| State DB | `schema.py` (`SchemaMixin`) | `_connect`, `_initialize`, migrations, `_add_missing_columns` |
| Poll | `poll.py` (`PollMixin`, `PollLoop`) | `poll_once`, handoff verification, poll-error streaks, attention |
| Eval log | `eval_log.py` (`EvalLogMixin`) | `_export_eval_log`: append `dispatcher.log` beside the database from the rows each poll committed (#545) |
| CI board | `ci_board.py` | `record`: the dashboard's `ci_jobs` rows from the check runs `ci_status` read, plus at most 3 `actions/runs/{id}/jobs` reads and 1 `actions/jobs/{id}/logs` read a cycle, each one `gh api` request (issue #717) |
| Cycle timing | `cycle_clock.py` (`CycleClock`) | per-phase cycle durations for poller.log and `orch status`; `CycleAlarm`: a `slow-cycle` or `lock-errors` needs_attention row on the `orch`/`poller` ticket naming the slowest or most-locked phase (#839) |
| Bookkeeping writes | `bookkeeping.py` (`LockLedger`) | `bookkeeping_write`: a poller bookkeeping write (read markers, poll errors, streaks, attention, feeder state) retried briefly on a held orch.db lock, else deferred to the next cycle and counted per phase in the cycle's `LockLedger`. Attention flags and clears wait in `attention_outbox` until orch.db takes them. A flag whose trigger commits (a handoff audit event, a failed ship intent, a handled comment, a dedupe drop) is written in that trigger's transaction instead, so a poller restart cannot lose it (#839) |
