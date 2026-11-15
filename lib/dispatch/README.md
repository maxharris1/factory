# lib/dispatch

The dispatcher's code, one module per area. `lib/dispatcher.py` is only the
facade: it re-exports every name it has always exported and composes
`Dispatcher` from the mixins below, so `from lib.dispatcher import X` keeps
working. In a ticket's `Touches:` line, name the module here, not
`lib/dispatcher.py`. A ticket that also changes shared code adds `core.py`,
`helpers.py`, `constants.py` or `types.py`.

No module here imports `lib.dispatcher` at run time, and no mixin imports
another mixin: shared code lives in `core.py`, `helpers.py`, `constants.py`,
`types.py` and `errors_text.py`.

| Area | Module | Main contents |
|---|---|---|
| Data types | `types.py` | `Comment`, `CIResult`, `TrackedTicket`, `TicketRef`, the Protocols (`PollSource`, `ReviewActions`, `GrokDelivery`, `Hook`, `DeviceChecks`, `Feeder`), ship errors, `ShipRequest`, `LINEAR_STATE_PHASES` |
| Constants | `constants.py` | state sets, grace periods, wake reasons, poll and ship error limits |
| Error text | `errors_text.py` | `_redact*`, `poll_error_detail`, `poll_error_text`, `safe_ship_error`, `scrub_detail` |
| Shared functions | `helpers.py` | `trusted_bots`, `_spread`, `_scope`, rounds, `_matching_head`, `_holds`, `ship_mode`, `_comment_order`, questions text, state names |
| Shared methods | `core.py` (`DispatcherBase`) | `__init__`, `_trusted*`, `_app_may_post`, `_review_actions`, `_tracker`, state labels, `_generation`, stuck checks, `_run_hooks`, `_fresh_ticket`, `_latest_step` |
| Comments | `comments.py` (`CommentsMixin`) | `process`, `_process_comments`, `_after_finish`, `_reject_edited`, handled-comment bookkeeping |
| Ready (T2) | `ready.py` (`ReadyMixin`) | `_t2`, `_queue_review`, `_verdict_after`, `_reenter_after_handoff`, `_flag_ready_after_cap`, `_record_ci_result`, `_review_context` |
| Verdicts (T3, T4) | `verdict.py` (`VerdictMixin`) | `_t3`, `_t4`, `_record_approval`, `_approval_covers` |
| Caps (T1) | `caps.py` (`CapsMixin`) | `_t1`, `_resume_after_cap`, `_over_cap`, `_effective_cap`, `_wake_capped` |
| Merge queue | `merge_queue.py` (`MergeQueueMixin`) | `_enter_merge_queue`, `_advance_merge_queue(s)`, base updates, `_settle_update`, `_classify_update`, `_update_failed` |
| Holds | `holds.py` (`HoldsMixin`) | `_hold_gate`, `_hold_decision`, `_held`, `_may_approve*`, `_approver_go`, `_ship_head` |
| Ship | `ship.py` (`ShipMixin`, `ReviewShipper`) | ship intents, handoff redrive, external ships |
| Questions (T5) | `questions.py` (`QuestionsMixin`) | `_t5_questions`, `_t5_answers`, `_answered_by`, cancel and requeue of answered jobs |
| Plan | `plan.py` (`PlanMixin`) | `_t_plan`, `_latest_plan_step` |
| Retry (TR) | `retry.py` (`RetryMixin`) | `_t_retry`, `_retry_step` (the resume table), `ticket_retries` rows |
| Jobs (T6) | `jobs.py` (`JobsMixin`) | `_t6`, `reconcile_jobs`, abandoned and dead jobs, devices, failure waits, stuck wakes |
| Job detail and Grok | `detail.py` (`DetailMixin`) | `_job_detail`, `_enqueue_step`, `_enqueue_or_flag`, `_project_wake_detail`, `wake_grok_once`, `_deliver_grok_jobs` |
| Tracking | `tracking.py` (`TrackingMixin`) | `_feed`, `_discover`, `_starts_tracking`, `_tracked_*`, `active_job_tickets`, `record_terminal`, `_enter_merged` |
| State DB | `schema.py` (`SchemaMixin`) | `_connect`, `_initialize`, migrations, `_add_missing_columns` |
| Poll | `poll.py` (`PollMixin`, `PollLoop`) | `poll_once`, handoff verification, poll-error streaks, attention |
