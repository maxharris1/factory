---- MODULE Orchestrator ----
\* Ticket loop from SPEC section 4.
\* State names are the orch:state labels (kebab-case). T1 is the kind=go handoff.
\* T9 (post-merge) is in this module. Phase 1 config sets AllowPostMerge to FALSE.

EXTENDS Integers

CONSTANTS AllowPostMerge, Omit

VARIABLES state, paused, cap, configOk, flags, counts, qa

vars == <<state, paused, cap, configOk, flags, counts, qa>>

States == {
  "intake", "refused", "questions", "waiting-max", "planning", "plan-check",
  "building", "review", "fixing", "stuck", "capped", "blocked", "ship",
  "merged", "handed-off", "merge-queue", "updating", "waiting-external",
  "awaiting-ok", "post-merge", "done", "abandoned"
}

\* handed-off is not terminal (#108): the poll still watches it, for a merge
\* and for a trusted ready that re-enters review inside the cap. merge-queue
\* and updating (#130) are watched the same way, every cycle.
Terminal == {"refused", "stuck", "abandoned", "done"}

\* Every state a tracked PR can be in when the poll sees it merged (#108).
\* Intake and questions have no PR yet; merged, post-merge and done are past it.
MergeFrom == {
  "planning", "plan-check", "building", "review", "fixing", "capped", "blocked",
  "ship", "awaiting-ok", "waiting-external", "stuck", "handed-off",
  "merge-queue", "updating"
}

\* States whose job T6 times out. post-merge is not here because T9 is not
\* built: no post-merge job exists to time out. TODO(T9): when T9 is
\* scheduled, add post-merge here and PostMerge --> Stuck to section 4.1 and
\* CHART_TRANSITIONS.
JobStates == {"planning", "building", "review", "fixing"}

AfterGo == {
  "planning", "plan-check", "building", "review", "fixing", "capped", "blocked",
  "ship", "merged", "handed-off", "merge-queue", "updating", "waiting-external",
  "awaiting-ok", "post-merge", "done", "stuck"
}

ReviewAndAfter == {
  "review", "fixing", "capped", "blocked", "ship", "merged", "handed-off",
  "merge-queue", "updating", "waiting-external", "awaiting-ok", "post-merge",
  "done"
}

\* Past ship: the head these states hold is covered (or merged by hand). In
\* ship and awaiting-ok a new head can uncover it (T4HeadMoves).
Covered == {
  "merged", "handed-off", "merge-queue", "updating", "waiting-external",
  "post-merge", "done"
}

Decided == {"T1", "T1P", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9"}

Guards == {"ShipGuard", "QaGuard"}

QaRuns == {"none", "running", "pass", "fail", "env-failed", "not-required"}

ASSUME AllowPostMerge \in BOOLEAN
ASSUME Omit \subseteq Decided \cup Guards

TypeOK ==
  /\ state \in States
  /\ paused \in {"none", "questions", "building", "fixing", "review", "ship"}
  /\ cap \in {0, 1, 3}
  /\ configOk \in BOOLEAN
  /\ flags \in [
       covered: BOOLEAN, planOk: BOOLEAN, questionsDone: BOOLEAN, byHand: BOOLEAN
     ]
  /\ counts \in [
       round: 0..4,
       acUnmet: 0..2,
       gate: 0..3,
       planGaps: 0..2,
       deaths: 0..2,
       declines: 0..3,
       extra: 0..1,
       updates: 0..2
     ]
  /\ qa \in [gate: BOOLEAN, run: QaRuns, envFails: 0..2]

Init ==
  /\ state = "intake"
  /\ paused = "none"
  /\ cap = 0
  /\ configOk \in BOOLEAN
  /\ flags = [
       covered |-> FALSE, planOk |-> FALSE, questionsDone |-> FALSE,
       byHand |-> FALSE
     ]
  /\ counts = [
       round |-> 0, acUnmet |-> 0, gate |-> 0,
       planGaps |-> 0, deaths |-> 0, declines |-> 0,
       extra |-> 0, updates |-> 0
     ]
  /\ qa \in [gate: BOOLEAN, run: {"none"}, envFails: {0}]

QaReset == [qa EXCEPT !.run = "none", !.envFails = 0]

QaFollowsHead == qa' = IF flags'.covered THEN qa ELSE QaReset

QaCovers == ~qa.gate \/ qa.run \in {"pass", "not-required"}

\* Valid config: brief posted, questions step starts. Invalid config: refused.
IntakeBrief ==
  /\ state = "intake"
  /\ configOk
  /\ state' = "questions"
  /\ paused' = "none"
  /\ cap' = cap
  /\ configOk' = configOk
  /\ flags' = flags
  /\ counts' = counts
  /\ QaFollowsHead

\* T7. Config refused. Wake Grok Bot once.
IntakeRefused ==
  /\ "T7" \notin Omit
  /\ state = "intake"
  /\ ~configOk
  /\ state' = "refused"
  /\ paused' = "none"
  /\ cap' = cap
  /\ configOk' = configOk
  /\ flags' = flags
  /\ counts' = counts
  /\ QaFollowsHead

\* T5. A questions comment sets orch:state/waiting-max. Not a review round.
T5Ask ==
  /\ "T5" \notin Omit
  /\ state \in {"questions", "building", "fixing"}
  /\ state' = "waiting-max"
  /\ paused' = state
  /\ cap' = cap
  /\ configOk' = configOk
  /\ flags' = flags
  /\ counts' = counts
  /\ QaFollowsHead

\* T5. Answers re-queue the same step.
T5Answer ==
  /\ "T5" \notin Omit
  /\ state = "waiting-max"
  /\ paused # "ship"
  /\ state' = paused
  /\ paused' = "none"
  /\ cap' = cap
  /\ configOk' = configOk
  /\ flags' = flags
  /\ counts' = counts
  /\ qa' = qa

QaParkAnswered ==
  /\ "T5" \notin Omit
  /\ state = "waiting-max"
  /\ paused = "ship"
  /\ state' = "ship"
  /\ paused' = "none"
  /\ qa' = QaReset
  /\ UNCHANGED <<cap, configOk, flags, counts>>

T5Abandon ==
  /\ "T5" \notin Omit
  /\ state = "waiting-max"
  /\ state' = "abandoned"
  /\ paused' = "none"
  /\ cap' = cap
  /\ configOk' = configOk
  /\ flags' = flags
  /\ counts' = counts
  /\ QaFollowsHead

\* T1. kind=go handoff (orch:go) after Questions. Over 3 files: queue plan.
T1KindGoPlan ==
  /\ "T1" \notin Omit
  /\ state = "questions"
  /\ state' = "planning"
  /\ paused' = "none"
  /\ cap' = cap
  /\ configOk' = configOk
  /\ flags' = [flags EXCEPT !.questionsDone = TRUE]
  /\ counts' = counts
  /\ QaFollowsHead

\* T1. kind=go handoff. 3 files or fewer: queue build.
T1KindGoBuild ==
  /\ "T1" \notin Omit
  /\ state = "questions"
  /\ state' = "building"
  /\ paused' = "none"
  /\ cap' = cap
  /\ configOk' = configOk
  /\ flags' = [flags EXCEPT !.questionsDone = TRUE]
  /\ counts' = [counts EXCEPT !.deaths = 0, !.gate = 0]
  /\ QaFollowsHead

T1PPlanPosted ==
  /\ "T1P" \notin Omit
  /\ state = "planning"
  /\ state' = "plan-check"
  /\ paused' = "none"
  /\ cap' = cap
  /\ configOk' = configOk
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.deaths = 0]
  /\ QaFollowsHead

T1PPlanGapOnce ==
  /\ "T1P" \notin Omit
  /\ state = "plan-check"
  /\ counts.planGaps = 0
  /\ state' = "planning"
  /\ paused' = "none"
  /\ cap' = cap
  /\ configOk' = configOk
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.planGaps = 1, !.deaths = 0]
  /\ QaFollowsHead

T1PPlanGapStuck ==
  /\ "T1P" \notin Omit
  /\ state = "plan-check"
  /\ counts.planGaps = 1
  /\ state' = "stuck"
  /\ paused' = "none"
  /\ cap' = cap
  /\ configOk' = configOk
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.planGaps = 2, !.deaths = 0]
  /\ QaFollowsHead

T1PPlanMapped ==
  /\ "T1P" \notin Omit
  /\ state = "plan-check"
  /\ state' = "building"
  /\ paused' = "none"
  /\ cap' = cap
  /\ configOk' = configOk
  /\ flags' = [flags EXCEPT !.planOk = TRUE]
  /\ counts' = [counts EXCEPT !.deaths = 0, !.gate = 0]
  /\ QaFollowsHead

\* #502. A mapped plan over factory.md's plan.max_lines or plan.max_criteria
\* waits for Max with the build paused; answers or an approver's go resume it.
T1PPlanOverSize ==
  /\ "T1P" \notin Omit
  /\ state = "plan-check"
  /\ state' = "waiting-max"
  /\ paused' = "building"
  /\ cap' = cap
  /\ configOk' = configOk
  /\ flags' = [flags EXCEPT !.planOk = TRUE]
  /\ counts' = [counts EXCEPT !.deaths = 0]
  /\ QaFollowsHead

keep == cap' = cap /\ configOk' = configOk /\ paused' = "none"

\* T2. ready head=<sha>, then CI on that SHA.
T2ReadyGreen ==
  /\ "T2" \notin Omit
  /\ state \in {"building", "fixing"}
  /\ state' = "review"
  /\ paused' = "none"
  /\ configOk' = configOk
  /\ flags' = [flags EXCEPT !.covered = FALSE]
  /\ counts' = [counts EXCEPT !.gate = 0, !.deaths = 0]
  /\ \/ /\ cap = 0
        /\ cap' \in {1, 3}
     \/ /\ cap \in {1, 3}
        /\ cap' = cap
  /\ QaFollowsHead

T2CiRed ==
  /\ "T2" \notin Omit
  /\ state \in {"building", "fixing"}
  /\ counts.gate < 2
  /\ state' = state
  /\ keep
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.gate = @ + 1, !.deaths = 0]
  /\ QaFollowsHead

T2CiRedStuck ==
  /\ "T2" \notin Omit
  /\ state \in {"building", "fixing"}
  /\ counts.gate = 2
  /\ state' = "stuck"
  /\ keep
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.gate = 3, !.deaths = 0]
  /\ QaFollowsHead

T2ReadyCovered ==
  /\ "T2" \notin Omit
  /\ state = "fixing"
  /\ flags.covered
  /\ state' = "ship"
  /\ keep
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.gate = 0, !.deaths = 0]
  /\ QaFollowsHead

\* T6. No handoff, or the job timeout. Retry once. Second time: stuck.
\* Section 4.1 draws that edge from Planning, Review, and Fixing, and from Building.
\* Issue #75: a dead job (its runner and worker confirmed gone) is a job
\* death here, the same as a timeout. A job its bot claimed and never started
\* is put back in the queue as the same job: the ticket's state, round and
\* deaths do not change, so that requeue is a stutter step and has no action.
T6JobRetry ==
  /\ "T6" \notin Omit
  /\ state \in JobStates
  /\ counts.deaths = 0
  /\ state' = state
  /\ paused' = paused
  /\ cap' = cap
  /\ configOk' = configOk
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.deaths = 1]
  /\ QaFollowsHead

T6JobStuck ==
  /\ "T6" \notin Omit
  /\ state \in JobStates
  /\ counts.deaths = 1
  /\ state' = "stuck"
  /\ keep
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.deaths = 2]
  /\ QaFollowsHead

\* T3. REVISE under the effective cap (cap plus Max's extra rounds) queues fix.
\* At it, or a second AC-unmet verdict: capped.
\* A REVISE also replaces a held APPROVE (T4H, issue #86).
T3Revise ==
  /\ "T3" \notin Omit
  /\ state \in {"review", "awaiting-ok"}
  /\ keep
  /\ flags' = [flags EXCEPT !.covered = FALSE]
  /\ \/ /\ counts.acUnmet = 1
        /\ state' = "capped"
        /\ counts' = [counts EXCEPT !.acUnmet = 2, !.deaths = 0]
     \/ /\ counts.round + 1 < cap + counts.extra
        /\ state' = "fixing"
        /\ counts' = [counts EXCEPT !.round = @ + 1, !.deaths = 0]
     \/ /\ counts.round + 1 >= cap + counts.extra
        /\ state' = "capped"
        /\ counts' = [counts EXCEPT !.round = cap + counts.extra, !.deaths = 0]
     \/ /\ counts.acUnmet = 0
        /\ counts.round + 1 < cap + counts.extra
        /\ state' = "fixing"
        /\ counts' = [counts EXCEPT !.acUnmet = 1, !.round = @ + 1, !.deaths = 0]
     \/ /\ counts.acUnmet = 0
        /\ counts.round + 1 >= cap + counts.extra
        /\ state' = "capped"
        /\ counts' = [counts EXCEPT !.acUnmet = 1, !.round = cap + counts.extra, !.deaths = 0]
  /\ QaFollowsHead

T3Unsat ==
  /\ "T3" \notin Omit
  /\ state \in {"review", "awaiting-ok"}
  /\ state' = "waiting-max"
  /\ paused' = "review"
  /\ cap' = cap
  /\ configOk' = configOk
  /\ flags' = [flags EXCEPT !.covered = FALSE]
  /\ counts' = counts
  /\ QaFollowsHead

\* T7. BLOCKED, checked like REVISE. No known answer. Wake Grok Bot once.
T7Blocked ==
  /\ "T7" \notin Omit
  /\ state = "review"
  /\ state' = "blocked"
  /\ keep
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.deaths = 0]
  /\ QaFollowsHead

\* T4. APPROVE ac=met head=<sha> on the current head. A later patch diff sends it back.
T4Approve ==
  /\ "T4" \notin Omit
  /\ state = "review"
  /\ state' = "ship"
  /\ keep
  /\ flags' = [flags EXCEPT !.covered = TRUE]
  /\ counts' = [counts EXCEPT !.deaths = 0]
  /\ QaFollowsHead

T4ApproveCiRed ==
  /\ "T4" \notin Omit
  /\ state = "review"
  /\ counts.gate < 2
  /\ state' = "fixing"
  /\ keep
  /\ flags' = [flags EXCEPT !.covered = TRUE]
  /\ counts' = [counts EXCEPT !.gate = @ + 1, !.deaths = 0]
  /\ QaFollowsHead

T4PatchDiffers ==
  /\ "T4" \notin Omit
  /\ state = "ship"
  /\ state' = "review"
  /\ keep
  /\ flags' = [flags EXCEPT !.covered = FALSE]
  /\ counts' = counts
  /\ QaFollowsHead

T4ShipMerge ==
  /\ "T4" \notin Omit
  /\ state = "ship"
  /\ flags.covered \/ "ShipGuard" \in Omit
  /\ QaCovers \/ "QaGuard" \in Omit
  /\ state' = "merged"
  /\ keep
  /\ flags' = flags
  /\ counts' = counts
  /\ QaFollowsHead

T4ShipHandoff ==
  /\ "T4" \notin Omit
  /\ state = "ship"
  /\ flags.covered
  /\ QaCovers
  /\ state' = "handed-off"
  /\ keep
  /\ flags' = flags
  /\ counts' = counts
  /\ QaFollowsHead

\* #130. finish_line merge (ship = handoff or merge): the PR is marked ready
\* and waits in the merge queue instead of handed-off, watched until it merges.
T4ShipQueue ==
  /\ "T4" \notin Omit
  /\ state = "ship"
  /\ flags.covered
  /\ QaCovers
  /\ state' = "merge-queue"
  /\ keep
  /\ flags' = flags
  /\ counts' = counts
  /\ QaFollowsHead

T4ShipAfter ==
  /\ "T4" \notin Omit
  /\ state = "ship"
  /\ flags.covered
  /\ QaCovers
  /\ state' = "waiting-external"
  /\ keep
  /\ flags' = flags
  /\ counts' = counts
  /\ QaFollowsHead

\* A new head is pushed while the ticket ships or waits for the approver's OK.
\* The APPROVE no longer covers the PR head.
T4HeadMoves ==
  /\ "T4" \notin Omit
  /\ state \in {"ship", "awaiting-ok"}
  /\ flags.covered
  /\ state' = state
  /\ keep
  /\ flags' = [flags EXCEPT !.covered = FALSE]
  /\ counts' = counts
  /\ QaFollowsHead

\* T4H (issues #86, #183). ship = merge or handoff and the PR is held (hold
\* label or hold.paths): no ship until the approver's OK at the covered head,
\* or the approver removes a label-only hold. The OK then ships as T4 would:
\* merged, merge-queue (finish_line merge) or handed-off. A head whose patch
\* differs goes back to review.
T4Hold ==
  /\ "T4" \notin Omit
  /\ state = "ship"
  /\ flags.covered
  /\ QaCovers
  /\ state' = "awaiting-ok"
  /\ keep
  /\ flags' = flags
  /\ counts' = counts
  /\ QaFollowsHead

T4HoldOk ==
  /\ "T4" \notin Omit
  /\ state = "awaiting-ok"
  /\ flags.covered
  /\ state' = "merged"
  /\ keep
  /\ flags' = flags
  /\ counts' = counts
  /\ QaFollowsHead

T4HoldOkQueue ==
  /\ "T4" \notin Omit
  /\ state = "awaiting-ok"
  /\ flags.covered
  /\ state' = "merge-queue"
  /\ keep
  /\ flags' = flags
  /\ counts' = counts
  /\ QaFollowsHead

T4HoldOkHandoff ==
  /\ "T4" \notin Omit
  /\ state = "awaiting-ok"
  /\ flags.covered
  /\ state' = "handed-off"
  /\ keep
  /\ flags' = flags
  /\ counts' = counts
  /\ QaFollowsHead

T4HoldPatchDiffers ==
  /\ "T4" \notin Omit
  /\ state = "awaiting-ok"
  /\ state' = "review"
  /\ keep
  /\ flags' = [flags EXCEPT !.covered = FALSE]
  /\ counts' = counts
  /\ QaFollowsHead

T4HoldBaseUpdate ==
  /\ "T4" \notin Omit
  /\ state = "awaiting-ok"
  /\ state' = "fixing"
  /\ keep
  /\ flags' = [flags EXCEPT !.covered = FALSE]
  /\ counts' = counts
  /\ QaFollowsHead

\* T8. ok head=<sha> on the previewed SHA ships. Decline fixes.
\* The third decline is WaitingExternal --> Stuck.
T8Ok ==
  /\ "T8" \notin Omit
  /\ state = "waiting-external"
  /\ state' = "merged"
  /\ keep
  /\ flags' = flags
  /\ counts' = counts
  /\ QaFollowsHead

T8Decline ==
  /\ "T8" \notin Omit
  /\ state = "waiting-external"
  /\ counts.declines < 2
  /\ state' = "fixing"
  /\ keep
  /\ flags' = [flags EXCEPT !.covered = FALSE]
  /\ counts' = [counts EXCEPT !.declines = @ + 1, !.deaths = 0, !.gate = 0]
  /\ QaFollowsHead

T8DeclineStuck ==
  /\ "T8" \notin Omit
  /\ state = "waiting-external"
  /\ counts.declines = 2
  /\ state' = "stuck"
  /\ keep
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.declines = 3, !.deaths = 0]
  /\ QaFollowsHead

\* T9. Present in the model. Enabled only when the project has post_merge.
\* Phase 1 config sets AllowPostMerge to FALSE, so this action is never enabled there.
T9PostMerge ==
  /\ "T9" \notin Omit
  /\ AllowPostMerge
  /\ state = "merged"
  /\ state' = "post-merge"
  /\ keep
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.deaths = 0, !.gate = 0]
  /\ QaFollowsHead

MergedDone ==
  /\ state = "merged"
  /\ state' = "done"
  /\ keep
  /\ flags' = flags
  /\ counts' = counts
  /\ QaFollowsHead

T9Pass ==
  /\ "T9" \notin Omit
  /\ state = "post-merge"
  /\ state' = "done"
  /\ keep
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.deaths = 0]
  /\ QaFollowsHead

T9Fail ==
  /\ "T9" \notin Omit
  /\ state = "post-merge"
  /\ state' = "done"
  /\ keep
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.deaths = 0]
  /\ QaFollowsHead

\* Max's extra round is recorded on the ticket (cap_extra, issue #105) and
\* raises the effective cap by one. Bounded at 1 for TLC: a later OK keeps it.
MaxOkAnotherRound ==
  /\ state = "capped"
  /\ state' = "fixing"
  /\ keep
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.deaths = 0, !.gate = 0, !.extra = 1]
  /\ QaFollowsHead

\* #118. An approver's go on a handed-off ticket whose next round would pass
\* the effective cap is the same extra round, and the approved round counts as
\* completed. Bounded at 1 for TLC: a go at the cap after a second handoff is
\* not modeled.
MaxOkAfterHandoff ==
  /\ state = "handed-off"
  /\ counts.round + 1 >= cap + counts.extra
  /\ counts.extra = 0
  /\ state' = "fixing"
  /\ keep
  /\ flags' = [flags EXCEPT !.covered = FALSE]
  /\ counts' = [counts EXCEPT !.round = @ + 1, !.deaths = 0, !.gate = 0, !.extra = 1]
  /\ QaFollowsHead

\* #188. Only an approver's go leaves blocked. It queues a fresh review at the
\* same round, since BLOCKED consumes none.
MaxGoAfterBlocked ==
  /\ state = "blocked"
  /\ state' = "review"
  /\ keep
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.deaths = 0]
  /\ QaFollowsHead

\* #611. TLC bounds extra at 1, so a second extra round this way is not modeled.
T1UnsatFix ==
  /\ state = "waiting-max"
  /\ paused = "review"
  /\ counts.round + 1 < cap + counts.extra \/ counts.extra = 0
  /\ state' = "fixing"
  /\ keep
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.round = @ + 1, !.deaths = 0,
       !.extra = IF counts.round + 1 >= cap + counts.extra THEN 1 ELSE @]
  /\ QaFollowsHead

\* #108. A trusted ready at a new head after handoff re-enters review. The
\* approved round counts as completed, so the next review is round + 2, and it
\* must be within the effective cap. #130: the same from merge-queue.
ReenterAfterHandoff ==
  /\ "T2" \notin Omit
  /\ state \in {"handed-off", "merge-queue"}
  /\ counts.round + 1 < cap + counts.extra
  /\ state' = "review"
  /\ keep
  /\ flags' = [flags EXCEPT !.covered = FALSE]
  /\ counts' = [counts EXCEPT !.round = @ + 1, !.deaths = 0, !.gate = 0]
  /\ QaFollowsHead

\* #108. The poll sees the PR merged, by the factory or by hand: merged, with
\* stuck, paused and every counter that marks stuck cleared.
MergeObserved ==
  /\ \/ state \in MergeFrom
     \/ state = "waiting-max" /\ paused # "questions"
  /\ state' = "merged"
  /\ keep
  /\ flags' = [flags EXCEPT !.byHand = ~flags.covered \/ ~QaCovers \/ state \notin
       {"ship", "awaiting-ok", "waiting-external", "handed-off", "merge-queue",
        "updating"}]
  /\ counts' = [counts EXCEPT !.gate = 0, !.deaths = 0, !.declines = 0,
       !.planGaps = 0, !.updates = 0]
  /\ QaFollowsHead

\* #130. The queue step acts on the head of a repo's merge queue. It squash-
\* merges it when it is clean, CI is green and the approval covers the head.
QueueMerge ==
  /\ "T4" \notin Omit
  /\ state = "merge-queue"
  /\ flags.covered
  /\ state' = "merged"
  /\ keep
  /\ flags' = flags
  /\ counts' = counts
  /\ QaFollowsHead

\* #130. The queue head conflicts with base: an update-from-main job is queued.
QueueConflict ==
  /\ "T4" \notin Omit
  /\ state = "merge-queue"
  /\ state' = "updating"
  /\ keep
  /\ flags' = flags
  /\ counts' = counts
  /\ QaFollowsHead

\* #130. The PR is closed unmerged.
QueueClosed ==
  /\ "T4" \notin Omit
  /\ state = "merge-queue"
  /\ state' = "abandoned"
  /\ keep
  /\ flags' = flags
  /\ counts' = counts
  /\ QaFollowsHead

\* #130. A trusted go on a merge-queue ticket leaves the queue and queues a plan
\* or a build; ship waits for a new APPROVE. The model draws the risk cap at
\* the first review, so it starts the ticket over from the go: no cap, no
\* mapped plan, and every counter cleared.
GoFresh ==
  /\ "T1" \notin Omit
  /\ state = "merge-queue"
  /\ paused' = "none"
  /\ cap' = 0
  /\ configOk' = configOk
  /\ flags' = [flags EXCEPT !.covered = FALSE, !.planOk = FALSE]
  /\ counts' = [c \in DOMAIN counts |-> 0]
  /\ QaFollowsHead

QueueGoPlan == GoFresh /\ state' = "planning"

QueueGoBuild == GoFresh /\ state' = "building"

\* #130. The update job's ready. Trivial (each conflict hunk took one side and
\* nothing else changed): the approval is carried to the new head and the
\* ticket goes back in the queue. Otherwise a cross-family re-review at the
\* approved round (the round does not move).
UpdateTrivial ==
  /\ "T2" \notin Omit
  /\ state = "updating"
  /\ state' = "merge-queue"
  /\ keep
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.updates = 0]
  /\ QaFollowsHead

UpdateNonTrivial ==
  /\ "T2" \notin Omit
  /\ state = "updating"
  /\ state' = "review"
  /\ keep
  /\ flags' = [flags EXCEPT !.covered = FALSE]
  /\ counts' = [counts EXCEPT !.updates = 0, !.deaths = 0, !.gate = 0]
  /\ QaFollowsHead

\* #130. An update fails (the job fails, its ready is still dirty, or no ready
\* arrives). Once: one more update. Twice: stuck, update-failed-2.
UpdateRetry ==
  /\ "T6" \notin Omit
  /\ state = "updating"
  /\ counts.updates = 0
  /\ state' = state
  /\ keep
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.updates = 1]
  /\ QaFollowsHead

UpdateStuck ==
  /\ "T6" \notin Omit
  /\ state = "updating"
  /\ counts.updates = 1
  /\ state' = "stuck"
  /\ keep
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.updates = 2]
  /\ QaFollowsHead

QaRunsHere ==
  /\ state = "ship"
  /\ qa.gate
  /\ flags.covered

QaStep(from, to) ==
  /\ QaRunsHere
  /\ qa.run = from
  /\ qa' = [qa EXCEPT !.run = to]
  /\ UNCHANGED <<state, paused, cap, configOk, flags, counts>>

QaStart ==
  \/ QaStep("none", "running")
  \/ qa.envFails = 1 /\ QaStep("env-failed", "running")

QaPass == QaStep("running", "pass")

QaFail == QaStep("running", "fail")

QaNotRequired == QaStep("none", "not-required")

QaEnvFail ==
  /\ QaRunsHere
  /\ qa.run = "running"
  /\ qa.envFails = 0
  /\ qa' = [qa EXCEPT !.run = "env-failed", !.envFails = 1]
  /\ UNCHANGED <<state, paused, cap, configOk, flags, counts>>

QaEnvFailPark ==
  /\ QaRunsHere
  /\ qa.run = "running"
  /\ qa.envFails = 1
  /\ state' = "waiting-max"
  /\ paused' = "ship"
  /\ qa' = [qa EXCEPT !.run = "env-failed", !.envFails = 2]
  /\ UNCHANGED <<cap, configOk, flags, counts>>

End ==
  /\ state \in Terminal
  /\ UNCHANGED vars

Next ==
  \/ IntakeBrief \/ IntakeRefused
  \/ T5Ask \/ T5Answer \/ QaParkAnswered \/ T5Abandon
  \/ T1KindGoPlan \/ T1KindGoBuild
  \/ T1PPlanPosted \/ T1PPlanGapOnce \/ T1PPlanGapStuck \/ T1PPlanMapped
  \/ T1PPlanOverSize
  \/ T2ReadyGreen \/ T2CiRed \/ T2CiRedStuck \/ T2ReadyCovered
  \/ T6JobRetry \/ T6JobStuck
  \/ T3Revise \/ T3Unsat \/ T7Blocked
  \/ T4Approve \/ T4ApproveCiRed \/ T4HeadMoves \/ T4PatchDiffers
  \/ T4ShipMerge \/ T4ShipHandoff \/ T4ShipAfter
  \/ T4Hold \/ T4HoldOk \/ T4HoldOkQueue \/ T4HoldOkHandoff
  \/ T4HoldPatchDiffers \/ T4HoldBaseUpdate
  \/ T8Ok \/ T8Decline \/ T8DeclineStuck
  \/ T9PostMerge \/ MergedDone \/ T9Pass \/ T9Fail
  \/ MaxOkAnotherRound \/ MaxGoAfterBlocked \/ ReenterAfterHandoff \/ MergeObserved
  \/ T1UnsatFix
  \/ MaxOkAfterHandoff
  \/ T4ShipQueue \/ QueueMerge \/ QueueConflict \/ QueueClosed
  \/ QueueGoPlan \/ QueueGoBuild
  \/ UpdateTrivial \/ UpdateNonTrivial \/ UpdateRetry \/ UpdateStuck
  \/ QaStart \/ QaPass \/ QaFail \/ QaNotRequired \/ QaEnvFail \/ QaEnvFailPark
  \/ End

Spec == Init /\ [][Next]_vars

QuestionsBeforeBuilding ==
  state \in AfterGo => flags.questionsDone

QuestionResumes ==
  /\ state = "waiting-max" => paused \in {"questions", "building", "fixing", "review", "ship"}
  /\ state # "waiting-max" => paused = "none"
  /\ paused = "questions" => ~flags.questionsDone /\ cap = 0
  /\ paused = "building" => flags.questionsDone /\ cap = 0
  /\ paused = "fixing" => flags.questionsDone /\ cap \in {1, 3}
  /\ paused = "review" => flags.questionsDone /\ cap \in {1, 3}
  /\ paused = "ship" => flags.questionsDone /\ cap \in {1, 3} /\ flags.covered

MappedBeforeBuilding ==
  /\ state \in {"planning", "plan-check"} => flags.questionsDone /\ ~flags.planOk
  /\ flags.planOk => state \notin {"intake", "refused", "questions", "planning", "plan-check"}

CountersBounded ==
  /\ cap = 0 => counts.round = 0 /\ counts.extra = 0
  /\ cap \in {1, 3} => counts.round <= cap + counts.extra
  /\ counts.gate = 3 => state = "stuck"
  /\ counts.declines = 3 => state = "stuck"
  /\ counts.planGaps = 2 => state = "stuck"
  /\ counts.deaths = 2 => state = "stuck"
  /\ counts.updates = 2 => state = "stuck"
  /\ counts.updates = 1 => state = "updating"
  /\ counts.acUnmet <= 2
  /\ paused = "ship" => qa.envFails = 2
  /\ qa.envFails = 2 => state \in {"waiting-max", "merged", "post-merge", "done", "abandoned"}
  /\ ~qa.gate \/ ~flags.covered => qa.run = "none" /\ qa.envFails = 0

ContradictoryConfigNeverRuns ==
  ~configOk => state \in {"intake", "refused"}

\* A hand merge (MergeObserved before a covered ship) is Max's, not the factory's.
ShipPinnedToCoveredHead ==
  state \in Covered => flags.covered \/ flags.byHand

ShipNeedsCoveredQa ==
  qa.gate /\ state \in Covered /\ ~flags.byHand => qa.run \in {"pass", "not-required"}

CapAtReview ==
  /\ state \in {"intake", "refused", "questions", "planning", "plan-check", "building"}
       => cap = 0
  /\ state \in ReviewAndAfter /\ ~flags.byHand => cap \in {1, 3}

Phase1T9Off ==
  ~AllowPostMerge => state # "post-merge"

\* Each decided row is enabled in the state the table names.
\* A fixture that puts "T1" in Omit makes T1KindGo false.

T1KindGo ==
  state = "questions" => ENABLED T1KindGoBuild /\ ENABLED T1KindGoPlan

T1PDecided ==
  /\ state = "planning" => ENABLED T1PPlanPosted
  /\ state = "plan-check" /\ counts.planGaps = 0 =>
       ENABLED T1PPlanGapOnce /\ ENABLED T1PPlanMapped /\ ENABLED T1PPlanOverSize
  /\ state = "plan-check" /\ counts.planGaps = 1 =>
       ENABLED T1PPlanGapStuck /\ ENABLED T1PPlanMapped /\ ENABLED T1PPlanOverSize

T2Decided ==
  /\ state \in {"building", "fixing"} => ENABLED T2ReadyGreen
  /\ state \in {"building", "fixing"} /\ counts.gate < 2 => ENABLED T2CiRed
  /\ state \in {"building", "fixing"} /\ counts.gate = 2 => ENABLED T2CiRedStuck
  /\ state = "fixing" /\ flags.covered => ENABLED T2ReadyCovered

T3Decided ==
  state \in {"review", "awaiting-ok"} => ENABLED T3Revise /\ ENABLED T3Unsat

T4Decided ==
  /\ state = "review" => ENABLED T4Approve
  /\ state = "review" /\ counts.gate < 2 => ENABLED T4ApproveCiRed
  /\ state = "ship" => ENABLED T4PatchDiffers
  /\ state = "ship" /\ flags.covered /\ QaCovers =>
       ENABLED T4ShipMerge /\ ENABLED T4ShipHandoff /\ ENABLED T4ShipAfter
         /\ ENABLED T4Hold /\ ENABLED T4ShipQueue
  /\ state = "awaiting-ok" => ENABLED T4HoldPatchDiffers
  /\ state = "awaiting-ok" /\ flags.covered =>
       ENABLED T4HoldOk /\ ENABLED T4HoldOkQueue /\ ENABLED T4HoldOkHandoff

QaDecided ==
  /\ state = "ship" /\ qa.gate /\ flags.covered =>
       /\ qa.run = "none" => ENABLED QaStart /\ ENABLED QaNotRequired
       /\ qa.run = "running" => ENABLED QaPass /\ ENABLED QaFail
       /\ qa.run = "running" /\ qa.envFails = 0 => ENABLED QaEnvFail
       /\ qa.run = "running" /\ qa.envFails = 1 => ENABLED QaEnvFailPark
       /\ qa.run = "env-failed" => ENABLED QaStart
  /\ state = "waiting-max" /\ paused = "ship" => ENABLED QaParkAnswered

T5Decided ==
  /\ state \in {"questions", "building", "fixing"} => ENABLED T5Ask
  /\ state = "waiting-max" /\ paused # "ship" => ENABLED T5Answer
  /\ state = "waiting-max" => ENABLED T5Abandon

T6Decided ==
  /\ state \in JobStates /\ counts.deaths = 0 => ENABLED T6JobRetry
  /\ state \in JobStates /\ counts.deaths = 1 => ENABLED T6JobStuck

T7Decided ==
  /\ state = "intake" /\ ~configOk => ENABLED IntakeRefused
  /\ state = "review" => ENABLED T7Blocked

T8Decided ==
  /\ state = "waiting-external" => ENABLED T8Ok
  /\ state = "waiting-external" /\ counts.declines < 2 => ENABLED T8Decline
  /\ state = "waiting-external" /\ counts.declines = 2 => ENABLED T8DeclineStuck

HandedOffDecided ==
  /\ state = "handed-off" => ENABLED MergeObserved
  /\ state = "handed-off" /\ counts.round + 1 < cap + counts.extra =>
       ENABLED ReenterAfterHandoff
  /\ state = "handed-off" /\ counts.round + 1 >= cap + counts.extra
     /\ counts.extra = 0 => ENABLED MaxOkAfterHandoff

\* #118. Leaving handoff for more work counts the approved round as completed.
HandoffCountsApprovedRound ==
  [][MaxOkAfterHandoff \/ ReenterAfterHandoff => counts'.round = counts.round + 1]_vars

\* #130. A ticket in merge-queue can always leave it: merged, abandoned,
\* updating, or back to work on a go or a ready. An update can always end:
\* back in the queue, a re-review, merged, or stuck after its second failure.
MergeQueueDecided ==
  /\ state = "merge-queue" =>
       /\ ENABLED QueueMerge /\ ENABLED MergeObserved
       /\ ENABLED QueueClosed /\ ENABLED QueueConflict
       /\ ENABLED QueueGoPlan /\ ENABLED QueueGoBuild
  /\ state = "merge-queue" /\ counts.round + 1 < cap + counts.extra =>
       ENABLED ReenterAfterHandoff
  /\ state = "updating" =>
       ENABLED UpdateTrivial /\ ENABLED UpdateNonTrivial /\ ENABLED MergeObserved
  /\ state = "updating" /\ counts.updates = 0 => ENABLED UpdateRetry
  /\ state = "updating" /\ counts.updates = 1 => ENABLED UpdateStuck

T9Decided ==
  /\ AllowPostMerge /\ state = "merged" => ENABLED T9PostMerge
  /\ state = "merged" => ENABLED MergedDone
  /\ state = "post-merge" => ENABLED T9Pass /\ ENABLED T9Fail

ChartEdges ==
  /\ state = "intake" /\ configOk => ENABLED IntakeBrief
  /\ state = "planning" => ENABLED T1PPlanPosted
  /\ state = "plan-check" /\ counts.planGaps = 0 =>
       ENABLED T1PPlanGapOnce /\ ENABLED T1PPlanMapped /\ ENABLED T1PPlanOverSize
  /\ state = "plan-check" /\ counts.planGaps = 1 =>
       ENABLED T1PPlanGapStuck /\ ENABLED T1PPlanMapped /\ ENABLED T1PPlanOverSize
  /\ state = "capped" => ENABLED MaxOkAnotherRound
  /\ state = "blocked" => ENABLED MaxGoAfterBlocked

\* Drawing bound for the Mermaid chart. The full configs explore every interleaving.
\* This keeps TLC under statechart's 500-state drawing limit and still reaches
\* every section 4.1 name. T1PPlanMapped is in the picture: a mapped plan may
\* enter building. One risk cap. A CI-red streak and a job-death streak are
\* not stacked, and the mapped plan does not replay those streaks. Max's extra
\* round is drawn up to the fixing it starts.
GatedFirstRoundToQaPark ==
  /\ configOk
  /\ counts = [c \in DOMAIN counts |-> 0]
  /\ ~flags.planOk
  /\ state \in {"intake", "questions", "building", "review", "ship", "waiting-max"}
  /\ state = "waiting-max" => paused = "ship"
  /\ state = "ship" => flags.covered /\ qa.run \in {"none", "running", "env-failed"}

ChartSlice ==
  /\ ~qa.gate \/ GatedFirstRoundToQaPark
  /\ counts.acUnmet = 0
  /\ counts.extra = 0 \/ state = "fixing"
  /\ cap \in {0, 3}
  /\ counts.deaths = 0 \/ counts.gate = 0
  /\ \/ ~flags.planOk
     \/ /\ counts.round = 0
        /\ counts.declines = 0
        /\ counts.gate = 0
        /\ counts.deaths = 0

====
