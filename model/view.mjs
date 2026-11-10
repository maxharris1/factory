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
    name === "merged" ||
    name === "handed-off" ||
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
  if (action === "PlanPosted") return "plan maps each acceptance criterion";
  if (action === "PlanGapOnce") return "an acceptance criterion is unmapped";
  if (action === "PlanGapStuck") return "gaps a second time";
  if (action === "PlanMapped") return "every acceptance criterion mapped";
  if (action === "T2ReadyGreen") return "ready, CI green";
  if (action === "T2CiRed") return "CI red, fix";
  if (action === "T2CiRedStuck") return "CI red three times";
  if (action === "T6JobRetry") return "job dies, retry once";
  if (action === "T6JobStuck") return "job dies twice (T6)";
  if (action === "T3Revise" && to.state === "fixing") return "REVISE, under the round cap";
  if (action === "T3Revise" && from.counts.acUnmet === 1 && to.counts.acUnmet === 2) {
    return "a second AC-unmet verdict";
  }
  if (action === "T3Revise") return "REVISE at the cap";
  if (action === "T7Blocked") return "BLOCKED";
  if (action === "T4Approve") return "APPROVE on the current head";
  if (action === "T4PatchDiffers") return "head changed and the patch differs";
  if (action === "T4ShipMerge") return "ship = merge";
  if (action === "T4ShipHandoff") return "ship = handoff";
  if (action === "T4ShipAfter") return "ship = after an approver";
  if (action === "T8Ok") return "OK names the previewed SHA";
  if (action === "T8Decline") return "declined";
  if (action === "T8DeclineStuck") return "3rd decline";
  if (action === "T9PostMerge") return "project has a post-merge check";
  if (action === "MergedDone") return "merged";
  if (action === "T9Pass") return "post-merge passes";
  if (action === "T9Fail") return "post-merge fails, follow-up filed";
  if (action === "MaxOkAnotherRound") return "Max okays one more round";
  return action;
};
