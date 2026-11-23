---- MODULE Orchestrator ----
\* Ticket loop from SPEC section 4.
\* State names are the orch:state labels (kebab-case). T1 is the kind=go handoff.
\* T9 (post-merge) is in this module. Phase 1 config sets AllowPostMerge to FALSE.

EXTENDS Integers

CONSTANTS AllowPostMerge, Omit

VARIABLES state, paused, cap, configOk, flags, counts, external, qa

vars == <<state, paused, cap, configOk, flags, counts, external, qa>>

States == {
  "intake", "refused", "questions", "waiting-max", "planning", "plan-check",
  "building", "review", "fixing", "stuck", "capped", "blocked", "ship",
  "merged", "handed-off", "merge-queue", "updating", "waiting-external",
  "awaiting-ok", "post-merge", "done", "abandoned", "handed-off-externally"
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
  "awaiting-ok", "post-merge", "done", "stuck", "handed-off-externally"
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

Decided == {"T1", "T1P", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9", "TL", "TRB"}

\* #749. States a Linear handoff name (orch.yml linear_handoff_states) parks
\* in handed-off-externally. orch.db keeps stuck as a flag on one of the
\* others; here it is a state. In ship, handed-off, merge-queue, updating and
\* waiting-external that name is the factory's own move and changes nothing.
HandoffFrom == {
  "planning", "plan-check", "building", "review", "fixing", "capped",
  "blocked", "waiting-max", "awaiting-ok", "stuck"
}

\* Past go and not finished: the tickets Linear Done or Canceled settles.
LinearFrom == HandoffFrom \cup {
  "ship", "handed-off", "merge-queue", "updating", "waiting-external",
  "handed-off-externally"
}

Guards == {"ShipGuard", "QaGuard", "StackGuard", "HoldGuard"}

QaRuns == {"none", "running", "pass", "fail", "env-failed", "not-required"}

ASSUME AllowPostMerge \in BOOLEAN
ASSUME Omit \subseteq Decided \cup Guards

TypeOK ==
  /\ state \in States
  /\ paused \in {"none", "questions", "building", "fixing", "review", "ship"}
  /\ cap \in {0, 1, 3}
  /\ configOk \in BOOLEAN
  /\ flags \in [
       covered: BOOLEAN, planOk: BOOLEAN, questionsDone: BOOLEAN, byHand: BOOLEAN,
       reopenHold: BOOLEAN, rebase: {"none", "capped", "other", "review"},
       stack: {"none", "parent", "held"}
     ]
  /\ external \in {"none"} \cup HandoffFrom
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
  /\ flags \in [
       covered: {FALSE}, planOk: {FALSE}, questionsDone: {FALSE},
       byHand: {FALSE}, reopenHold: {FALSE}, rebase: {"none"},
       stack: {"none", "parent"}
     ]
  /\ counts = [
       round |-> 0, acUnmet |-> 0, gate |-> 0,
       planGaps |-> 0, deaths |-> 0, declines |-> 0,
       extra |-> 0, updates |-> 0
     ]
  /\ external = "none"
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
  /\ external' = external
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
  /\ external' = external
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
  /\ external' = external
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
  /\ external' = external
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
  /\ UNCHANGED <<cap, configOk, flags, counts, external>>

T5Abandon ==
  /\ "T5" \notin Omit
  /\ state = "waiting-max"
  /\ state' = "abandoned"
  /\ paused' = "none"
  /\ external' = external
  /\ cap' = cap
  /\ configOk' = configOk
  /\ flags' = [flags EXCEPT !.reopenHold = FALSE, !.rebase = "none"]
  /\ counts' = counts
  /\ QaFollowsHead

\* T1. kind=go handoff (orch:go) after Questions. Over 3 files: queue plan.
T1KindGoPlan ==
  /\ "T1" \notin Omit
  /\ state = "questions"
  /\ state' = "planning"
  /\ paused' = "none"
  /\ external' = external
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
  /\ external' = external
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
  /\ external' = external
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
  /\ external' = external
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
  /\ external' = external
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
  /\ external' = external
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
  /\ external' = external
  /\ cap' = cap
  /\ configOk' = configOk
  /\ flags' = [flags EXCEPT !.planOk = TRUE]
  /\ counts' = [counts EXCEPT !.deaths = 0]
  /\ QaFollowsHead

\* #1117. On a GitHub-ticket repo with an intake label, an over-size plan
\* whose split parses into 2+ parts is filed as tickets and this one abandoned.
T1PPlanAutoSplit ==
  /\ "T1P" \notin Omit
  /\ state = "plan-check"
  /\ state' = "abandoned"
  /\ paused' = "none"
  /\ external' = external
  /\ cap' = cap
  /\ configOk' = configOk
  /\ flags' = [flags EXCEPT !.planOk = TRUE]
  /\ counts' = [counts EXCEPT !.deaths = 0]
  /\ QaFollowsHead

Unheld == flags.stack # "held" \/ "HoldGuard" \in Omit

keep ==
  cap' = cap /\ configOk' = configOk /\ paused' = "none" /\ external' = external

\* T2. ready head=<sha>, then CI on that SHA.
T2ReadyGreen ==
  /\ "T2" \notin Omit
  /\ state \in {"building", "fixing"}
  /\ flags.rebase = "none"
  /\ state' = "review"
  /\ paused' = "none"
  /\ external' = external
  /\ configOk' = configOk
  /\ flags' = [flags EXCEPT !.covered = FALSE]
  /\ counts' = [counts EXCEPT !.gate = 0, !.deaths = 0]
  /\ \/ /\ cap = 0
        /\ cap' \in {1, 3}
     \/ /\ cap \in {1, 3}
        /\ cap' = cap
  /\ QaFollowsHead

RebaseRound ==
  IF flags.rebase = "capped" /\ counts.round > 0 THEN counts.round - 1 ELSE counts.round

T2CiRed ==
  /\ "T2" \notin Omit
  /\ state \in {"building", "fixing"}
  /\ counts.gate < 2
  /\ state' = state
  /\ keep
  /\ flags' = [flags EXCEPT !.rebase = "none"]
  /\ counts' = [counts EXCEPT !.gate = @ + 1, !.deaths = 0, !.round = RebaseRound]
  /\ QaFollowsHead

T2CiRedStuck ==
  /\ "T2" \notin Omit
  /\ state \in {"building", "fixing"}
  /\ counts.gate = 2
  /\ state' = "stuck"
  /\ keep
  /\ flags' = [flags EXCEPT !.rebase = "none"]
  /\ counts' = [counts EXCEPT !.gate = 3, !.deaths = 0]
  /\ QaFollowsHead

T2ReadyCovered ==
  /\ "T2" \notin Omit
  /\ state = "fixing"
  /\ flags.covered
  /\ state' = "ship"
  /\ keep
  /\ flags' = [flags EXCEPT !.reopenHold = FALSE]
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
  /\ external' = external
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
  /\ flags' = [flags EXCEPT !.rebase = "none"]
  /\ counts' = [counts EXCEPT !.deaths = 2]
  /\ QaFollowsHead

\* T3. REVISE under the effective cap (cap plus Max's extra rounds) queues fix.
\* At it, or a second AC-unmet verdict: capped.
\* A REVISE also replaces a held APPROVE (T4H, issue #86).
T3Revise ==
  /\ "T3" \notin Omit
  /\ state \in {"review", "awaiting-ok"}
  /\ keep
  /\ flags' = [flags EXCEPT !.covered = FALSE, !.rebase = "none"]
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
  /\ external' = external
  /\ cap' = cap
  /\ configOk' = configOk
  /\ flags' = [flags EXCEPT !.covered = FALSE, !.rebase = "none"]
  /\ counts' = counts
  /\ QaFollowsHead

\* T7. BLOCKED, checked like REVISE. No known answer. Wake Grok Bot once.
T7Blocked ==
  /\ "T7" \notin Omit
  /\ state = "review"
  /\ state' = "blocked"
  /\ keep
  /\ flags' = [flags EXCEPT !.rebase = "none"]
  /\ counts' = [counts EXCEPT !.deaths = 0]
  /\ QaFollowsHead

\* T4. APPROVE ac=met head=<sha> on the current head. A later patch diff sends it back.
T4Approve ==
  /\ "T4" \notin Omit
  /\ state = "review"
  /\ state' = "ship"
  /\ keep
  /\ flags' = [flags EXCEPT !.covered = TRUE, !.reopenHold = FALSE, !.rebase = "none"]
  /\ counts' = [counts EXCEPT !.deaths = 0]
  /\ QaFollowsHead

T4ApproveCiRed ==
  /\ "T4" \notin Omit
  /\ state = "review"
  /\ counts.gate < 2
  /\ state' = "fixing"
  /\ keep
  /\ flags' = [flags EXCEPT !.covered = TRUE, !.rebase = "none"]
  /\ counts' = [counts EXCEPT !.gate = @ + 1, !.deaths = 0]
  /\ QaFollowsHead

T4PatchDiffers ==
  /\ "T4" \notin Omit
  /\ state = "ship"
  /\ Unheld
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
  /\ flags.stack = "none" \/ "StackGuard" \in Omit
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
  /\ flags.stack = "none"
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
  /\ flags.stack = "none"
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
  /\ flags.stack = "none"
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
  /\ flags.stack = "none"
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

\* #891. Max can hand a ticket off before its first review, when the model
\* has drawn no cap yet. orch.db always has the risk cap, so leaving
\* handed-off draws it here as T2ReadyGreen does.
HandoffCaps == IF cap = 0 THEN {1, 3} ELSE {cap}

\* #118. An approver's go on a handed-off ticket whose next round would pass
\* the effective cap is the same extra round, and the approved round counts as
\* completed. Bounded at 1 for TLC: a go at the cap after a second handoff is
\* not modeled.
MaxOkAfterHandoff ==
  /\ state = "handed-off"
  /\ cap' \in HandoffCaps
  /\ counts.round + 1 >= cap' + counts.extra
  /\ counts.extra = 0
  /\ state' = "fixing"
  /\ configOk' = configOk /\ paused' = "none" /\ external' = external
  /\ flags' = [flags EXCEPT !.covered = FALSE, !.byHand = FALSE]
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

\* #1420. The round steps back one so the re-review counts the stopped round.
ApproverOverrule ==
  /\ state = "capped"
  /\ counts.round >= 1
  /\ state' = "review"
  /\ keep
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.round = @ - 1, !.deaths = 0]
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
  /\ cap' \in HandoffCaps
  /\ counts.round + 1 < cap' + counts.extra
  /\ state' = "review"
  /\ configOk' = configOk /\ paused' = "none" /\ external' = external
  /\ flags' = [flags EXCEPT !.covered = FALSE, !.byHand = FALSE]
  /\ counts' = [counts EXCEPT !.round = @ + 1, !.deaths = 0, !.gate = 0]
  /\ QaFollowsHead

\* #1197. Max's go under the effective cap reopens a handed-off ticket.
ReopenAfterHandoff ==
  /\ state = "handed-off"
  /\ cap' \in HandoffCaps
  /\ counts.round + 1 < cap' + counts.extra
  /\ state' = "fixing"
  /\ configOk' = configOk /\ paused' = "none" /\ external' = external
  /\ flags' = [flags EXCEPT !.covered = FALSE, !.byHand = FALSE, !.reopenHold = TRUE]
  /\ counts' = [counts EXCEPT !.round = @ + 1, !.deaths = 0, !.gate = 0]
  /\ QaFollowsHead

RebaseFrom == {
  "review", "fixing", "awaiting-ok", "capped", "handed-off", "merge-queue",
  "waiting-external"
}

RebaseQueued ==
  /\ "TRB" \notin Omit
  /\ state \in RebaseFrom
  /\ state' = "fixing"
  /\ cap' \in HandoffCaps
  /\ configOk' = configOk /\ paused' = "none" /\ external' = external
  /\ flags' = [flags EXCEPT !.covered = FALSE, !.byHand = FALSE,
       !.rebase = IF state = "capped" THEN "capped" ELSE "other"]
  /\ counts' = [counts EXCEPT !.deaths = 0, !.gate = 0]
  /\ QaFollowsHead

RebaseReady ==
  /\ "TRB" \notin Omit
  /\ state = "fixing"
  /\ flags.rebase \in {"capped", "other"}
  /\ state' = "review"
  /\ keep
  /\ flags' = [flags EXCEPT !.covered = FALSE, !.rebase = "review"]
  /\ counts' = [counts EXCEPT !.gate = 0, !.deaths = 0, !.round = RebaseRound]
  /\ QaFollowsHead

StackWaits == state = "ship" /\ flags.covered /\ flags.stack = "parent"

StackHold ==
  /\ StackWaits
  /\ flags' = [flags EXCEPT !.stack = "held"]
  /\ UNCHANGED <<state, paused, cap, configOk, counts, external, qa>>

\* #108. The poll sees the PR merged, by the factory or by hand: merged, with
\* stuck, paused and every counter that marks stuck cleared.
MergeObserved ==
  /\ \/ state \in MergeFrom
     \/ state = "waiting-max" /\ paused # "questions"
  /\ state' = "merged"
  /\ keep
  /\ flags' = [flags EXCEPT !.byHand = ~flags.covered \/ ~QaCovers \/ state \notin
       {"ship", "awaiting-ok", "waiting-external", "handed-off", "merge-queue",
        "updating"} \/ flags.stack # "none", !.reopenHold = FALSE, !.rebase = "none"]
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
  /\ external' = external
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
  /\ Unheld
  /\ qa.gate
  /\ flags.covered

QaStep(from, to) ==
  /\ QaRunsHere
  /\ qa.run = from
  /\ qa' = [qa EXCEPT !.run = to]
  /\ UNCHANGED <<state, paused, cap, configOk, flags, counts, external>>

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
  /\ UNCHANGED <<state, paused, cap, configOk, flags, counts, external>>

QaEnvFailPark ==
  /\ QaRunsHere
  /\ qa.run = "running"
  /\ qa.envFails = 1
  /\ state' = "waiting-max"
  /\ paused' = "ship"
  /\ qa' = [qa EXCEPT !.run = "env-failed", !.envFails = 2]
  /\ UNCHANGED <<cap, configOk, flags, counts, external>>

End ==
  /\ state \in Terminal
  /\ UNCHANGED vars

\* #749. TL: the poll applies Max's Linear moves, before and after process. Before
\* go (questions, or waiting-max on a product question) Linear is not read.
PastGo == state # "waiting-max" \/ paused # "questions"

\* A handoff name parks the ticket. Nothing else changes: round, counters,
\* cap and the paused step stay as they were, for the resume.
LinearHandoff ==
  /\ "TL" \notin Omit
  /\ state \in HandoffFrom
  /\ PastGo
  /\ ~flags.reopenHold
  /\ state' = "handed-off-externally"
  /\ external' = state
  /\ UNCHANGED <<paused, cap, configOk, flags, counts, qa>>

\* A started state that is not a handoff name (In Progress) resumes the
\* recorded state, stuck included.
LinearResume ==
  /\ "TL" \notin Omit
  /\ state = "handed-off-externally"
  /\ state' = external
  /\ external' = "none"
  /\ UNCHANGED <<paused, cap, configOk, flags, counts, qa>>

\* Linear Done or Canceled settles a ticket past go, parked or not. Like a
\* merge (TM) it clears stuck and the paused step.
LinearSettle ==
  /\ "TL" \notin Omit
  /\ state \in LinearFrom
  /\ PastGo
  /\ paused' = "none"
  /\ cap' = cap
  /\ configOk' = configOk
  /\ external' = "none"
  /\ qa' = qa
  /\ counts' = [counts EXCEPT !.gate = 0, !.deaths = 0, !.declines = 0,
       !.planGaps = 0, !.updates = 0]

\* Linear Done: one fresh PR read, merged if the PR merged, else done.
LinearDone ==
  /\ LinearSettle
  /\ state' \in {"merged", "done"}
  /\ flags' = [flags EXCEPT !.byHand = TRUE, !.reopenHold = FALSE, !.rebase = "none"]

LinearCanceled ==
  /\ LinearSettle
  /\ state' = "abandoned"
  /\ flags' = [flags EXCEPT !.reopenHold = FALSE, !.rebase = "none"]

\* #891. Max hands off a ticket that waits on him (answers state=handed-off,
\* approver only). A ticket with a running job is not a source: that job's
\* ready would re-enter review behind him. The move is Max's, not the
\* factory's, so byHand exempts it from the covered-ship rules.
MaxHandoffFrom == {"awaiting-ok", "capped", "blocked", "waiting-external", "stuck"}

T5Handoff ==
  /\ "T5" \notin Omit
  /\ \/ state \in MaxHandoffFrom
     \/ state = "waiting-max" /\ PastGo
  /\ state' = "handed-off"
  /\ keep
  /\ flags' = [flags EXCEPT !.byHand = TRUE, !.reopenHold = FALSE, !.rebase = "none"]
  /\ counts' = [counts EXCEPT !.gate = 0, !.deaths = 0, !.declines = 0,
       !.planGaps = 0, !.updates = 0]
  /\ qa' = QaReset

\* Every step but Linear's leaves a parked ticket alone.
Factory ==
  \/ IntakeBrief \/ IntakeRefused
  \/ T5Ask \/ T5Answer \/ QaParkAnswered \/ T5Abandon \/ T5Handoff
  \/ T1KindGoPlan \/ T1KindGoBuild
  \/ T1PPlanPosted \/ T1PPlanGapOnce \/ T1PPlanGapStuck \/ T1PPlanMapped
  \/ T1PPlanOverSize \/ T1PPlanAutoSplit
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
  \/ T1UnsatFix \/ ApproverOverrule
  \/ MaxOkAfterHandoff \/ ReopenAfterHandoff
  \/ T4ShipQueue \/ QueueMerge \/ QueueConflict \/ QueueClosed
  \/ QueueGoPlan \/ QueueGoBuild
  \/ UpdateTrivial \/ UpdateNonTrivial \/ UpdateRetry \/ UpdateStuck
  \/ RebaseQueued \/ RebaseReady
  \/ StackHold
  \/ QaStart \/ QaPass \/ QaFail \/ QaNotRequired \/ QaEnvFail \/ QaEnvFailPark
  \/ End

Next ==
  \/ Factory
  \/ LinearHandoff \/ LinearResume \/ LinearDone \/ LinearCanceled

Spec == Init /\ [][Next]_vars

\* The state a parked ticket keeps for its resume; any other ticket's own.
Prior == IF state = "handed-off-externally" THEN external ELSE state

QuestionsBeforeBuilding ==
  state \in AfterGo => flags.questionsDone

QuestionResumes ==
  /\ Prior = "waiting-max" => paused \in {"questions", "building", "fixing", "review", "ship"}
  /\ Prior # "waiting-max" => paused = "none"
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
  /\ counts.gate = 3 => Prior = "stuck"
  /\ counts.declines = 3 => Prior = "stuck"
  /\ counts.planGaps = 2 => Prior = "stuck"
  /\ counts.deaths = 2 => Prior = "stuck"
  /\ counts.updates = 2 => Prior = "stuck"
  /\ counts.updates = 1 => Prior = "updating"
  /\ counts.acUnmet <= 2
  /\ paused = "ship" => qa.envFails = 2
  /\ qa.envFails = 2 => Prior \in {"waiting-max", "merged", "post-merge", "done", "abandoned"}
  /\ ~qa.gate \/ ~flags.covered => qa.run = "none" /\ qa.envFails = 0

ContradictoryConfigNeverRuns ==
  ~configOk => state \in {"intake", "refused"}

\* A hand merge (MergeObserved before a covered ship) is Max's, not the factory's.
ShipPinnedToCoveredHead ==
  state \in Covered => flags.covered \/ flags.byHand

QaResultFromRun ==
  [][/\ qa'.run = "pass" /\ qa.run # "pass" => qa.run = "running"
     /\ qa'.run = "not-required" /\ qa.run # "not-required" => qa.run = "none"]_vars

ShipNeedsCoveredQa ==
  qa.gate /\ state \in Covered /\ ~flags.byHand => qa.run \in {"pass", "not-required"}

CapAtReview ==
  /\ Prior \in {"intake", "refused", "questions", "planning", "plan-check", "building"}
       => cap = 0
  /\ Prior \in ReviewAndAfter /\ ~flags.byHand => cap \in {1, 3}

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
       /\ ENABLED T1PPlanAutoSplit
  /\ state = "plan-check" /\ counts.planGaps = 1 =>
       ENABLED T1PPlanGapStuck /\ ENABLED T1PPlanMapped /\ ENABLED T1PPlanOverSize
       /\ ENABLED T1PPlanAutoSplit

T2Decided ==
  /\ state = "building" \/ (state = "fixing" /\ flags.rebase = "none") => ENABLED T2ReadyGreen
  /\ state \in {"building", "fixing"} /\ counts.gate < 2 => ENABLED T2CiRed
  /\ state \in {"building", "fixing"} /\ counts.gate = 2 => ENABLED T2CiRedStuck
  /\ state = "fixing" /\ flags.covered => ENABLED T2ReadyCovered

T3Decided ==
  state \in {"review", "awaiting-ok"} => ENABLED T3Revise /\ ENABLED T3Unsat

T4Decided ==
  /\ state = "review" => ENABLED T4Approve
  /\ state = "review" /\ counts.gate < 2 => ENABLED T4ApproveCiRed
  /\ state = "ship" /\ flags.stack # "held" => ENABLED T4PatchDiffers
  /\ state = "ship" /\ flags.covered /\ QaCovers /\ flags.stack = "none" =>
       ENABLED T4ShipMerge /\ ENABLED T4ShipHandoff /\ ENABLED T4ShipAfter
         /\ ENABLED T4Hold /\ ENABLED T4ShipQueue
  /\ state = "awaiting-ok" => ENABLED T4HoldPatchDiffers
  /\ state = "awaiting-ok" /\ flags.covered =>
       ENABLED T4HoldOk /\ ENABLED T4HoldOkQueue /\ ENABLED T4HoldOkHandoff

QaDecided ==
  /\ state = "ship" /\ qa.gate /\ flags.covered /\ flags.stack # "held" =>
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
  /\ state \in MaxHandoffFrom \/ (state = "waiting-max" /\ PastGo) => ENABLED T5Handoff

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
  /\ state = "handed-off" /\ cap = 0 => ENABLED ReenterAfterHandoff
  /\ state = "handed-off" /\ counts.round + 1 < cap + counts.extra =>
       ENABLED ReenterAfterHandoff
  /\ state = "handed-off" /\ cap = 0 => ENABLED ReopenAfterHandoff
  /\ state = "handed-off" /\ counts.round + 1 < cap + counts.extra =>
       ENABLED ReopenAfterHandoff
  /\ state = "handed-off" /\ counts.round + 1 >= cap + counts.extra
     /\ counts.extra = 0 => ENABLED MaxOkAfterHandoff

\* #118. Leaving handoff for more work counts the approved round as completed.
HandoffCountsApprovedRound ==
  [][MaxOkAfterHandoff \/ ReenterAfterHandoff \/ ReopenAfterHandoff
       => counts'.round = counts.round + 1]_vars

RebaseDecided ==
  /\ state \in RebaseFrom => ENABLED RebaseQueued
  /\ state = "fixing" /\ flags.rebase \in {"capped", "other"} => ENABLED RebaseReady

RebaseSpendsNoRound ==
  [][RebaseQueued => counts'.round = counts.round /\ counts'.extra = counts.extra]_vars

RebaseInFlight ==
  /\ flags.rebase \in {"capped", "other"} =>
       Prior = "fixing" \/ (Prior = "waiting-max" /\ paused = "fixing")
  /\ flags.rebase = "review" => Prior = "review"

RebaseKeepsExtra ==
  [][flags.rebase # "none" => counts'.extra = counts.extra]_vars

RebaseReviewsAtR ==
  [][(/\ flags.rebase \in {"capped", "other"}
      /\ flags'.rebase \notin {"capped", "other"}
      /\ state' \in {"review", "fixing"})
     => counts'.round = RebaseRound]_vars

ReopenHoldOnlyAtWork ==
  flags.reopenHold => state \in HandoffFrom

NoShipIntoParent ==
  state \in Covered /\ flags.stack # "none" => flags.byHand

StartsJob == state' \in JobStates \/ (qa.run # "running" /\ qa'.run = "running")

StackHeldStartsNoJob ==
  flags.stack = "held" => ~ENABLED (Factory /\ StartsJob)

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

\* #749. A parked ticket remembers the state it left, and only Linear moves
\* it: no read, claim, retry, round or wake acts on it meanwhile.
ExternalKeepsPrior ==
  /\ state = "handed-off-externally" => external \in HandoffFrom
  /\ state # "handed-off-externally" => external = "none"
  /\ state = "handed-off-externally" => ~ENABLED Factory

HandedOffExternallyDecided ==
  /\ state \in HandoffFrom /\ PastGo /\ ~flags.reopenHold => ENABLED LinearHandoff
  /\ state \in LinearFrom /\ PastGo => ENABLED LinearDone /\ ENABLED LinearCanceled
  /\ state = "handed-off-externally" => ENABLED LinearResume

\* #749. Parking and resuming leaves the ticket as the hand-off found it:
\* the same state, round, counters, cap and paused step.
ResumeRestores ==
  [][ /\ state' = "handed-off-externally"
           => UNCHANGED <<paused, cap, configOk, flags, counts>>
      /\ state = "handed-off-externally"
         /\ state' \notin {"handed-off-externally", "merged", "done", "abandoned"}
           => /\ state' = external
              /\ UNCHANGED <<paused, cap, configOk, flags, counts>> ]_vars

ChartEdges ==
  /\ state = "intake" /\ configOk => ENABLED IntakeBrief
  /\ state = "planning" => ENABLED T1PPlanPosted
  /\ state = "plan-check" /\ counts.planGaps = 0 =>
       ENABLED T1PPlanGapOnce /\ ENABLED T1PPlanMapped /\ ENABLED T1PPlanOverSize
       /\ ENABLED T1PPlanAutoSplit
  /\ state = "plan-check" /\ counts.planGaps = 1 =>
       ENABLED T1PPlanGapStuck /\ ENABLED T1PPlanMapped /\ ENABLED T1PPlanOverSize
       /\ ENABLED T1PPlanAutoSplit
  /\ state = "capped" => ENABLED MaxOkAnotherRound
  /\ state = "capped" /\ counts.round >= 1 => ENABLED ApproverOverrule
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
  /\ ~flags.reopenHold \/ (state = "fixing" /\ counts.gate = 0 /\ counts.deaths = 0)
  /\ cap \in {0, 3}
  /\ counts.deaths = 0 \/ counts.gate = 0
  /\ flags.stack = "none" \/ state \in {"intake", "questions", "building", "review", "ship"}
  /\ \/ ~flags.planOk
     \/ /\ counts.round = 0
        /\ counts.declines = 0
        /\ counts.gate = 0
        /\ counts.deaths = 0

\* Drawing bound for Linear's hand-off (TL). The chart draws the incident
\* shapes: a capped or stuck ticket parked and resumed. Section 4.1 states the
\* rule for every other state in prose. Only a ticket with no decline, and a
\* stuck one stuck in its first round with no CI-red streak or plan gap, is
\* parked here, to stay under statechart's 500-state drawing limit. This names states, not actions: a
\* constraint on LinearDone would also drop the merge steps it shares.
ChartLinear ==
  state' = "handed-off-externally" /\ state # "handed-off-externally" =>
    /\ state \in {"capped", "stuck"}
    /\ counts.declines = 0
    /\ state = "capped" \/ (counts.round = 0 /\ counts.gate = 0 /\ counts.planGaps = 0)

\* Drawing bound for abandoned (#891). A dropped ticket is a leaf, and its
\* every round and counter was a state of its own. Drawing only first-round
\* drops keeps every arrow into abandoned and leaves room under statechart's
\* 500-state drawing limit for Max's hand-off.
ChartAbandoned ==
  state' = "abandoned" /\ state # "abandoned" => counts.round = 0

\* Drawing bound for kind=rebase (#1404). A rebase from round 0 lands in a
\* fixing no other step reaches, and its every successor is a new state, so
\* only rebases after a REVISE are drawn, to stay under statechart's
\* 500-state drawing limit. The rebase in fixing is a self-loop the chart
\* does not draw. Naming RebaseQueued here bounds nothing under TLC 1.7.4, so
\* the constraint names the step's effect. A rebase is drawn up to the fixing
\* it starts. Its ready is the fixing to review arrow every ready draws.
ChartRebase ==
  /\ (/\ state' = "fixing"
      /\ state \in RebaseFrom
      /\ ~flags'.covered
      /\ counts'.round = counts.round
      /\ counts'.extra = counts.extra
      /\ counts'.gate = 0
      /\ counts'.deaths = 0)
     => state # "fixing" /\ counts.round >= 1
  /\ flags.rebase = "none"

====
