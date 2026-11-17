# Mission Control design packet status

- Packet written against `main` `e9d3683` plus the #100 branch. Since then #100 (job telemetry), #124, #125, and #126 (GitHub App approval login, switch still off) merged (`main` `c087676`).
  `LIVE-RUN-FOLLOWUP.md` is tracked as issues #147 and #148, not a PR comment.
- Issues and order: T1 #138, T2 #139, T3 #140, T8 #141, T4 #142, T6 #143, T7 #144, T5a #145, T5b #146, T9 #149;
  data D1 #150 (transition times), D2 #151 (subscription headroom), D3 #152 (ship mode and round cap), LR1 #147, LR2 #148.
- Precedence: `STATUS.md` > `SPEC.md` > artboards for intent; artboards win for every look and motion value.
- **Max's rule (2026-11-13, ~6:10 PM ET):** "copy the UI that Claude created one-to-one, and if anything we add to it, we shouldn't be
  removing anything. We should make sure that we have the functionality to support the components proposed in the design."
  So: additions are allowed, nothing in the design is removed, every component gets real backing data, no permanent placeholders.
  Since 2026-11-16 the "nothing in the design is removed" part is lifted for removals that a review-round ticket names; see
  `Review round (2026-11-16)` below.
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
- **D3 #152 (data):** `status.json` now has `projects` (tracker, ship mode, approver, low and high round caps per `orch.yml`
  project, or `status: unknown` with a `reason`). It backs SPEC 3.6 item 5's "[not shown by the dashboard today]" values; #141 renders it.
- **D2 #151 (data):** `status.json` now has `usage` (5-hour and 7-day used, resets_at, headroom, observed_at per bot account),
  captured from every job. Max's picks (2026-11-14): Claude reads Claude Code's `get_usage` control reply, sent once at each job's
  start; Codex reads the `rate_limits` of its own session rollout's `token_count` events. A login that cannot report limits, or a
  window the CLI leaves null, shows `no reading`. Cursor shows `not tracked`.
- **Later (2026-11-13, 6:30 PM ET):** Max said to get these into the factory queue ("get these into the factory que").

## Review round (2026-11-16)

Claude reviewed the built dashboard against the design on 2026-11-16. Max's decisions (morning of 2026-11-16 ET, answering Claude's
questions before the tickets were filed):

1. Removals: Max chose "Allow the removals". The 2026-11-13 rule "we shouldn't be removing anything" is lifted for this round only:
   a review-round ticket may remove a named element. Anything a ticket does not name stays.
2. Ticket page scope: Max chose "Every ticket". The ticket page will open for all tickets, shipped history included, and the board
   and the detail become separate views. This is not filed yet.
3. Order of work: Max chose "Bugs now, mock the rest". Pure bug fixes are filed now. Design changes (one-row attention strip,
   collapsed idle slots, capped Shipped shelf, Stopped placement, snapshot freshness state, narrow status line, ticket-keyed detail
   page, phone layouts, Efficiency and System reorder, Relay windowing) are mocked on the design canvas first and filed after Max
   signs off, with artboards.
4. Queueing: Max chose "Label them all". The bug tickets are queued together and chained with `Depends on` so they build one at a
   time.
5. Phones: Max chose "Real target". A 375px-wide phone gets its own layout where the desktop one does not fit; horizontal page
   scroll is a bug.
6. Re-key timing: Max chose "After the mock-up round". Keying the detail page by ticket waits until the shipped-ticket and
   stopped-ticket variants are mocked.

## Round 2 approved (2026-11-16)

Max approved the round-2 mock-ups on 2026-11-16 at 10:45 AM ET: "Designs are approved". The packet is `round2/` (`SPEC.md`,
`artboards/`, `screenshots/`). Precedence for intent is now `STATUS.md` > `round2/SPEC.md` > `SPEC.md` > artboards; a round-2
artboard wins for look and motion wherever one exists, and the first-round artboards apply elsewhere. All data in the round-2
artboards is sample data. The design tickets that build this round each name their artboards and depend on the packet being merged.
