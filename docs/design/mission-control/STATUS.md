# Mission Control design packet status

- Packet written against `main` `e9d3683` plus the #100 branch. Since then #100 (job telemetry), #124, #125, and #126 (GitHub App approval login, switch still off) merged (`main` `c087676`).
  `LIVE-RUN-FOLLOWUP.md` is tracked as issues #147 and #148, not a PR comment.
- Issues and order: T1 #138, T2 #139, T3 #140, T8 #141, T4 #142, T6 #143, T7 #144, T5a #145, T5b #146, T9 #149;
  data D1 #150 (transition times), D2 #151 (subscription headroom), D3 #152 (ship mode and round cap), LR1 #147, LR2 #148.
- Precedence: `STATUS.md` > `SPEC.md` > artboards for intent; artboards win for every look and motion value.
- **Max's rule (2026-11-13, ~6:10 PM ET):** "copy the UI that Claude created one-to-one, and if anything we add to it, we shouldn't be
  removing anything. We should make sure that we have the functionality to support the components proposed in the design."
  So: additions are allowed, nothing in the design is removed, every component gets real backing data, no permanent placeholders.
- **Max's decisions (2026-11-13):**
  1. Lanes job modal: **dropped**; slot tiles link to the ticket page (6:10 PM ET, confirmed 6:11 PM ET).
  2. #100's step feed and per-tool counts in the job view: **removed** to match the design (6:10 PM ET, confirmed 6:11 PM ET: "they're not even in the application or functioning at this point").
  3. #100's 7-day tokens and cost panel on Efficiency: **kept and restyled** with the design's recipes (an addition).
  4. `capped` tickets go in **Stopped** (SPEC 3.2's phase list put it in Review; this overrides it).
  5. `stuck` tickets **always go in Stopped** (overrides "stuck tickets stay in their column").
  6. **Add a Ship column** after Review for in-flight ship states: ship, awaiting-ok, waiting-external, post-merge.
  7. Live run data: **build now** (#147). This is Max's OK to partly reverse r10's no-text rule for task-list text, current
     task and sub-agent description and type only, still scrubbed and redacted.
  8. File the tickets but do not start them until Max says so.
- **Open question for Max:** the subscription headroom usage source per harness (#151 reports what each CLI exposes, then Max picks).
- **Later (2026-11-13, 6:30 PM ET):** Max said to get these into the factory queue ("get these into the factory que").
