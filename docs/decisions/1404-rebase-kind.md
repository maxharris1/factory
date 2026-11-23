# #1404: a trusted `kind=rebase` queues a rebase-only fix

- **Decision:** A trusted `kind=rebase`, its own handoff kind, queues one `fix` job with `detail.kind="rebase"` that spends no review round, raises no `cap_extra`, records the `review_round` it read as `rebase.round`, and is kept out of Linear's external handoff by a hold read from the newest harness job, not by `reopen_hold`.
- **When:** 2026-11-23 1:14 PM ET, by Factory decided (technical), from Max's words on #1404 ("Factory should be able to que rebase jobx")
- **Source:** plan https://github.com/maxharris1/factory/pull/1405#issuecomment-7105745815 (trigger and job shape), plan https://github.com/maxharris1/factory/pull/1433#issuecomment-7109864473 for #1407 (states, `round`, Linear hold), plan https://github.com/maxharris1/factory/pull/1438#issuecomment-7110064377 for #1436 (model scope)

## Why

Before #1404 the factory could not queue a rebase. Under the cap, a go on a handed-off ticket became the reopen fix, whose brief runs `git merge origin/<base>` (#1197). So aut0 #1446 and #1281 went to Cursor cloud agents.

**Its own kind, not `go rebase=true`.** `parse_header_line` refuses a kind it does not know, so a dispatcher that misses `rebase` queues nothing. A `rebase=true` field on a go would reach `_t1` first, and `_t1` runs the cap lift, `_resume_after_cap`, the reopen and `go-after-handoff` before it reads any field. A separate kind keeps the reopen go unchanged by construction (#1404 ac-4). `kind=retry` is the precedent. It has its own handler and is listed in `CORE_KINDS`, so hooks never see it. `rebase` follows it, with its handler `_t_rebase` in `lib/dispatch/rebase.py`.

**A `fix` job with `detail.kind="rebase"`.** This mirrors the merge-only fix that `merge_queue.py` `_base_update_request` already queues. A `fix` already settles on `ready` or `questions` and already counts in retry, the dashboards, the CLI and the eval reports. A new queue action would touch about 12 hard-coded action lists and add a statechart state, and no reader would see a difference. The job detail's `rebase` block is the only record of the rebase, so `ticket_state` gets no new column. A rebase changes no behavior, so it is not a review round. Queueing it writes neither `review_round` nor `cap_extra`, and it is accepted under, at and over the cap. A `rebase_rounds` credit was rejected because it adds two columns and moves `_effective_cap` for every caller. Review jobs already use `detail["rebase"]` for the range-diff scope `{last_reviewed_head, reviewed_base, base}`. `_with_evidence` and `_review_bases` now read that key only from review jobs, so a rebase fix cannot raise `KeyError` at enqueue.

**`round` is the `review_round` the handler read.** The block is `rebase={from_state, from_head, round}`. `round` is a recorded fact, not a rule. Part 3 of #1404 (#1408) owns the round `R` at which the rebased head is reviewed, and it computes `R` from `from_state` and this value. The job's display `round=n/cap` stays `review_round + 1`, like every fix.

**A Linear hold in place of `reopen_hold`.** `reopen_hold` belongs to the reopen (#1196, #1197, from #1191), and a rebase must not look like a reopen, so it never sets it (#1407 ac-3). Without a hold, `linear_move` would move a `fixing` ticket whose Linear state is a handoff name, such as Ready for Review on aut0, to `handed-off-externally`. Instead `_apply_linear_moves` passes a hold while the ticket's newest harness job is a `fix` with `kind="rebase"`, read from the `jobs` table. `linear_move` and the schema do not change, and there is no stored flag to clear. Linear Done and Canceled still settle the ticket, since `linear_move` reads them before the hold. The hold ends once a newer harness job is queued. Until #1408 widens it, a handed-off Linear ticket's re-review after a rebase can still be parked.

The Linear hold is code-level and is not in the TLA+ model. `HandedOffExternallyDecided` keeps today's rule. Modelling the hold needs a new flag cleared on every exit from `fixing` and on every CI-red or retry fix. That is about eight action edits and doubles the drawn states, for a rule no criterion of #1436 names. A follow-up ticket can add it.

## Outcomes

A trusted `kind=rebase` is `_trusted` for grok-bot as orchestrator, or `_approver_go`. On a tracked PR in `review`, `fixing`, `awaiting-ok`, `capped`, `handed-off`, `merge-queue` or `waiting-external` with no harness job active, one `BEGIN IMMEDIATE` transaction queues one `fix` with `detail.kind="rebase"` at the PR head, keyed `t-rebase:<scope>:<comment id>`. It writes no `cap_extra`, `review_round` or `reopen_hold`, and moves the ticket to `fixing` (source `rebase-queued`). From `merge-queue` or `waiting-external` it also clears the approval and cancels a queued `update-from-main`. It records one decision with question `rebase:<comment id>` and choice `Rebase onto origin/<base> queued`, where `<base>` is `factory.md` `base_branch`. The `handled_comments` outcome is one of these:

| Outcome | When |
| --- | --- |
| `queued-rebase` | The rebase fix is queued. A replay finds its job by the key and queues nothing more. |
| `rejected-rebase-author` | The author is neither trusted nor an approver. Read first, and nothing is written. |
| `rejected-rebase-no-pr` | The ticket has no open PR head. |
| `rejected-rebase-state` | The state is not one of the seven above. |
| `rejected-rebase-job-active` | A harness job of the ticket is queued or running. |

A refusal writes no decision row and queues no job. Like every `rejected-*` outcome, it wakes Grok Bot once through `_wake_comment_problem`. A queued rebase wakes no one. Its fix is the builder's job.

`model/Orchestrator.tla` encodes the accepted path. `RebaseQueued` goes from any state in `RebaseFrom` to `fixing` with `counts.round` and `counts.extra` unchanged. The `RebaseDecided` invariant says it is enabled from every state in `RebaseFrom`, and the `RebaseSpendsNoRound` property says it never moves the round or the extra. `model/fixtures/omit-trb.cfg` shows `RebaseDecided` breaks without it. The chart gains one new pair, `merge-queue → fixing`.

## A conflict parked on Max (#1409)

The rebase brief's conflict path posts a builder `questions` that lists the files, with the branch still at the PR head. T5 parks it as any question: `waiting-max`, `paused_action="fix"`, `paused_head` the unchanged head, and Grok woken. When the ticket's newest harness job is the rebase fix, the pause also records one decision in the same transaction: `answer_id` and `question_id` are the questions comment, the question is `rebase-conflict:<id>`, and the choice is `Conflict: waiting on Max; files: <a>, <b>`. The files are the comment's list items, each item's first backticked span or its text when that has no space. An answer resumes the paused step through `_resume_step`, which reads the paused job's detail: a `kind="rebase"` detail queues the same rebase fix, with the original `rebase` block and no `ci_failure`, so a red head does not turn it into a CI fix.

## Not changed

- The under-cap handed-off `kind=go` still reopens with `kind=reopen`, `reopen_hold` and the merge-only brief. `lib/dispatch/caps/t1.py`, `caps/resume.py` and `lib/brief/fix.py` are untouched.
- A rebase on a stuck ticket is still `ticket-stuck`. `retry` is the way out of stuck.
- `waiting-max`, `handed-off-externally`, `building` and `planning` refuse a rebase. A `waiting-max` paused on a rebase conflict goes on through Max's answer, not a new rebase (#1409).
- The ready after a rebase and its re-review at round `R` are #1408.
