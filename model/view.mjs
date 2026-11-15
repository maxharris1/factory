export const title = "Ticket loop";

export const colors = {
  Ask: "blue",
  Plan: "purple",
  Build: "orange",
  Review: "yellow",
  Ship: "teal",
  Stop: "tan",
};

export const state = (v) => v.state;

export const group = (v) => {
  const name = v.state;
  if (name === "intake" || name === "questions" || name === "waiting-max") return "Ask";
  if (name === "planning" || name === "plan-check") return "Plan";
  if (name === "building" || name === "fixing") return "Build";
  if (name === "review" || name === "capped") return "Review";
  if (
    name === "ship" ||
    name === "waiting-external" ||
    name === "awaiting-ok" ||
    name === "merged" ||
    name === "handed-off" ||
    name === "merge-queue" ||
    name === "updating" ||
    name === "post-merge" ||
    name === "done"
  ) {
    return "Ship";
  }
  return "Stop";
};

export const step = (action, from, to) => {
  if (action === "End") return null;
  if (action === "IntakeRefused") return "project config invalid";
  if (action === "IntakeBrief") return "brief and acceptance criteria posted";
  if (action === "T5Ask") return "a product question";
  if (action === "T5Answer" && to.state === "questions") return "Max answers";
  if (action === "T5Answer") return "answer, same step resumes";
  if (action === "T5Abandon") return "Max drops it";
  if (action === "T1KindGoPlan") return "kind=go, queue plan";
  if (action === "T1KindGoBuild") return "kind=go, queue build";
  if (action === "T1PPlanPosted") return "trusted builder posts kind=plan";
  if (action === "T1PPlanGapOnce") return "an acceptance criterion is unmapped";
  if (action === "T1PPlanGapStuck") return "gaps a second time";
  if (action === "T1PPlanMapped") return "every acceptance criterion mapped";
  if (action === "T2ReadyGreen") return "ready, CI green";
  if (action === "T2CiRed") return "CI red, fix";
  if (action === "T2CiRedStuck") return "CI red three times";
  if (action === "T6JobRetry") return "job dies, retry once";
  if (action === "T6JobStuck") return "job dies twice (T6)";
  if (action === "T6DeviceOffline") return "device offline 60 minutes";
  if (action === "T3Revise" && to.state === "fixing") return "REVISE, under the round cap";
  if (action === "T3Revise") return "REVISE at the cap";
  if (action === "T3Unsat") return "REVISE with unsat=, the criterion goes to Max";
  if (action === "T7Blocked") return "BLOCKED";
  if (action === "T4Approve") return "APPROVE on the current head";
  if (action === "T4HeadMoves") return "a new head is pushed";
  if (action === "T4PatchDiffers") return "head changed and the patch differs";
  if (action === "T4ShipMerge") return "ship = merge";
  if (action === "T4ShipHandoff") return "ship = handoff";
  if (action === "T4ShipAfter") return "ship = after an approver";
  if (action === "T4Hold") return "ship = merge or handoff and the PR is held";
  if (action === "T4HoldOk") return "approver's OK at the head, or approver removes a label-only hold";
  if (action === "T4HoldOkQueue") return "approver's OK at the head, finish_line merge";
  if (action === "T4HoldOkHandoff") return "approver's OK at the head, ship = handoff, finish_line ready-for-review";
  if (action === "T4HoldPatchDiffers") return "head changed and the patch differs";
  if (action === "T4HoldBaseUpdate") return "PR dirty or behind, base update";
  if (action === "T8Ok") return "OK names the previewed SHA";
  if (action === "T8Decline") return "declined";
  if (action === "T8DeclineStuck") return "3rd decline";
  if (action === "T9PostMerge") return "project has a post-merge check";
  if (action === "MergedDone") return "merged";
  if (action === "T9Pass") return "post-merge passes";
  if (action === "T9Fail") return "post-merge fails, follow-up filed";
  if (action === "MaxOkAnotherRound") return "Max okays one more round";
  if (action === "MaxGoAfterBlocked") return "approver's go, fresh review at the same round";
  if (action === "ReenterAfterHandoff") return "trusted ready at a new head, within the effective cap";
  // A merge seen in any state ends the ticket (#108); the chart draws only the
  // handed-off one, and section 4.1 says the rest in prose. From merge-queue
  // the QueueMerge arrow's label names it.
  if (action === "MergeObserved") return from.state === "handed-off" ? "PR merged" : null;
  if (action === "T4ShipQueue") return "APPROVE, finish_line merge";
  if (action === "QueueMerge") {
    return "poller squash-merges the queue head (clean, CI green, approval covers the head), or the merge is observed";
  }
  if (action === "QueueConflict") return "PR conflicts with base, update-from-main queued";
  if (action === "QueueClosed") return "PR closed unmerged";
  if (action === "QueueGoPlan") return "trusted kind=go, queue plan";
  if (action === "QueueGoBuild") return "trusted kind=go, queue build";
  if (action === "UpdateTrivial") return "trivial update, approval carried";
  if (action === "UpdateNonTrivial") return "non-trivial update, cross-family re-review";
  if (action === "UpdateRetry") return "update fails, one more update";
  if (action === "UpdateStuck") return "two failed updates (update-failed-2)";
  return action;
};
