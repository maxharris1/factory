# lib/dispatch

The dispatcher's code, one module per area. `lib/dispatcher.py` is only the
facade: it re-exports every name it has always exported and composes
`Dispatcher` from the mixins below, so `from lib.dispatcher import X` keeps
working. In a ticket's `Touches:` line, name the module here, not
`lib/dispatcher.py`. A ticket that also changes shared code adds `core.py`,
`helpers.py`, `constants.py`, `types.py` or `transitions.py`.

No module here imports `lib.dispatcher` at run time, and no mixin imports
another mixin: shared code lives in `core.py`, `helpers.py`, `constants.py`,
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
`verdict/`, only `__init__.py` imports the rule modules.

| Area | Module | Main contents |
|---|---|---|
| Data types | `types.py` | `Comment`, `CIResult`, `FailedCheck`, `TrackedTicket`, `TicketRef`, the Protocols (`PollSource`, `ReviewActions`, `GrokDelivery`, `Hook`, `DeviceChecks`, `Feeder`), ship errors, `ShipRequest`, `LINEAR_STATE_PHASES` |
| Constants | `constants.py` | state sets, grace periods, wake reasons, poll and ship error limits |
| Error text | `errors_text.py` | `_redact*`, `poll_error_detail`, `poll_error_text`, `safe_ship_error`, `scrub_detail` |
| Shared functions | `helpers.py` | `trusted_bots`, `_spread`, `_scope`, rounds, `_matching_head`, `_holds`, `ship_mode`, `_comment_order`, questions text, state names |
| State writes | `transitions.py` | `write_workflow_state`: the one writer of `ticket_state.workflow_state`, which appends a `ticket_transitions` row in the same transaction (#150) |
| Shared methods | `core.py` (`DispatcherBase`) | `__init__`, `_trusted*`, `_app_may_post`, `_review_actions`, `_tracker`, state labels, `_generation`, stuck checks, `_run_hooks`, `_fresh_ticket`, `_latest_step` |
| Comments | `comments.py` (`CommentsMixin`) | `process`, `_process_comments`, `_slash_go` (Max's plain `/go`), `_after_finish`, `_reject_edited`, `_remember_links`, handled-comment bookkeeping |
| Ready (T2) | `ready.py` (`ReadyMixin`) | `_t2`, `_queue_review`, `_verdict_after`, `_reenter_after_handoff`, `_flag_ready_after_cap`, `_record_ci_result`, `_review_context`, `_ci_fix_evidence` (reads each failed Actions run's logs once, before a CI fix is queued) |
| Verdicts: composition | `verdict/__init__.py` (`VerdictMixin`) | composes the ten rule mixins below and re-exports the old `verdict.py` names |
| Verdicts: REVISE (T3) | `verdict/t3.py` (`T3Mixin`) | `ReviseCIGate`, `_t3`, `_revise_ci_gate` |
| Verdicts: repeat block (T3) | `verdict/repeat.py` (`RepeatBlockMixin`) | `_repeat_trigger`, `_accepted_revises`, `_t3_repeat`: park a REVISE that blocks on the same rule again at waiting-max |
| Verdicts: APPROVE (T4) | `verdict/t4.py` (`T4Mixin`) | `_t4`, `_approve_ci_red_fix`, `_held_ci_red`, `_held_ci_fix_out`, `_held_approve_covers`, `_latest_fix_answer`, `_ship_approval`: T4's ship tail, which T2 also runs for a covered ready |
| Verdicts: BLOCKED | `verdict/blocked.py` (`BlockedMixin`) | `_t_blocked`: stop the ticket and wake Grok once. `_blocked_by_another`: whether a verdict other than this comment blocked the ticket |
| Verdicts: approval recording | `verdict/record.py` (`ApprovalRecordMixin`) | `_record_approval`: the covering APPROVE the merge queue orders and gates on |
| Verdicts: approval coverage | `verdict/coverage.py` (`ApprovalCoverageMixin`) | `_approval_covers`: whether an APPROVE covers a head, by T4's rule |
| Verdicts: pending approval | `verdict/pending.py` (`PendingApprovalMixin`) | `_red_ci_approval_covering`: the APPROVE T4 left on red CI that covers a ready's head (T2, #493) |
| Verdicts: shipped approval | `verdict/shipped.py` (`ShippedApprovalMixin`) | `_shipped_approval`: the APPROVE whose ship intent completed last (T2) |
| Verdicts: previewed head | `verdict/preview.py` (`PreviewedHeadMixin`) | `_previewed_head`: the head an ok of an `after:<approver>` APPROVE may ship |
| Verdicts: latest verdict | `verdict/latest.py` (`LatestVerdictMixin`) | `_latest_review_verdict` |
| Caps: composition | `caps/__init__.py` (`CapsMixin`) | composes the five rule mixins below and re-exports the old `caps.py` names |
| Caps: go (T1) | `caps/t1.py` (`T1Mixin`) | `_t1`: route a go, resume a capped ticket, clarify, queue the plan or build |
| Caps: resume after a cap or BLOCKED | `caps/resume.py` (`ResumeMixin`) | `_resume_after_cap`: raise `cap_extra` and queue the fix, on a `capped` ticket or a `handed-off` one at the cap (issue #118). `_resume_after_blocked`: queue a fresh review at the same round |
| Caps: effective cap | `caps/effective.py` (`EffectiveCapMixin`) | `_over_cap`, `_config_cap`, `_effective_cap` |
| Caps: capped wake | `caps/wake.py` (`CappedWakeMixin`) | `_wake_capped` |
| Caps: auto extra round | `caps/auto_extra.py` (`AutoExtraMixin`) | `_auto_granted_cap_extra`, `_record_auto_extra` (T3), `_drain_auto_extra`: the poller grants factory.md `review.auto_extra_rounds` rounds at the cap and posts one comment per grant (issue #263) |
| Merge queue | `merge_queue.py` (`MergeQueueMixin`) | `_enter_merge_queue`, `_advance_merge_queue(s)`, base updates, `_settle_update`, `_classify_update`, `_update_failed` |
| Holds | `holds.py` (`HoldsMixin`) | `_hold_gate`, `_hold_decision`, `_held`, `_hold_in_play`, `_may_approve*`, `_approver_go`, `_ship_head` |
| Ship | `ship.py` (`ShipMixin`, `ReviewShipper`) | ship intents, handoff redrive, external ships |
| Questions (T5) | `questions.py` (`QuestionsMixin`) | `_t5_questions`, `_t5_answers`, `_resume_paused`, `_answered_by`, cancel and requeue of answered jobs |
| Plan | `plan.py` (`PlanMixin`) | `_t_plan`, `_latest_plan_step` |
| Retry (TR) | `retry.py` (`RetryMixin`) | `_t_retry`, `_retry_step` (the resume table), `ticket_retries` rows |
| Jobs (T6) | `jobs.py` (`JobsMixin`) | `_t6`, `reconcile_jobs`, abandoned and dead jobs, devices, failure waits, stuck wakes |
| Follow-ups | `followups.py` (`FollowupsMixin`) | `_record_followups` (T3, T4), `_drain_followups`: park a verdict's `[follow-up]` findings once on the PR's one unlabelled follow-up issue and link them from the PR (issues #259, #492) |
| Job detail and Grok | `detail.py` (`DetailMixin`) | `_job_detail`, `_enqueue_step`, `_enqueue_or_flag`, `_project_wake_detail`, `wake_grok_once`, `_skips_wake`, `_drain_skipped_wakes`, `_grok_request`, `_linked_wake_detail`, `_wake_link`, `_deliver_grok_jobs`: a reason in `GROK_WAKE_SKIP` posts one PR notice instead of a Grok wake (issue #543) |
| Tracking | `tracking.py` (`TrackingMixin`) | `_feed`, `_discover`, `_starts_tracking`, `_tracked_*`, `active_job_tickets`, `record_terminal`, `_enter_merged` |
| State DB | `schema.py` (`SchemaMixin`) | `_connect`, `_initialize`, migrations, `_add_missing_columns` |
| Poll | `poll.py` (`PollMixin`, `PollLoop`) | `poll_once`, handoff verification, poll-error streaks, attention |
| Eval log | `eval_log.py` (`EvalLogMixin`) | `_export_eval_log`: append `dispatcher.log` beside the database from the rows each poll committed (#545) |
| Cycle timing | `cycle_clock.py` (`CycleClock`) | per-phase cycle durations for poller.log and `orch status` |
