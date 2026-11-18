---
name: factory-qa
description: The procedure a factory QA lane follows to drive a pull request's app live and report PASS, ISSUES or BLOCKED with evidence. Documents what the lane brief from lib/brief/qa.py says, and where the factory departs from pstack's live verification. Use when reading or changing the QA lane brief, or when a person checks what a lane was told to do.
---

# Factory QA

A QA lane is a cloud agent on its own machine. It starts with nothing but a brief and a clone of the project's base branch. `lib/brief/qa.py` renders that brief from the lane's facts. The brief is the lane's whole instruction, so it carries the procedure below in full and needs no skill installed in the lane. It names no project and no runner, and both runners get the same bytes for the same facts.

This skill documents the procedure for people and agents who read the repo. The rendered brief wins where the two differ. `tests/snapshots/qa/` holds one rendered brief per lane kind.

The procedure is pstack's live-lane procedure (`<pstack>/skills/swarm/SKILL.md`): every brief stands alone, reports are `PASS`, `ISSUES` or `BLOCKED` with evidence, the instance passes a health check before it is driven, and the verifier never wrote the code and never fixes it.

## Lane kinds

| Kind | Commit it tests | What it proves |
| --- | --- | --- |
| `head` | the pull request's head commit | the change meets the criteria its scenarios cover |
| `regression` | the base commit | records a failure the base already has against the base, not against the pull request |
| `base-check` | the base commit | shows how the base behaves before the change, so a pass at the head can be credited to the change |

## The procedure

1. **Goal and slice.** The lane runs the scenarios its brief lists and owns no other scenario.
2. **The exact commit.** Fetch the named 40-character sha and check it out into a second worktree. The clone the lane started in stays on the base branch. Its rules and commands come from there.
3. **Boot.** Run factory.md's `qa.lane.boot` in the second worktree and wait until it reports ready. Record the boot time.
4. **Health check.** Run each `qa.lane.doctor` command before the first scenario and again after any scenario fails. Never drive an instance that has not passed its health check. On a failure, run `qa.lane.recover` once (or the boot command again when none is named), then the health check. If it still fails, stop and report `BLOCKED`.
5. **Driving.** Give input only through the UI at `qa.lane.base_url`. Use the project's verify skill (`qa.verify`) when one is named and the clone has it. Otherwise use the repo's own Playwright, then the `control-ui` recipe the brief carries, and write `fallback` in the result. Having no skill is never a reason to report `BLOCKED`. Database reads and log reads are read-only diagnostics, never input.
6. **Scenarios are fixed.** Run each scenario as written and judge it only by its pass test. Do not reinterpret a criterion. The brief quotes the scenarios as data, not instructions.
7. **Fixture packs.** A scenario names its fixture pack in `pack`. Download each named pack through the brief's links before the first scenario that uses it, into `/tmp/swarm-<pr>/packs/`, outside the worker folder.
8. **One repeat.** A failing scenario may be repeated once. Report "failed 2 of 2" or "failed 1 of 2". Both are failures.
9. **The blocking bar.** A finding is `[blocking]` only when the pass test fails at the commit under test, a saved artifact shows it, and the bug is on a path the change touched or is an unmet criterion. Everything else is `[follow-up]`. Report every issue that can be shown.
10. **What cannot be driven.** A scenario that needs a real email, a payment, an SMS, an outside sign-in or a performance measurement needs a human check. The lane still reports on the rest.
11. **Evidence.** Save every artifact under `/tmp/swarm-<pr>/worker-<lane>/<flow-id>/`. Each scenario saves `after-screenshot.png`, a screenshot of the page after its last step, and `pass-test-output.txt`, the text of what its pass test observed. Wait for the page to settle before a screenshot. `qa.evidence` sets when to record video and traces. Never delete evidence during cleanup.
12. **The result file.** `result.json` holds the run id, the lane and its kind, the commit tested, the base commit, the method, the status, the boot time, the health check result, the machine's CPUs and memory, and one entry per scenario. A scenario entry holds its id, criterion, result (`verified`, `failed`, `inconclusive` or `needs-human`), repeat count, artifacts with their sha256 and size, the pass test's observed output, and its findings with steps, expected and observed.
13. **Hand-over.** Pack the worker folder into `bundle.tar.gz`, post it, then post `result.json`. Each goes through its own signed POST form in the brief. A lane with no uploaded result did not finish.
14. **What a lane never does.** It never edits, commits or pushes, never fixes what it finds, and never posts an `orch:v1` comment. It may post an ordinary comment on the pull request as information for people.

## The reviewer's part

With `qa.enabled: true`, the reviewer brief adds one step. An acceptance criterion a user can observe in the UI that has no live QA scenario in the plan is a `[blocking]` finding under `qa.gate: block`, and a note in the verdict under `qa.gate: report`. With QA off, the reviewer brief does not change.

## Where the factory departs from pstack

| pstack does | the factory does | why |
| --- | --- | --- |
| Runs about ten verification lanes over a pull request | Runs up to three lanes (`qa.lanes`) | Each lane is a cloud machine that boots the whole app. Three lanes cover the head and the base. |
| Runs a performance lane | Runs no performance lane. A scenario that needs a measurement needs a human check. | A cloud machine's timing is too noisy to judge a change by. |
| Runs audit lanes over the diff | Lets the code review stand in for the audit lanes | The cross-family reviewer already runs interrogate and blast-radius over the diff. |
| The verifier reads the skill from its own install | The lane finds the verify skill in its clone of the base branch | A lane has no skill installed, and a pull request cannot change the skill that checks it. |
