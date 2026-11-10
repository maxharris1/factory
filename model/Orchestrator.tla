---- MODULE Orchestrator ----
\* Ticket loop from SPEC section 4.
\* State names are the orch:state labels (kebab-case). T1 is the kind=go handoff.
\* T9 (post-merge) is in this module. Phase 1 config sets AllowPostMerge to FALSE.

EXTENDS Integers

CONSTANTS AllowPostMerge, HarnessExec, Omit

VARIABLES state, paused, cap, configOk, flags, counts

vars == <<state, paused, cap, configOk, flags, counts>>

States == {
  "intake", "refused", "questions", "waiting-max", "planning", "plan-check",
  "building", "review", "fixing", "stuck", "capped", "blocked", "ship",
  "merged", "handed-off", "waiting-external", "post-merge", "done", "abandoned"
}

Terminal == {"refused", "stuck", "blocked", "abandoned", "done", "handed-off"}

JobStates == {"planning", "building", "review", "fixing", "post-merge"}

AfterGo == {
  "planning", "plan-check", "building", "review", "fixing", "capped", "blocked",
  "ship", "merged", "handed-off", "waiting-external", "post-merge", "done", "stuck"
}

ReviewAndAfter == {
  "review", "fixing", "capped", "blocked", "ship", "merged", "handed-off",
  "waiting-external", "post-merge", "done"
}

Decided == {"T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9"}

KindGo == "go"

ASSUME AllowPostMerge \in BOOLEAN
ASSUME HarnessExec \in BOOLEAN
ASSUME Omit \subseteq Decided

TypeOK ==
  /\ state \in States
  /\ paused \in {"none", "questions", "building", "fixing"}
  /\ cap \in {0, 1, 3}
  /\ configOk \in BOOLEAN
  /\ flags \in [covered: BOOLEAN, planOk: BOOLEAN, questionsDone: BOOLEAN]
  /\ counts \in [
       round: 0..3,
       acUnmet: 0..2,
       gate: 0..3,
       planGaps: 0..2,
       deaths: 0..2,
       declines: 0..3
     ]

Init ==
  /\ state = "intake"
  /\ paused = "none"
  /\ cap = 0
  /\ configOk \in BOOLEAN
  /\ flags = [covered |-> FALSE, planOk |-> FALSE, questionsDone |-> FALSE]
  /\ counts = [
       round |-> 0, acUnmet |-> 0, gate |-> 0,
       planGaps |-> 0, deaths |-> 0, declines |-> 0
     ]

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

\* T5. Answers re-queue the same step.
T5Answer ==
  /\ "T5" \notin Omit
  /\ state = "waiting-max"
  /\ state' = paused
  /\ paused' = "none"
  /\ cap' = cap
  /\ configOk' = configOk
  /\ flags' = flags
  /\ counts' = counts

T5Abandon ==
  /\ "T5" \notin Omit
  /\ state = "waiting-max"
  /\ state' = "abandoned"
  /\ paused' = "none"
  /\ cap' = cap
  /\ configOk' = configOk
  /\ flags' = flags
  /\ counts' = counts

\* T1. kind=go handoff (orch:go) after Questions. Over 3 files: queue plan.
T1KindGoPlan ==
  /\ "T1" \notin Omit
  /\ KindGo = "go"
  /\ state = "questions"
  /\ state' = "planning"
  /\ paused' = "none"
  /\ cap' = cap
  /\ configOk' = configOk
  /\ flags' = [flags EXCEPT !.questionsDone = TRUE]
  /\ counts' = counts

\* T1. kind=go handoff. 3 files or fewer: queue build.
T1KindGoBuild ==
  /\ "T1" \notin Omit
  /\ KindGo = "go"
  /\ state = "questions"
  /\ state' = "building"
  /\ paused' = "none"
  /\ cap' = cap
  /\ configOk' = configOk
  /\ flags' = [flags EXCEPT !.questionsDone = TRUE]
  /\ counts' = [counts EXCEPT !.deaths = 0, !.gate = 0]

PlanPosted ==
  /\ state = "planning"
  /\ state' = "plan-check"
  /\ paused' = "none"
  /\ cap' = cap
  /\ configOk' = configOk
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.deaths = 0]

PlanGapOnce ==
  /\ state = "plan-check"
  /\ counts.planGaps = 0
  /\ state' = "planning"
  /\ paused' = "none"
  /\ cap' = cap
  /\ configOk' = configOk
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.planGaps = 1, !.deaths = 0]

PlanGapStuck ==
  /\ state = "plan-check"
  /\ counts.planGaps = 1
  /\ state' = "stuck"
  /\ paused' = "none"
  /\ cap' = cap
  /\ configOk' = configOk
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.planGaps = 2, !.deaths = 0]

PlanMapped ==
  /\ state = "plan-check"
  /\ state' = "building"
  /\ paused' = "none"
  /\ cap' = cap
  /\ configOk' = configOk
  /\ flags' = [flags EXCEPT !.planOk = TRUE]
  /\ counts' = [counts EXCEPT !.deaths = 0, !.gate = 0]

keep == cap' = cap /\ configOk' = configOk /\ paused' = "none"

\* T2. ready head=<sha>, then CI on that SHA.
T2ReadyGreen ==
  /\ "T2" \notin Omit
  /\ state \in {"building", "fixing"}
  /\ state' = "review"
  /\ paused' = "none"
  /\ configOk' = configOk
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.gate = 0, !.deaths = 0]
  /\ \/ /\ cap = 0
        /\ cap' \in {1, 3}
     \/ /\ cap \in {1, 3}
        /\ cap' = cap

T2CiRed ==
  /\ "T2" \notin Omit
  /\ state \in {"building", "fixing"}
  /\ counts.gate < 2
  /\ state' = state
  /\ keep
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.gate = @ + 1, !.deaths = 0]

T2CiRedStuck ==
  /\ "T2" \notin Omit
  /\ state \in {"building", "fixing"}
  /\ counts.gate = 2
  /\ state' = "stuck"
  /\ keep
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.gate = 3, !.deaths = 0]

\* T6. No handoff, or the job timeout. Retry once. Second time: stuck.
\* Section 4.1 draws that edge from Planning, Review, and Fixing, and from Building.
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

T6JobStuck ==
  /\ "T6" \notin Omit
  /\ state \in JobStates
  /\ counts.deaths = 1
  /\ state' = "stuck"
  /\ keep
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.deaths = 2]

\* T3. REVISE under the cap queues fix. At the cap, or a second AC-unmet verdict: capped.
T3Revise ==
  /\ "T3" \notin Omit
  /\ state = "review"
  /\ keep
  /\ flags' = flags
  /\ \/ /\ counts.acUnmet = 1
        /\ state' = "capped"
        /\ counts' = [counts EXCEPT !.acUnmet = 2, !.deaths = 0]
     \/ /\ counts.round + 1 < cap
        /\ state' = "fixing"
        /\ counts' = [counts EXCEPT !.round = @ + 1, !.deaths = 0]
     \/ /\ counts.round + 1 >= cap
        /\ state' = "capped"
        /\ counts' = [counts EXCEPT !.round = cap, !.deaths = 0]
     \/ /\ counts.acUnmet = 0
        /\ counts.round + 1 < cap
        /\ state' = "fixing"
        /\ counts' = [counts EXCEPT !.acUnmet = 1, !.round = @ + 1, !.deaths = 0]
     \/ /\ counts.acUnmet = 0
        /\ counts.round + 1 >= cap
        /\ state' = "capped"
        /\ counts' = [counts EXCEPT !.acUnmet = 1, !.round = cap, !.deaths = 0]

\* T7. BLOCKED. No known answer. Wake Grok Bot once.
T7Blocked ==
  /\ "T7" \notin Omit
  /\ state = "review"
  /\ state' = "blocked"
  /\ keep
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.deaths = 0]

\* T4. APPROVE head=<sha> on the current head. A later patch diff sends it back.
T4Approve ==
  /\ "T4" \notin Omit
  /\ state = "review"
  /\ state' = "ship"
  /\ keep
  /\ flags' = [flags EXCEPT !.covered = TRUE]
  /\ counts' = [counts EXCEPT !.deaths = 0]

T4PatchDiffers ==
  /\ "T4" \notin Omit
  /\ state = "ship"
  /\ state' = "review"
  /\ keep
  /\ flags' = [flags EXCEPT !.covered = FALSE]
  /\ counts' = counts

T4ShipMerge ==
  /\ "T4" \notin Omit
  /\ state = "ship"
  /\ flags.covered
  /\ state' = "merged"
  /\ keep
  /\ flags' = flags
  /\ counts' = counts

T4ShipHandoff ==
  /\ "T4" \notin Omit
  /\ state = "ship"
  /\ flags.covered
  /\ state' = "handed-off"
  /\ keep
  /\ flags' = flags
  /\ counts' = counts

T4ShipAfter ==
  /\ "T4" \notin Omit
  /\ state = "ship"
  /\ flags.covered
  /\ state' = "waiting-external"
  /\ keep
  /\ flags' = flags
  /\ counts' = counts

\* T8. ok head=<sha> on the previewed SHA ships. Decline fixes.
\* The third decline is WaitingExternal --> Stuck.
T8Ok ==
  /\ "T8" \notin Omit
  /\ state = "waiting-external"
  /\ state' = "merged"
  /\ keep
  /\ flags' = flags
  /\ counts' = counts

T8Decline ==
  /\ "T8" \notin Omit
  /\ state = "waiting-external"
  /\ counts.declines < 2
  /\ state' = "fixing"
  /\ keep
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.declines = @ + 1, !.deaths = 0, !.gate = 0]

T8DeclineStuck ==
  /\ "T8" \notin Omit
  /\ state = "waiting-external"
  /\ counts.declines = 2
  /\ state' = "stuck"
  /\ keep
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.declines = 3, !.deaths = 0]

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

MergedDone ==
  /\ state = "merged"
  /\ state' = "done"
  /\ keep
  /\ flags' = flags
  /\ counts' = counts

T9Pass ==
  /\ "T9" \notin Omit
  /\ state = "post-merge"
  /\ state' = "done"
  /\ keep
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.deaths = 0]

T9Fail ==
  /\ "T9" \notin Omit
  /\ state = "post-merge"
  /\ state' = "done"
  /\ keep
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.deaths = 0]

MaxOkAnotherRound ==
  /\ state = "capped"
  /\ state' = "fixing"
  /\ keep
  /\ flags' = flags
  /\ counts' = [counts EXCEPT !.deaths = 0, !.gate = 0]

End ==
  /\ state \in Terminal
  /\ UNCHANGED vars

Next ==
  \/ IntakeBrief \/ IntakeRefused
  \/ T5Ask \/ T5Answer \/ T5Abandon
  \/ T1KindGoPlan \/ T1KindGoBuild
  \/ PlanPosted \/ PlanGapOnce \/ PlanGapStuck \/ PlanMapped
  \/ T2ReadyGreen \/ T2CiRed \/ T2CiRedStuck
  \/ T6JobRetry \/ T6JobStuck
  \/ T3Revise \/ T7Blocked
  \/ T4Approve \/ T4PatchDiffers \/ T4ShipMerge \/ T4ShipHandoff \/ T4ShipAfter
  \/ T8Ok \/ T8Decline \/ T8DeclineStuck
  \/ T9PostMerge \/ MergedDone \/ T9Pass \/ T9Fail
  \/ MaxOkAnotherRound \/ End

Spec == Init /\ [][Next]_vars

QuestionsBeforeBuilding ==
  state \in AfterGo => flags.questionsDone

QuestionResumes ==
  /\ state = "waiting-max" => paused \in {"questions", "building", "fixing"}
  /\ state # "waiting-max" => paused = "none"
  /\ paused = "questions" => ~flags.questionsDone /\ cap = 0
  /\ paused = "building" => flags.questionsDone /\ cap = 0
  /\ paused = "fixing" => flags.questionsDone /\ cap \in {1, 3}

MappedBeforeBuilding ==
  /\ state \in {"planning", "plan-check"} => flags.questionsDone /\ ~flags.planOk
  /\ flags.planOk => state \notin {"intake", "refused", "questions", "planning", "plan-check"}

CountersBounded ==
  /\ cap = 0 => counts.round = 0
  /\ cap \in {1, 3} => counts.round <= cap
  /\ counts.gate = 3 => state = "stuck"
  /\ counts.declines = 3 => state = "stuck"
  /\ counts.planGaps = 2 => state = "stuck"
  /\ counts.deaths = 2 => state = "stuck"
  /\ counts.acUnmet <= 2

ContradictoryConfigNeverRuns ==
  ~configOk => state \in {"intake", "refused"}

ShipPinnedToCoveredHead ==
  state \in {"ship", "merged", "handed-off", "waiting-external", "post-merge", "done"}
    => flags.covered

CapAtReview ==
  /\ state \in {"intake", "refused", "questions", "planning", "plan-check", "building"}
       => cap = 0
  /\ state \in ReviewAndAfter => cap \in {1, 3}

Phase1T9Off ==
  ~AllowPostMerge => state # "post-merge"

OrchDoesNotExec ==
  ~HarnessExec

\* Each decided row is enabled in the state the table names.
\* A fixture that puts "T1" in Omit makes T1KindGo false.

T1KindGo ==
  state = "questions" => ENABLED T1KindGoBuild /\ ENABLED T1KindGoPlan

T2Decided ==
  /\ state \in {"building", "fixing"} => ENABLED T2ReadyGreen
  /\ state \in {"building", "fixing"} /\ counts.gate < 2 => ENABLED T2CiRed
  /\ state \in {"building", "fixing"} /\ counts.gate = 2 => ENABLED T2CiRedStuck

T3Decided ==
  state = "review" => ENABLED T3Revise

T4Decided ==
  /\ state = "review" => ENABLED T4Approve
  /\ state = "ship" =>
       ENABLED T4PatchDiffers /\ ENABLED T4ShipMerge
         /\ ENABLED T4ShipHandoff /\ ENABLED T4ShipAfter

T5Decided ==
  /\ state \in {"questions", "building", "fixing"} => ENABLED T5Ask
  /\ state = "waiting-max" => ENABLED T5Answer /\ ENABLED T5Abandon

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

T9Decided ==
  /\ AllowPostMerge /\ state = "merged" => ENABLED T9PostMerge
  /\ state = "merged" => ENABLED MergedDone
  /\ state = "post-merge" => ENABLED T9Pass /\ ENABLED T9Fail

ChartEdges ==
  /\ state = "intake" /\ configOk => ENABLED IntakeBrief
  /\ state = "planning" => ENABLED PlanPosted
  /\ state = "plan-check" /\ counts.planGaps = 0 => ENABLED PlanGapOnce /\ ENABLED PlanMapped
  /\ state = "plan-check" /\ counts.planGaps = 1 => ENABLED PlanGapStuck /\ ENABLED PlanMapped
  /\ state = "capped" => ENABLED MaxOkAnotherRound

\* Drawing bound for the Mermaid chart. The full configs explore every interleaving.
\* This keeps TLC under statechart's 500-state drawing limit and still reaches
\* every section 4.1 name. One risk cap, the small-ticket build, and the plan-check
\* branch. A CI-red streak and a job-death streak are not stacked.
ChartSlice ==
  /\ counts.acUnmet = 0
  /\ ~flags.planOk
  /\ cap \in {0, 3}
  /\ counts.deaths = 0 \/ counts.gate = 0

====
