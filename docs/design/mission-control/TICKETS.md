# Proposed tickets

Run these **one after another**. All of them edit `lib/dashboard_page.py`; parallel work will conflict.
Before filing, check for open PRs that touch `lib/dashboard_page.py`, `lib/dashboard.py` or `lib/dashboard_flow.py`
(PR #100 touches all of them: land or rebase around it first).

Every ticket's definition of done includes: matches its artboard at 1440px wide and wraps cleanly at 375px;
server render and script render produce the same markup; `prefers-reduced-motion` shows still frames;
no external assets; existing dashboard tests pass; new tests listed below pass.

---

## T1 · Pixel engine and groundwork
**Build:** the `PixelStrip` module in the SCRIPT (effects, palette, Bayer dither, one-shots), one shared rAF loop, the
strip registry keyed by element id that survives the 1.5 s re-render, visibility pausing, the state-to-effect table,
CSS for `.px` and its gradient-only no-script fallback.
**Reference:** `PixelStrip.dc.html`, `Main.dc.html`, `Playground.dc.html`; SPEC 2, 6.
**Accept:** every effect in SPEC 6 renders as in `Main.dc.html`; a strip does not restart when its panel is rebuilt; 20 fps cap; hidden tab pauses the loop; reduced motion gives one still frame per state.
**Tests:** state-to-effect table (no broken state maps to an animated effect); `.px` markup and attributes; CSP unchanged.

## T2 · Global shell
**Build:** TopBar (pixel caret by link state, nav labels Lanes · Tickets · Activity · Efficiency · System, header band tied to running / total slots), attention strip on every page, StatusLine on every page (sticky bottom).
**Reference:** `TopBar.dc.html`, `StatusLine.dc.html`, `HeaderBand.dc.html`; SPEC 3 (file names stay), 4.
**Accept:** attention strip lists the same items as today's badge count and hides when clear; status line shows poll, poll errors, linear, webhook, feeder, components, version drift from the snapshot; file names and anchors unchanged.
**Depends on:** T1.

## T3 · Lanes page
**Build:** stat tiles with decrypt and burst on change; lanes as rows with a queue lane; `SlotTile` for every slot state; Just happened; Intake.
**Reference:** `Lanes.dc.html`, `SlotTile.dc.html`, `Bits.dc.html`; SPEC 3.1, 7.
**Accept:** each device is one row and 13 slots fit above the fold at 1440 x 900; a real heartbeat fires the ripple; build fill follows elapsed / timeout; stale decays with heartbeat age; dead, overdue and stuck are static red; no "running" chip on healthy jobs; verbs rotate every 4 s and never on stale or dead tiles.
**Open question for Max:** keep the job modal on Lanes, or let the tile link straight to the ticket page (the mock links).
**Depends on:** T1, T2.

## T4 · Tickets board
**Build:** `job.html` default view becomes the board (filters, four phase columns, ticket cards, shipped shelf, stopped group) with the old tickets table behind the "table" switch.
**Reference:** `Tickets.dc.html`; SPEC 3.2.
**Accept:** every tracked ticket appears exactly once; a stuck ticket stays in its phase column with a static red strip and its reason; live cards animate by step and bot; counts in headers and filters are correct.
**Depends on:** T1, T2.

## T5a · Journey data
**Build (in `lib/dashboard.py` / `lib/dashboard_flow.py`):** per visit: state, who (job bot, or `script` when no job backs the state), started_at and duration where known, off-chart flag, the transition's reason text; shortest path length to a shipped end state. Decide and document how to record transition times for states with no job (today "the database records no transition times").
**Accept:** `job_flow` (or a sibling) returns the list above; visits with unknown times say so explicitly rather than guessing.
**Depends on:** none (can go before T5b at any point after PR #100 settles).

## T5b · Ticket detail page
**Build:** breadcrumb, hero, run log, Journey, Can come next, collapsed Full state chart, with the run log and the journey driven by one list and one selection.
**Reference:** `Job.dc.html`, `JourneyClimb.dc.html`, `FlowMap.dc.html`; SPEC 3.3, 7.
**Accept:** picking a log row frames and flashes the matching bar and the reverse; default selection is the current visit; the journey draws in once, then only the current bar and marker move, plus a beat on each real heartbeat; no ring animation; nothing ahead of now is named; even-steps is the default; to-scale is offered only when every visit has a duration; chart colours come from who did the step, never from the phase.
**Depends on:** T1, T2, T5a.

## T6 · Activity page
**Build:** full Needs attention panel; Relay threads merging handoffs and verdicts per ticket; filters; settle on a new shipped verdict.
**Reference:** `Activity.dc.html`; SPEC 3.4.
**Accept:** every handoff and verdict in the snapshot appears in exactly one thread in time order; bot-to-bot steps are dashed; the tickets table and poller.log no longer appear here.
**Depends on:** T1, T2, T4 (tickets table moved), T8 (log moved).

## T7 · Efficiency page
**Build:** waffle, jobs lifetime bar, revise rounds before ship, who is working now, the proposed Subscription headroom panel with placeholders.
**Reference:** `Efficiency.dc.html`; SPEC 3.5.
**Accept:** the waffle's lit cells equal the scripted share rounded to a whole percent; the definition texts are unchanged; headroom shows `[not tracked yet]` and is labelled `proposed`.
**Depends on:** T1, T2. Headroom data is a later ticket (after PR #100).

## T8 · System page
**Build:** component matrix, process details (same row ids), version drift, poll and Linear budget (moved), poller.log (moved, coloured lines), rules and limits, no-waiter panel.
**Reference:** `System.dc.html`; SPEC 3.6.
**Accept:** attention links still land on the right row; a component not on the newest version is flagged; a down component is static red.
**Depends on:** T1, T2.

## T9 · Live run panel
**Build:** the panel on the ticket page.
**Reference:** `RunTape.dc.html`; SPEC 7; `LIVE-RUN-FOLLOWUP.md`.
**Accept:** tokens, cost, files touched and commands come from job telemetry; current task, plan and sub-agents come from the follow-up data; with telemetry off or absent the panel says "no telemetry" and nothing is invented.
**Depends on:** PR #100 merged, the follow-up tickets from `LIVE-RUN-FOLLOWUP.md`, T5b.

---

## Suggested order
T1 → T2 → T3 → T8 → T4 → T6 → T7 → T5a → T5b → T9.
T3 first because Lanes is the page Max looks at most. T9 last because its data does not exist yet.
