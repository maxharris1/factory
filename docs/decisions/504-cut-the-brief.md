- **Decision:** A brief carries only the text its job needs: the Review rules go to review briefs only, and each repeated rule keeps one statement.
- **When:** 2026-11-16 3:52 PM ET, by Max (go on the plan); the cuts are Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/599#issuecomment-6928992419

## Size

The box half of `tests/snapshots/brief/build.txt` was 10,373 bytes at `14dacef` (and at `793b150`). It is 8,762 bytes now, 15.5% smaller. `tests/test_brief_cut.py` pins the 15% floor.

## Sentences removed

No removed sentence was the only statement of its rule. Each row names where the rule is still stated.

| Removed sentence | Where it was | Still stated in | Why it was cut |
| --- | --- | --- | --- |
| The whole `## Review rules (docs/SPEC.md section 8)` section: one reviewer; the verify skill, interrogate and blast-radius; no second reviewer or Judge; risk rounds; gate retries and three CI-red cycles; ship pinned; another agent's approval; the `ac=` and `verdict=` definitions | Plan, build, fix, CI-fix, merge-only, resume and retry briefs | Every review brief, and `docs/SPEC.md` section 8 | It is written to the reviewer. A builder acts on none of it. The one sentence a builder needs, "Builders do not fix `[follow-up]` findings.", moves to the builder's Rules section when follow-ups are filed. |
| "That reviewer marks a finding `[blocking]` only when they have run it and seen it fail." | Review rules, every brief | Review step: "Mark a finding `[blocking]` only when you have run it and seen it fail, and give the repro." | The review brief said it twice. |
| "Blocking bar (`review.block_on: user-impact`): only an unmet acceptance criterion, ... Every other finding is `[follow-up]`, from round 1." | Review rules, user-impact review briefs | Review step 5 (`_bar_step`), which states the same bar in full and says "Mark every other finding `[follow-up]`, not `[blocking]`." | The review brief said it twice. |
| "Tests: run only the tests for the files you touched plus a short smoke set, in the foreground. Then push and post `ready`." | Builder notes, all builder briefs | Each kind's own test step (build and fix step 3, merge-only step 4), which now also says "in the foreground", and step 5 (push, then post `ready`) | The test-scope rule was stated two or three times. "CI runs the full suite and is the gate ..." from the same note stays. |
| "Never run the whole test suite locally, in the background, or in chunks, and never poll, watch or wait on that suite, on GitHub Actions or on any other CI from inside this job." | Builder notes | The test step now says "never the whole suite, in the background or in chunks". The note keeps "Never poll, watch or wait on GitHub Actions or any other CI from inside this job." | The scope half repeated the test step. The polling half stays once. |
| "CI after `ready` is the full-suite gate." | Builder notes | The kept note: "CI runs the full suite and is the gate"; `docs/SPEC.md` | Same fact twice in one note. |
| "Do not poll, watch or wait on GitHub Actions or any other CI from inside this job: the factory's own review and CI gate check CI after `ready`." | Merge-only task | The builder notes in the same brief | Merge-only stated the polling rule twice. |
| "never wait on it" (CI-fix step 2; review-fix CI rerun clause) | Fix briefs | The builder notes' polling rule | A third statement of the polling rule. |
| "If something left open needs a decision, post a `questions` handoff." | Plan and builder briefs | The Handoff section: "If a product question blocks the work, post a questions handoff instead" | Stated twice. The dependency line ("If the ticket lists a dependency that has not merged ...") stays: it is the only statement of that rule. |
| "... or message another bot." (Finish section, now "Never start another factory job.") | Every brief | Rules: "Message no other bot." | Stated twice. |
| `- reviewer harness: ...` and `- verify skill: ...` | Job section of builder briefs | Job data (`reviewer_harness`, `verify_skill`) and the review brief's Job section | Reviewer facts. A builder does not run the verify skill. |
| `- round: not set for this job` | Job section of builder briefs with no round | Nothing to state: the line said a value is absent. A set round still shows. Review briefs keep the line. | Empty line for a builder. |
| The open findings in Job data `evidence.findings.findings` (now "(quoted in the What to do section)") | Review-fix briefs with quoted findings | The What to do section quotes each finding's text and rule | Each finding appeared once as prose and twice in Job data. `reviewed_base`, `reviewed_head` and `verdict_url` stay. |

## Left as is

- The model lines: `skills.section` already renders only the brief's own harness. `tests/test_brief_cut.py` now pins it for both harnesses.
- The criteria in a fix brief's Job data (`criteria.criteria` and `criteria.text`): no criterion covers them.
- `evidence.instructions.findings` in Job data of a fix resumed after answers: with `evidence.findings.findings` marked, it is at most one copy.
- No kept sentence was reworded. Merge-only step 4 gained "in the foreground; never in the background or in chunks" but not "never the whole suite", because its next sentence allows the full suite when the affected tests are the full suite.
