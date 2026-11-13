# Plan for #130: watch approved PRs until they merge, with a factory.md finish line

Today an APPROVE ends the factory's work. T4 writes `handed-off`, and nothing watches the PR after that. If main moves and the PR conflicts, no update job runs, no re-review runs, and a `go` is recorded as `go-after-handoff` and does nothing. #111 and #116 are stuck that way now.

This plan adds a per-project `finish_line` to `factory.md`:

- `ready-for-review` keeps today's handoff.
- `merge` keeps watching the approved PR until it merges or closes. While it waits, the factory updates the PR from base when it conflicts, merges approved PRs one at a time, and honors a `go`.

One PR (#131, branch `factory/issue-130`). Codex Bot reviews it (risk high, up to three rounds).

## How to read this

One box is one unit of work. Every box names the evidence that checks it. Check a box only when that evidence exists: a test run, a log line or a SHA.

The build job runs under poteto-mode's feature playbook. It is one backend PR with no UI work beyond state names on the dashboard, so the multi-lane skeleton does not apply. Its verification rule does: see **Verify**.

## Unit 0: start from #113, by merge commit

#113 (issue #108, branch `factory/issue-108`) has not merged. This ticket builds on its lifecycle work: `FINISHED_STATES`, `STOPPED_STATES`, `_enter_merged`, `_reenter_after_handoff`, the `_tracked_refs` rewrite and `allow_merged`. Every line reference below is to #113's tree.

- [ ] Merge `origin/factory/issue-108` into `factory/issue-130` with a merge commit. Do not rebase or force-push. Check that main's `docs/decisions/README.md` (#120) survives the merge, since #113 branched before #120. When #113 lands, merge `origin/main` the same way. Evidence: the merge SHAs, and `git log --merges` on the branch.

If #113's code changes before it lands, merge it again. Do not copy its code by hand.

## Decisions this plan makes

The ticket and Max's words settle the product. These five calls are technical, and each one gets its own file in `docs/decisions/` (the SPEC decision log is frozen, #120). If Max disagrees with any of them, they are cheap to change before the build.

1. **The poller merges a `ship: handoff` repo once `finish_line: merge` is set.** The ticket asks for this repo to be "watched through auto-update and merge". On APPROVE, `ship: handoff` still runs its usual action: mark the PR ready and send the `done` wake. After that the watch merges the PR (a squash, the only merge built). The `done` wake says the poller will merge, so Factory does not also merge by hand. A merge by hand is still harmless, because merged is absorbing.
2. **`ship: merge` with `finish_line: ready-for-review` is refused by `check-config`.** `ship: merge` merges on APPROVE, so it contradicts a finish line that never auto-merges. `ship: after:<approver>` with `finish_line: merge` is allowed: the watch updates the PR, and the merge still waits for that approver's `ok` on the current head.
3. **The re-review after a non-trivial update reuses the approved round.** After T4, `review_round` is the approved round minus one, so the re-review job is round `review_round + 1`, the same number as the approval it replaces. A Codex REVISE then follows the normal fix loop and cap. This matches the existing rule that gate retries are not review rounds.
4. **The merge queue moves one ticket per repo at a time.** Only the head of a repo's queue (oldest covering APPROVE) is merged or gets an update job. Every other ticket waits in `merge-queue`, so two approved PRs cannot keep re-conflicting each other. A head that goes stuck leaves the queue, so the next ticket moves up.
5. **The poller decides whether an update is trivial, never the builder.** It checks the update against the bare clone the waiter already keeps per repo (`lib/worktrees.py`, `<root>/clones/<owner>/<name>.git`). Any error, unknown conflict kind or doubt counts as non-trivial, so the update gets a re-review.

## The data shape

| Fact | Where it lives | Written by | Read by |
| --- | --- | --- | --- |
| `finish_line` | `FactoryConfig.finish_line` (`"ready-for-review"` or `"merge"`, resolved from `ship` when omitted) | `lib/factory_md.py` `_finish_line` | T4, the queue step, `_t1`, `_t2`, status |
| Watched states | `merge-queue` (approved, waiting to merge) and `updating` (an update-from-main job is out), in `lib/handoff.py` `STATES`, the `orch:state/` label and SPEC section 4.1 | T4, the queue step, T2 | `_tracked_refs` (polled every cycle, not in `STOPPED_STATES`), status, dashboard |
| Covering approval | New `ticket_state` columns `approved_head TEXT`, `approved_at REAL` (the APPROVE comment's `created_at`), `approve_comment_id TEXT`. They are added by the existing `PRAGMA table_info` migration in `Dispatcher._initialize` | T4 on entry to `merge-queue`. A trivial update moves `approved_head` to the new head and keeps `approved_at` | The queue step's merge gate and ordering |
| Carried approval note | `handled_comments.outcome = "approval-carried:<old>-><new>"` on the update job's `ready`, plus a `log` line | T2 in `updating` | Status, the replay tests, a person reading the database |
| Update failures | New `ticket_state` columns `update_failures INTEGER NOT NULL DEFAULT 0` and `update_from_head TEXT` | The queue step, T2 and T6 | The two-failure stop |
| PR mergeability and closed state | New `TrackedTicket` fields `mergeable: bool \| None`, `mergeable_state: str \| None`, `closed: bool`, read by `lib/poll_source.py` from the pull JSON it already fetches | `poll_source._github_ticket` | The queue step, `process` |

Everything stays in `orch.db`: no second state store. Dedupe keys, `generation` and `orch:v1` comments keep their current shape.

## Criterion-to-work map

| # | Acceptance criterion | Units | Tests (new files) |
| --- | --- | --- | --- |
| AC1 | After a covered APPROVE, a `finish_line: merge` ticket stays watched in an approved / merge-queue state until its PR merges or closes. `handed-off` is no longer the finish line for those tickets. | 1, 2, 3, 9 | `tests/test_merge_queue.py`: approve enters merge-queue, stays polled every cycle, merge leaves through `_enter_merged`, close leaves as `abandoned` |
| AC2 | When base moves, an approved PR that is mergeable, has green CI and has a covering approval is merged automatically, respecting `ship`, `hold:` and `finish_line`. Merges are serialized, oldest approval first. | 4 | `tests/test_merge_queue.py`: merge gate, hold on `ship: merge`, `after:` waits for ok, two tickets merge oldest-first across two cycles, one merge per repo per cycle |
| AC3 | An approved PR that conflicts gets a Claude update-from-main job on the ticket branch, with no human reopening the ticket. | 5, 6 | `tests/test_update_from_main.py`: a dirty queue head queues one `update-from-main` job for the builder harness, deduped. `tests/test_brief.py`-style check in a new `tests/test_update_brief.py` |
| AC4 | A trivial update (conflict-marker resolution only, no test or logic change) carries the approval forward with a recorded note and queues no re-review. | 7, 8 | `tests/test_update_check.py` (real git repos in `tmp_path`): one-side-per-hunk resolutions are trivial. `tests/test_update_from_main.py`: trivial gives `approval-carried`, no review job, and a merge once CI is green |
| AC5 | A non-trivial update (any other change against the approved head) queues a cross-family re-review, and ship waits for a new covering APPROVE. | 7, 8 | `tests/test_update_check.py`: mixed-line resolution, extra edit, extra file, rename/delete conflict and git failure are all non-trivial. `tests/test_update_from_main.py`: a review job for the reviewer harness at the approved round, state `review`, no merge until the new APPROVE |
| AC6 | Two failed update-from-main attempts on the same ticket stop and ask Max. They do not loop. | 6 | `tests/test_update_from_main.py`: job failed then T6 retry failed gives stuck `update-failed-2` and one Grok `stuck` wake. A ready that leaves the PR still conflicting counts as a failure. No third job is queued |
| AC7 | A trusted go comment on an approved / merge-queue ticket is honored. Dedupe, generation and handoff comments stay intact. | 10 | `tests/test_go_on_merge_queue.py`: a `go state=building` queues a build, leaves the queue and clears `approved_head`. A replayed go is deduped. An untrusted go is rejected. A go on a `ready-for-review` `handed-off` ticket still gives `go-after-handoff` |
| AC8 | `factory.md` accepts `finish_line: ready-for-review \| merge`, validates it, and documents the default (inherit from `ship`). This repo sets `finish_line: merge`. `finish_line: ready-for-review` (Aut0) still stops at Ready for Review and never auto-merges. | 1, 11, 12 | `tests/test_finish_line_config.py`: the defaults, an unknown value refused, the `ship: merge` contradiction refused, this repo's factory.md parses to `merge`. `tests/test_merge_queue.py`: a Linear / `handoff` ticket goes `handed-off`, moves the Linear issue as today, never calls `merge` and never queues an update |
| AC9 | Tests in new files fail on revert. SPEC and the example factory.md are updated. | 12, 13 | Verify V2 (revert run). `check-config` on the example and on this repo |

## Units

### Unit 1: `finish_line` in the schema (`lib/factory_md.py`)

- Add `"finish_line"` to `_TOP_KEYS`, and `finish_line: str` to `FactoryConfig`, placed after `ship`. Give it no default, so every constructor in the tests has to state it. If that change is too wide, use a `"ready-for-review"` default and a parse-time resolution, and say which one was chosen in the PR body.
- `_finish_line(data, ship, findings)`:
  - If the key is missing, return `"merge"` when `ship == "merge"`, else `"ready-for-review"`.
  - A non-string or unknown value is an error: `finish_line must be ready-for-review or merge`.
  - `ship: merge` with `finish_line: ready-for-review` is an error (Decision 2).
- `orch check-config` already reports every finding from `load_factory_md`. Nothing new is needed there beyond the findings.
- Evidence: `tests/test_finish_line_config.py` passes, and `bin/orch check-config factory.md docs/factory.example.md` is clean.

### Unit 2: states and labels (`lib/handoff.py`, model, eval report)

- Add `"merge-queue"` and `"updating"` to `STATES`, after `"handed-off"`. `GitHubReviewActions.set_state` already validates against `STATES`, so the labels `orch:state/merge-queue` and `orch:state/updating` follow from that.
- Add both to SPEC section 4.1's mermaid diagram:
  - `Ship --> MergeQueue: APPROVE, finish_line merge`
  - `MergeQueue --> Merged`
  - `MergeQueue --> Updating: conflicts with base`
  - `Updating --> MergeQueue: trivial update`
  - `Updating --> Review: non-trivial update`
  - `Updating --> Stuck: two failed updates`
  - `MergeQueue --> Planning / Building: trusted go`
  - `MergeQueue --> Abandoned: PR closed unmerged`
- Add the same states and transitions to `model/Orchestrator.tla`, `model/chart.mmd` and `model/view.mjs`, and to `CHART_TRANSITIONS` in `lib/eval_report.py`. `model/check.sh` requires the drawings to match the section 4.1 names.
- Add a model invariant like #113's `HandedOffDecided`: a ticket in `merge-queue` can always reach `merged`, `abandoned`, `updating` or `stuck`.
- Evidence: `bash model/check.sh` passes, and the omit-T1 fixtures still fail as designed.

### Unit 3: T4 enters the merge queue (`lib/dispatcher.py` `_t4`, ~1977–2093)

- When `ticket.factory.finish_line == "merge"`, after the hold gate and `_prepare_ship_intent`:
  - **`ship: handoff`:** run `actions.handoff` as today (mark ready). Complete the ship intent with state `merge-queue` instead of `handed-off`, write `approved_head`, `approved_at` and `approve_comment_id`, label `merge-queue`, and send the `done` wake with the note "finish_line merge: the poller merges this PR; do not merge by hand".
  - **`ship: merge`:** do not merge inside T4. Complete the intent as `merge-queue` and let the queue step merge, so even the first merge is serialized. The `is_merged` check stays: a PR already merged still completes as `merged`.
  - **`ship: after:<approver>`:** keep `await_approver` and `waiting-external`, and also record `approved_*`. The queue step treats `waiting-external` with `finish_line: merge` as a queue member whose merge gate is the approver's existing `ok` path. It updates the PR when it conflicts, but never merges on its own.
- `_ship_state(policy)` takes `finish_line` into account, and so does `_replay_ship_intent`'s label write, so a replayed intent relabels `merge-queue`, not `handed-off`.
- `finish_line: ready-for-review` keeps every line of today's T4 path, including the Linear Ready for Review move in `LinearReviewActions.handoff`.
- Evidence: `tests/test_merge_queue.py::test_approve_enters_merge_queue_*` (all three ships) and `::test_ready_for_review_still_hands_off`.

### Unit 4: the queue step (`lib/dispatcher.py`, new `_advance_merge_queues`)

Call it once per cycle in `poll_once`, after the `process` loop and before `reconcile_jobs`. For each repo, the queue members are its tickets in `merge-queue`, plus `waiting-external` tickets with `finish_line: merge`. Order them by `approved_at`, then `ticket_id`. Only the head of each queue is acted on (Decision 4).

The head is read fresh through `self._fresh_ticket`, which costs one pull read per repo per cycle, because GitHub computes `mergeable` lazily. Then:

1. **Merged:** `_enter_merged` through `record_terminal` (#108's path), the `merged` label, and a `done` wake.
2. **Closed unmerged:** set `abandoned`, cancel queued non-Grok jobs as `_enter_merged` does, label `abandoned`, and log it. Do not touch #108's merged cleanup.
3. **`mergeable is None`:** wait. GitHub is still computing.
4. **`mergeable_state == "dirty"` (conflict):** go to Unit 5.
5. **Otherwise, the merge gate.** All of these must hold:
   - `current_head` is covered: it equals `approved_head`, or has an equal `stable_patch_id` when the adapter builds one (today the GitHub adapter raises, which counts as not covered, the same as T4).
   - CI on `current_head` is `success`.
   - `ship: merge` with `hold:` passes `_hold_gate` on the current head.
   - `after:<approver>` has the approver's `ok` on the current head. If not, wait.
   - `finish_line == "merge"`. This is checked again here, so a `factory.md` edited back to `ready-for-review` mid-queue stops the merge.

   If all hold, merge with `_ship_head(ticket, current_head)`: squash, `match_head_commit` pinned. That is at most one merge per repo per cycle. A merge refused as retryable (405, a 409 head move) waits for the next cycle. A non-retryable refusal sends a `ship-failed` wake once per head. Then `_enter_merged` and a `done` wake.
6. **Head not covered and not from an update** (the builder pushed by hand): run #108's re-entry, in Unit 8.

Write the "covered" check as a helper, `_covers(ticket, approved_head, head)`, shared with T4, so the two rules cannot drift.

Evidence: `tests/test_merge_queue.py`, covering:

- merge when green, covered and clean
- no merge on red, pending or uncovered CI
- `hold` blocks on `ship: merge`
- `after:` waits for `ok`
- two tickets in one repo: the older approval merges first, the younger only on a later cycle
- tickets in two repos each merge in the same cycle
- a ticket closed unmerged becomes `abandoned`
- `ready-for-review` never reaches this step

### Unit 5: the update-from-main job (`lib/queue.py`, `lib/brief.py`, dispatcher)

- Add `"update-from-main"` to `ACTIONS`. Map it to the brief kind `merge-only` in `lib/brief.py` `_ACTION_KINDS`. Its task section is the existing merge-only text: fetch base, `git merge origin/<base>` as a merge commit only, resolve conflicts without behavior changes, run the tests, push, then post `ready` with the pushed head. Add one line telling the builder to resolve each conflict hunk by taking one side when it can, since that keeps the approval.
- The queue step queues it for a dirty head:
  - recipient `ticket.factory.builder_harness`, current generation, `head = current_head`
  - `detail` from `_job_detail(action="update-from-main", head=..., round_number=approved round)`
  - `dedupe_key = f"t-update:{scope}:{current_head}:{update_attempt}:g{generation}"`
  - set `updating` and its label, and set `update_from_head = current_head`
- `_state_for_action` maps `update-from-main` to `updating`.
- Evidence: `tests/test_update_from_main.py::test_dirty_head_queues_one_update_job` (the second cycle queues nothing) and `tests/test_update_brief.py` (the rendered brief names `merge-only`, the base and the head, and has the `ready` header).

### Unit 6: two failures stop and ask Max

An update attempt fails when any of these happens:

- T6 sees the `update-from-main` job fail (its existing one retry is the second attempt)
- the job's `ready` names a head that is still `dirty`
- the job finishes `done` and no `ready` with a new head arrives

Each failure increments `update_failures` and counts against the `update_from_head` the attempt started from. On the second failure:

- `_mark_stuck_and_wake` with reason `update-failed-2` and detail "update-from-main failed twice on <head>: ask Max"
- no third job

A successful update (a new head that is not `dirty`) resets `update_failures` to 0. The T6 path for this action runs the same counter instead of the generic `job-failed-2`, so one failure is never counted twice.

Evidence: `tests/test_update_from_main.py::test_two_failed_updates_stop_and_wake_once` (both failure paths) and `::test_successful_update_resets_failures`.

### Unit 7: the trivial-update check (new `lib/update_check.py`)

`classify(clone: Path, approved_head: str, new_head: str, base_ref: str) -> UpdateClass(trivial: bool, reason: str)` uses git plumbing only (git ≥ 2.38 for `merge-tree --write-tree`; the box has 2.47).

1. `new_head` must have exactly two parents: P1 is `approved_head`, and P2 is an ancestor of the fetched `base_ref` tip. Anything else is non-trivial: `"not a merge of the approved head and base"`.
2. Run `git merge-tree --write-tree --name-only --messages -z P1 P2`. If it is clean, the update is trivial only when `tree(new_head) == the merge-tree result`.
3. Otherwise, every conflicted path must be a text content conflict, with stages 2 and 3 present and no rename, delete, mode or binary conflict. Anything else is non-trivial.
4. For each conflicted path, run `git merge-file -p --marker-size=61` over the stage 1, 2 and 3 blobs (an empty base for add/add). Parse the hunks and check that the new head's blob equals the shared text with each conflict hunk replaced by exactly its ours side or exactly its theirs side. This is a linear scan with backtracking only between the two sides of one hunk.
5. Every path that is not conflicted must match the merge-tree result exactly. `git diff --name-only <merge-tree> new_head` minus the conflicted paths must be empty: no test, logic or other edit, and no new file.
6. Any `CalledProcessError`, timeout or parse failure makes the update non-trivial (fail closed).

Clone access is a small public helper in `lib/worktrees.py`, `fetched_clone(root, repo, refs, url_template)`. It reuses `_ensure_clone`, `_clone_lock` and `check_remote_url`, fetches the three refs and returns the bare clone path. No credentials are read or passed: git authenticates the usual way, as for job worktrees.

Evidence: `tests/test_update_check.py` builds real repos in `tmp_path` and covers:

- clean merge, no edit: trivial
- one conflict, ours: trivial
- one conflict, theirs: trivial
- two hunks, ours and theirs: trivial
- a hand-merged mixed line: non-trivial
- an extra edit outside the conflict: non-trivial
- an extra file: non-trivial
- modify/delete conflict: non-trivial
- first parent not the approved head: non-trivial
- a rebased (single-parent) head: non-trivial
- git missing or failing (monkeypatched): non-trivial

### Unit 8: the update's ready (`_t2`)

Generalize #113's `state == "handed-off"` branch into a watched-state branch:

- **State `updating` and a trusted builder `ready`:**
  - If the head is stale, or CI is pending, wait as today.
  - If the PR is still `dirty`, count it as an update failure (Unit 6).
  - Otherwise run `classify`.
  - **Trivial:** set `approved_head` to the new head and keep `approved_at`. Set `merge-queue` and its label, and mark the ready handled as `approval-carried:<old>-><new>`. Log `approval carried`. Queue no review. The queue step merges once CI is green on the new head.
  - **Non-trivial:** clear `approved_head`, then `_queue_review` at round `review_round + 1` (Decision 3) with `head = new head`. Set `review` and mark the ready `update-rereview`. The next APPROVE goes through T4 and re-enters `merge-queue` with a fresh `approved_at`.
  - CI red on the new head goes through T2's existing CI-fix path. Those are gate retries; three red heads mark the ticket stuck.
- **State `merge-queue` and a builder `ready` on a new head:** this is #108's re-entry. Clear `approved_head` and re-enter review within the cap with `_reenter_after_handoff`, widened to accept `merge-queue`.
- In `_process_comments`, the "verdict after handoff wakes nobody" guard (~697) applies to `handed-off` only, not to the watched states, so the re-review's APPROVE is processed.

Evidence: `tests/test_update_from_main.py`, covering:

- `test_trivial_update_carries_approval_and_merges`: no review job queued, the `approval-carried` outcome row exists, and a merge on the next cycle with green CI
- `test_nontrivial_update_queues_rereview_at_approved_round`: a review job for `codex`, and no merge on the next cycle
- `test_rereview_approve_reenters_queue_and_merges`

### Unit 9: polling and terminal sets

- `_tracked_refs`: `merge-queue` and `updating` are not in `STOPPED_STATES`, so they are polled every cycle. `waiting-external` is unchanged.
- `allow_merged` stays as #113 has it. The merge of a watched PR is seen by the normal poll through `terminal_sink`, or by the queue step's fresh read.
- `poll_source._github_ticket`: fill `mergeable`, `mergeable_state` and `closed` (`pull["state"] == "closed" and not merged`) from the pull JSON it already reads. No new API call.
- `lib/linear_webhook.py` `_TERMINAL_STATES` stays `("merged", "handed-off", "done", "abandoned")`. The watched states are not added. Add a test that a `merge-queue` Linear ticket is still re-read. No Linear project uses `finish_line: merge` today.
- `lib/status.py` and `lib/dashboard.py`: list `merge-queue` and `updating` as active states. They already are by default, because only `merged` and `done` are filtered out, but give them labels and lanes, and count `update-failed-2` like the other stuck reasons. Show the queue position (`1 of 2`) beside the state.
- Evidence: `tests/test_merge_queue.py::test_merge_queue_polled_every_cycle` and `tests/test_merge_queue_status.py` (status and dashboard rows name the state and the queue position).

### Unit 10: go on a watched ticket (`_t1`)

- Keep `go-after-handoff` for `handed-off`, which is still `ready-for-review`'s finish line.
- For `merge-queue`, `updating` and `waiting-external` with `finish_line: merge`, the go runs `_t1`'s normal path: the same trust check, `_resume_after_cap`, generation, dedupe key and `_answered_by`. It queues the plan or build. It also clears `approved_head`, `update_failures` and `update_from_head` in the same transaction that sets `planning` or `building`, so the ticket leaves the queue and ship waits for a new APPROVE. A queued `update-from-main` job for the ticket is cancelled, with reason `go`.
- Evidence: `tests/test_go_on_merge_queue.py` (queues the job, leaves the queue, the replay is deduped, the untrusted go is rejected, a go on `handed-off` with `ready-for-review` is unchanged).

### Unit 11: this repo's `factory.md`

- Add `finish_line: merge`. Rewrite **Ship**: "`ship: handoff`, `finish_line: merge`. On APPROVE of the covered head with green CI, the poller marks a draft PR ready, labels it `orch:state/merge-queue` and wakes Grok Bot with a `done` wake. The poller then watches the PR. It brings main in with a Claude update-from-main job when it conflicts, re-reviews any update that is more than taking one side of each conflict, and squash-merges approved PRs oldest-first. Do not merge by hand." Leave Aut0's `factory.md` alone.
- Evidence: `tests/test_finish_line_config.py::test_this_repo_finish_line_merge` reads the real `factory.md`.

### Unit 12: SPEC, example, decisions

- `docs/SPEC.md`:
  - section 4.1: states and the diagram
  - section 4.2: T4's finish-line branch, plus a new **TQ** row for the queue step and the update classification
  - section 4.3: the invariants "one merge per repo per cycle", "the poller classifies updates" and "fail closed to re-review"
  - section 6: a `finish_line` row in the optional list, with its values, the default inherited from `ship` and the `ship: merge` contradiction
  - section 8: the merge rules for a carried approval
  - section 9: `update-failed-2`
- `docs/factory.example.md`: show `finish_line` commented with its default.
- `docs/decisions/130-finish-line.md`, `130-update-rereview-round.md`, `130-merge-queue-head-only.md`, `130-trivial-update-check.md` and `130-handoff-repo-poller-merges.md`, using the README template. When Max's words are the source, cite his 3:59 PM ET message on 2026-11-13.
- Evidence: the diff, and `check-config` clean.

### Unit 13: tests fail on revert

All new tests live in new files:

- `test_finish_line_config.py`
- `test_merge_queue.py`
- `test_merge_queue_status.py`
- `test_update_from_main.py`
- `test_update_check.py`
- `test_update_brief.py`
- `test_go_on_merge_queue.py`

`tests/test_dispatcher.py` is not touched. Existing tests that construct `FactoryConfig` get `finish_line` only where the schema requires it.

## Verify

Tests alone are not enough. The PR is verified only when all four boxes are checked.

- [ ] **V1, unit:** `env -u ORCH_DB python -m pytest -q` is green on the final head (the count in the PR body).
- [ ] **V2, revert:** check out the base merge commit's `lib/` with the new test files kept. Every new test file fails. Paste the failing count.
- [ ] **V3, replay:** a poll-cycle replay in `tests/test_update_from_main.py`, shaped like #113's `test_lifecycle_replay.py` and modeled on #111. Approve, then base moves, the PR goes dirty, an update job runs, a trivial ready arrives, the approval is carried, CI goes green and the PR merges, with no human comment. A second replay runs the non-trivial path to a re-review and a merge after the new APPROVE. These use fakes only: #111 and #116 themselves are not touched.
- [ ] **V4, CI:** `ci.yml` is green on the PR head: pytest, `check-config` and the statechart check.

## Out of scope (from the ticket)

- #111 and #116 themselves.
- Aut0's `factory.md`, or any auto-merge on `ready-for-review`.
- Replacing `ship:`.
- #128/#129, #112, #122/#126, #100, the UI animation work.
- Restarting the poller. After merge, a restart goes through `deploy/run-poller.sh` only, and only if Max asks.

## Risks the reviewer should press on

- **Merge without a fresh CI run on the new base.** The gate is GitHub's `mergeable` plus green CI on the PR head, as the ticket specifies. A clean merge onto a moved base is not re-tested before merging. Branch protection that requires up-to-date branches turns this into a 405, which waits for the next cycle. That is not a loop, because the head is unchanged and no job is queued.
- **Clone access from the poller.** The poller has not used the bare clones before. The clone lock serializes it with waiter jobs, and a failure fails closed to re-review.
- **Head-of-line blocking.** A queue head that waits on pending CI or an approver's `ok` holds the repo's queue. That is the price of serial merges. A stuck head leaves the queue.
