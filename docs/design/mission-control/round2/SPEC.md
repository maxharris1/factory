# Mission Control — review round (round 2) spec

Approved by Max on 2026-11-16 at 10:45 AM ET: "Designs are approved".

This folder is the second design round for the dashboard. It changes the first-round design where the built dashboard did not scale (222 tickets, 20 slots) or did not fit a phone. Read it together with the first-round packet one folder up.

## 1. Precedence and how to read this folder

- For intent: `../STATUS.md`, then this file, then `../SPEC.md`, then artboards. Where this file and `../SPEC.md` disagree, this file wins.
- For every look and motion value (px, colours, type, spacing, rates): the round-2 artboard named by the ticket wins. Where a round-2 artboard does not show something, the first-round artboard still applies.
- `artboards/` holds one file per approved board. They are mock-ups on a canvas runtime, read the same way as the first round (`../reference/HOW-TO-READ-ARTBOARDS.md`). They import `PixelStrip` from `../reference/artboards/`. `TopBarR2`, `StatusLineR2`, `SlotTileR2`, `JourneyClimbR2` and `FlowMapR2` are the shared pieces; `ShellR2` shows every shell state on one board.
- `screenshots/` holds one still of every board, rendered from the artboard source with a stand-in runtime (not exported from the canvas). Phone boards are 375px wide.
- **Every number, ticket id, time, reason and log line in the artboards is sample data.** Ship real data only. A value the dashboard cannot know is left out or shown as plain text such as `no reading`; it is never invented.
- Removals in this round are deliberate and approved (Max, 2026-11-16: "Allow the removals"). Remove what a ticket names and nothing else.

## 2. Rules that carry over

- Colour means who, pattern means what, green/orange/red mean status, cyan means the path taken, now, or selected.
- Motion is truth: only active work moves; broken, stopped, stale, held and idle things are still; done settles once; waiting on a human is still amber. Reduced motion shows one still frame per state.
- Chips appear only for what is not normal. No `running` chip on a healthy job, no `ONLINE` chip on a healthy device, no `OVERDUE` chip and no `PR`/`ticket` kind tag on tiles.
- Phones (375px) are a real target (Max, 2026-11-16: "Real target"). A phone gets its own layout where the desktop one does not fit. The page never scrolls sideways; a wide thing gets a phone layout or its own scroll box.
- Every panel keeps a server render and a page-script twin that produce the same markup. All text goes through the existing escaping and redaction. No external assets, no new dependencies, CSP unchanged.

## 3. Page decisions

### Shell — `ShellR2.dc.html`, `TopBarR2.dc.html`, `StatusLineR2.dc.html`
1. The attention strip is one row, 36px, at any item count: the red still block, the count (links to Activity), a tally by kind in the kind's tone, the two worst items (bad before warn, then oldest first) each as chip, subject link and detail cut with an ellipsis, and a `+N more` link to Activity at the right.
2. On Activity the strip shows the count and "listed below" only. With nothing to show there is no strip.
3. On a phone the strip is one tappable line that opens Activity: block, count, a short tally, an arrow.
4. Snapshot freshness is a state of the whole page. **Stale** (the snapshot is older than three publish intervals): the link indicator says `STALE`, an amber bar under the band says how old the snapshot is and when the writer last published, the band stops and turns grey, and the status line turns grey and gains an `as of <age> ago` segment. **Lost** (polls are failing): the same in red, with "Cannot reach the dashboard" and the time of the last snapshot. **Paused** (the tab was in the background and no poll has answered since it came back): the indicator says `PAUSED · resuming` in grey and the band is still; no bar. `LINK LOST` is never shown for a tab that was only in the background.
5. The status line is always one row. At 1100px wide or more it is the first-round line. Narrower, it keeps `slots used/total` and `queue`, shows every vital that is not ok in full, and folds the ok vitals into one `N ok` segment that links to System. The phone form is the same with tighter padding. When stale, the device segments fold into `slots` so the age segment fits.

### Lanes — `LanesR2.dc.html`, `LanesR2Phone.dc.html`
1. The KPI row is removed. The Lanes panel header carries the live counts on its right: `6 running · 1 held · 13 idle · 1 queued`. Lifetime done and failed move to Efficiency.
2. Each device row shows only real work as SlotTiles (running, stale, held). Idle slots collapse into one **idle rail** at the end of the row: a single 24px-high line holding one 8px square per idle slot (1px dashed `#414868`, no fill, 3px gap, still) followed by `4 idle` in mono 12px `#8b93b8`. A device with no work is just its head and the rail (about 44px tall instead of a row of 124px tiles). A disabled device's squares are solid `#24283b` and the label reads `4 off`.
3. Device head: name, slot meter, `2 / 6 slots`, harness list. No `ONLINE` chip (normal needs no chip). A probe-down device would show a `PROBE DOWN` bad chip; none is down in this sample.
4. Tile line 1 is bot chip + short ticket id only. The stale tile has a `stale` warn chip, a still decay strip and "no heartbeat" on line 2. A `clarify` job uses the plan pattern (verbs Clarifying, Untangling, Answering) and an `update-from-main` job the fix pattern (verbs Merging, Syncing, Reconciling), as built in #481.
5. Queue lane unchanged in style.
6. Under the lanes, the pair "Just happened" and "Intake". Intake shows trouble and waits first: the webhook line, then errors and refused tickets if any, then the tickets waiting on a blocker (first three rows, the rest in a closed `<details>`), then a closed `<details>` with the tickets fed today.
7. Phone: one device per block, tiles full width stacked, idle rail under them, queue as a list. Header counts wrap under the panel title.

### Tickets board — `TicketsR2.dc.html`, `TicketsR2Phone.dc.html`
1. The board is its own page; the detail is a separate page. Every card id and every shipped tile is a link: open tickets to `TicketR2.dc.html`, shipped to `TicketShippedR2.dc.html`, stopped to `TicketStoppedR2.dc.html`.
2. Toolbar: project filter, board/table toggle, and a "Find a ticket" text input so any tracked ticket can be opened.
3. Six columns in one row: Ask, Plan, Build, Review, Ship, **Stopped**. Stopped is the last column with the red static glyph, always visible; when empty it reads "nothing stopped".
4. Card order inside a column: live jobs first, then queued, then idle, longest in state first. A card without a job never says "no job running"; it shows how long it has been in its state (`in intake 14m`) and, when it waits on a blocker, `waits on #478`. A card whose job is stale shows the `stale` warn chip beside the state.
5. A column shows at most six cards, then a `<details>` "+3 more".
6. Shipped shelf under the board: header `Shipped` with count `206` and sub-line `204 merged · 2 handed off`. The ten newest as tiles in one wrapping row (green settled square, id, `r3`, `2h 14m ago`), newest first, then a closed `<details>` "196 earlier".
7. Phone: a phase bar of six buttons with counts (`Ask 9`, `Plan 0`, `Build 3`, `Review 2`, `Ship 1`, `Stopped 1`), one phase's cards listed at a time (the first phase that has a live job is selected at start), then the Shipped shelf with five tiles and the `<details>`.

### Ticket page — `TicketR2.dc.html`, `TicketShippedR2.dc.html`, `TicketStoppedR2.dc.html`, `TicketR2Phone.dc.html`
The page is keyed by ticket, so it exists for every tracked ticket, shipped and stopped ones included. Breadcrumb `Tickets / factory#474`. The hero names the ticket, its tracker and its issue and PR numbers as plain mono text.
1. **Live** (`TicketR2`, factory#474): same sections as `Job.dc.html` in this order: hero, then Run log and Live run side by side, Journey (`JourneyClimbR2`), Can come next, Full state chart collapsed (`FlowMapR2`). The heartbeat tile is green because the job is healthy; no `running` chip.
2. **Shipped** (`TicketShippedR2`, factory#470): hero strip is settled green and still (`effect="off" done="true"`), chip `merged` (ok). Line: `merged 2h 14m ago · 3 revise rounds · 1h 52m from intake to merge`. Tiles: total time, revise rounds (three pips, all done), hops, cost. No Live run panel; in its place a "Run summary" panel (jobs, tokens in and out, cost). Journey with `ended="shipped"`. "Can come next" is replaced by an "Ended" line: `merged by the poller at 7:56 AM ET`. Run log lists all 13 steps.
3. **Stopped** (`TicketStoppedR2`, cleffy#103): hero strip static red, never moves; chip `stuck` (bad). Line: `stuck in planning for 3d 2h`. Directly under the hero a "Why it stopped" panel: the reason text, when, and "Ways out" (the transitions out of the stopped state, from the chart). Then Run log (3 steps) and Journey with `ended="stopped"`. No Live run panel, no heartbeat tile (tiles: time stopped, hops before it stopped, revise round `none`).
4. **Phone** (`TicketR2Phone`, the live ticket): order is hero (tiles 2x2), Live run, Journey (`compact="true"`), Run log, Can come next, then the state chart in its own horizontal scroll box inside a closed `<details>`. Nothing else scrolls sideways.

### Journey and state chart — `JourneyClimbR2.dc.html`, `FlowMapR2.dc.html`
1. The Journey keeps every first-round behaviour.
2. `ended="shipped"`: no current bar, no NOW marker, no "ahead" dotted line; every bar is in the past style; the last bar sits in the shipped lane and the flag is reached; play the green settle once on load, then everything is still. The ruler ends with the end time, not `NOW`. Caption: `shipped · 12 hops · 1h 52m`.
3. `ended="stopped"`: the last bar sits on the red stopped floor in the still red style; no NOW marker, no sparks, no marching strip, no beat; no "ahead" line. Caption: `stopped in planning · 3d 2h ago`.
4. `compact="true"` (phone): the canvas takes the container's width instead of 1000px (derive the cell count from the measured width; still 4px cells), bars carry no name text, lane labels shorten to `SHP, SHIP, REV, BLD, PLN, ASK, STOP`, steps are spaced evenly (no time scale toggle), height 60 cells. The caption under the canvas names the selected step (state, actor, duration) and tapping a bar selects it. Controls reduce to `replay`.
5. `FlowMapR2`: the grid gains a fifth row above the stop rail (stop rail moves down one row; canvas height 416). Ship column, top to bottom: `ship`, `merge-queue`, `updating`, `awaiting-ok`, `waiting-external`. `blocked` stays on the stop rail at column 3. New edges (from the factory's ship rules): `review>merge-queue`, `merge-queue>merged`, `merge-queue>updating`, `updating>merge-queue`, `updating>review`, `updating>stuck`. Glosses: merge-queue "approved, waiting to merge", updating "merging base in". The build reads states and edges from the real chart; the artboard's edge list is a sketch.

### Activity — `ActivityR2.dc.html`, `ActivityR2Phone.dc.html`
1. The attention strip shows the count only on this page: the panel below already lists the items.
2. Needs attention: bad items first. Each row's still block takes its chip's tone (red for bad, amber for warn).
3. Relay is windowed by ticket: the 12 tickets with the newest activity, newest first. A thread shows its six newest steps; older ones collapse into a `+5 earlier` button at the start of the thread. Each row is a two-column grid: fixed id column, steps wrap in the second column.
4. Every thread ends on where the ticket is now: a live step block with a small moving PixelStrip in that job's pattern and `running 11m 05s`; or a `shipped` chip; or a still red `stuck` chip.
5. Verdict vocabulary: one chip plus plain words, raw outcome only in the `title` tooltip. `queued-review` → chip `routed`, "sent to review" · `queued-plan` → "sent to plan" · `queued-plan-build` → "plan accepted, sent to build" · `merge-queue` → "approved, queued to merge" · `resumed-fix` → "resumed the fix" · `handed-off` → chip `shipped`, "handed off" · `merged` → chip `shipped`, "merged" · `authorized-unstick-*` → chip `unstuck` (accent), "Max unstuck it" · `waiting-max-repeat` → chip `waiting`, "still waiting on Max" · `approve-ci-red` → chip `blocked`, "approved, CI red". Every verdict shows its age.
6. Filters: one compact segmented control in a single row (`all · needs me · shipped · grok wakes`), buttons 28px tall.
7. Phone: each thread is a block: id line, then its steps as a vertical list (one step per line, newest last, at most four, with `+n earlier`).

### Efficiency — `EfficiencyR2.dc.html`, `EfficiencyR2Phone.dc.html`
1. Order: Subscription headroom and "Cost, last 7 days" side by side at the top; then Revise rounds before ship; then Handoff savings (unchanged); then a small "Lifetime" line (jobs done and failed, all time).
2. Headroom is per account, not per device: one row per account with the devices that share it as muted text, the two windows, the reset time, and headroom as a number plus a 20-cell pixel meter. A window without a reading says "no reading" and the headroom shows neutral with `(7-day only)`. Untracked harnesses are one muted line each. No `proposed` chip.
3. Cost panel: four stat tiles (cost, jobs, tokens in with the cached share under it, tokens out), then the per-harness table. Per repo sits in a closed `<details>`.
4. Revise rounds: one horizontal bar per round count, widths proportional to the counts, with the count and its share at the end (`0 rounds · 88 · 43%`). No ticket ids. A link "see shipped tickets" to the Tickets board.
5. Removed: "Who is working now" (Lanes shows it), the recent-jobs table.
6. Phone: panels stack; the headroom table becomes one block per account; tables that remain sit in their own scroll boxes.

### System — `SystemR2.dc.html`, `SystemR2Phone.dc.html`
1. Order by what is broken. First panel "Broken now" (only drawn when something is): one row per down or stale component with its still red block, name and device, state and for how long, last seen, and the reason or "never seen". The no-waiter item appears here too. When nothing is broken the panel is absent.
2. Components matrix next. Abnormal cells carry state and duration in the cell (`down 8h 02m`); normal cells are a quiet green square. The panel header carries version drift when it is clean: `14 configured · 12 up · all on <version>`. The separate Version drift panel appears only when something is behind.
3. "process details" stays as a `<details>`, closed.
4. Poll and Linear budget; then poller.log, newest line first, with error and warning lines coloured; then Rules and limits without the repeated legend and budget text.
5. No empty "Queued jobs with no waiter" panel.
6. Phone: the matrix becomes a stacked list, one block per device, components as rows (name, state, last seen), abnormal rows first. The log sits in its own scroll box with wrapped lines.

## 4. Details approved in the boards that the list above does not spell out

- **Lanes:** the device head is two lines (name and meter, then slot count and harness list); on desktop the idle rail is the last cell of the tile row. The stale tile's `stale` chip sits on line 2 before "no heartbeat". The held tile is still. The webhook line has a neutral `quiet` chip.
- **Tickets board:** a stale card has a still strip, an amber border, the `stale` chip and a "no heartbeat for …" line. A queued card says who it is queued for and how long it has waited. Column headers list their states. The pipeline header reads `N tracked · N open · N shipped`. "Find a ticket" filters the tracked tickets by id and opens the match.
- **Ticket page:** Run log is the left column; the right column holds Live run (or Run summary), Journey, Can come next (or Ended), and the collapsed state chart. On the stopped page "Why it stopped" is full width under the hero. The Run summary has a still "time by who" bar. The breadcrumb's right side shows the start, merge or stop time.
- **Journey:** in an ended mode the selected bar takes a white frame. The shipped Journey plays its draw-in and one green sweep, then stops redrawing. The stopped Journey is fully still and has no replay button. Compact mode shows no ruler labels.
- **Activity:** a ticket queued for a bot ends its thread with a still grey-bordered block (`codex review queued 1m 30s`). A stale job ends with a still block, a `stale` chip and the heartbeat age. The live ending has a full-strength border in the bot's colour. A stopped ticket stays in the Relay until it moves again. The legend gains "where the ticket is now".
- **Efficiency:** the headroom meter is 20 still cells at 5% each, green when both windows have a reading and neutral otherwise. Cost tiles are 2x2. Revise bars are scaled to the largest count; the 0-rounds bar is green.
- **System:** "Broken now" is a table (what, device, state, for, last seen, why); abnormal matrix cells link to its rows. Poll and Linear budget sits beside poller.log. A traceback in the log stays in reading order as one block even though entries are newest first.

## 5. Data each change needs

Real data only. Where the snapshot does not carry a value yet, the ticket adds it to the snapshot from the database, or leaves the element out when the source is absent.

- Ticket page for every ticket: each ticket's visits (state, actor, duration, reason) and, for shipped tickets, totals (jobs, tokens, cost where reported, elapsed). The page set is written by the dashboard writer as static files, so the design must stay within a bounded size as tickets grow.
- Shipped shelf: when each ticket shipped. Tickets board: time in the current state; what a waiting ticket waits on.
- Relay: verdict ages; the mapping from raw outcomes to the plain words in section 3 (raw outcome kept in the tooltip; an outcome with no mapping shows its raw text).
- Headroom per account: which devices share an account.
- "Broken now": the reason is shown only when health recorded one.
- Stale and lost shell states: the snapshot's age and the publish interval already in the snapshot.
