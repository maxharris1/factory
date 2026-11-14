# Factory Mission Control — refactor spec

Approved by Max on 2026-11-13. The artboards in `reference/artboards/` are normative for look and motion;
this document explains intent, behaviour, data and how to port. Exact tokens and recipes are in
`reference/DESIGN-TOKENS-AND-DATA.md` (sections 3 and 4).

## 1. Goal

Mission control for a fleet of coding agents (claude, codex, cursor, grok) in fixed slots on three devices.
The operator needs, in this order: what needs me, what is every bot doing, where is every ticket, is the tool healthy.
The refactor keeps the current look (Tokyo Night, square corners, Geist and JetBrains Mono) and adds a pixel-grid
motion language, a clearer information structure, and a ticket page that tells the ticket's story.

## 2. Principles (requirements)

**Design decisions**
1. **Colour = who.** Bot hues only for bots: claude `#e0af68`, codex `#7aa2f7`, cursor `#bb9af7`, grok `#73daca`. Work done by a script, not a bot, is `#a9b1d6`. Phases do not get hues (today the state chart reuses bot colours; remove that).
2. **Pattern = what.** One pixel pattern per kind of work, everywhere: plan = drifting dither field, build = bricks filling, review = scanner, fix = patch pass, claimed = 3x3 spinner, stale = decay, trouble = static red dither, idle = faint twinkle, done = green settle.
3. **Status = `ok` green, `warn` orange, `bad` red** only. Cyan `#7dcfff` means "path taken / now / selected".
4. **Attention is global**: the needs-attention items sit in a strip under the top bar on every page.
5. **Tool vitals are global**: a status line at the bottom of every page.
6. **Lanes are rows**: one horizontal row of slot tiles per device.
7. **Tickets are the unit, jobs are steps**: one Tickets board replaces the Jobs list and the tickets table.
8. **Feeds are grouped by ticket**: handoffs and verdicts merge into relay threads.
9. **Health becomes System** and absorbs the poll panel and poller.log.
10. **A healthy running job shows no "running" chip.** Chips appear only for states that are not normal.

**Motion rules**
1. Only active work moves (running, claimed, queued, live).
2. Broken never moves: stuck, blocked, dead, overdue, capped, refused, no waiter, down are static.
3. Done settles: one green sweep (~600 ms), then still.
4. Waiting on a human is still and amber.
5. Motion encodes truth: fill = elapsed / timeout, ripple = a real heartbeat, conveyor speed = real wait age. No decorative loops on data.
6. Calm speed: nothing loops faster than ~6 Hz; no full-panel flashing. One-shots may run faster than a loop but stay under ~500 ms.
7. `prefers-reduced-motion`: one still frame per state; one-shots are skipped.

## 3. Navigation and pages

Nav labels: **Lanes · Tickets · Activity · Efficiency · System**. Keep the existing file names so links and
`_HEALTH_LINK` anchors keep working: `index.html` (Lanes), `job.html` (Tickets, and a ticket's detail at `#job-<id>`),
`activity.html`, `efficiency.html`, `health.html` (System). Every page = TopBar, `<main>` (max-width 1600px,
padding 20px 24px 32px, 16px gaps), StatusLine. Design width 1440px; all layouts wrap down to a phone.

### 3.1 Lanes (`Lanes.dc.html`) — replaces `_kpis, _lanes, _queue`
1. **Factory status**: five stat tiles (running, queued, done, failed, stuck / overdue). A changed number plays the digit "decrypt" roll and a cell burst (see `Bits.dc.html`).
2. **Lanes and slots**: a column of lane rows. First a **queue lane** (conveyor strip; still when the queue is empty; queued items ride it, faster with wait age; "no waiter" is static red). Then one row per device: a 168px header cell (name, state chip, load meter of used / max slots, "n / m slots", harness line) and a grid of `SlotTile`s (`repeat(auto-fill, minmax(180px, 1fr))`, 8px gap). DISABLED, PLACEHOLDER and UNLISTED devices keep today's treatment; a device whose probe is down shows a static red meter.
3. **Just happened** (six newest handoffs; bot-to-bot ones carry a grok-tinted "grok wake" chip) and **Intake** (Linear webhook line; feeder line with a meter; three newest fed lines; the rest in a `<details>`).

### 3.2 Tickets (`Tickets.dc.html`) — replaces `_jobs` and `_tickets`
1. Filter row: project filters with counts; board / table switch (table = today's tickets table).
2. **Pipeline** board: columns Ask, Plan, Build, Review. Header = 16px phase glyph (a tiny PixelStrip), name, count, the state names in that phase. A ticket card shows: an 8px strip when a job is live (effect by step, tint by bot), ticket id (links to its detail), tracker tag, state, round pips, bot chip, device, T+. Stuck tickets stay in their column with a static red strip, a `stuck` chip and the reason. Empty column = dashed box.
3. **Shipped**: handed-off and merged as compact settled tiles (green block, id, revise-round count). **Stopped**: refused, abandoned, blocked, capped.

Phase membership: Ask = intake, questions, waiting-max · Plan = planning, plan-check · Build = building, fixing · Review = review, capped · Ship = ship, awaiting-ok, waiting-external, merged, post-merge, handed-off, done · Stop = refused, abandoned, blocked, stuck.

### 3.3 Ticket detail (`Job.dc.html`) — replaces `_job_article` / `_jobview`
1. Breadcrumb (Tickets / id, job id on the right).
2. **Hero**: a 40px strip in the running step's pattern and the bot's colour; ticket id, bot chip, kind tag, device; "now <state> · round n of m · time in state"; verb line; four stat tiles (T+ on this device, last heartbeat, revise round with pips, hops with off-chart count).
3. Two columns (wrap on narrow screens):
   - Left, **Run log**: one row per visit, oldest first. Marker in the colour of who did it (dotted when the hop is off-chart, red for a stop state), state, who, duration, `off-chart` chip, reason.
   - Right: **Live run** (`RunTape`), **Journey** (`JourneyClimb`), **Can come next** (every chart edge out of the current state with its condition; the normal way first, human waits amber, stop states red and last), and a collapsed **Full state chart** (`FlowMap` plus legend).
4. **The run log and the journey are one data set and one selection.** Both render from the same list of visits. Picking a row frames and flashes the matching bar; picking a bar highlights the row. Default selection = the current visit.

### 3.4 Activity (`Activity.dc.html`) — replaces `_attention, _handoffs, _verdicts`
1. **Needs attention**: full rows with a static red block, chip, subject, detail, and a link to the ticket or to System.
2. **Relay**: one row per ticket, oldest step first. A step block = bot name in its colour, step, when; solid border for a scripted handoff, dashed for a bot-to-bot one (grok wake). Verdicts sit inline as chips (`routed` accent, `shipped` ok, `other` muted) with their detail. Filter buttons: all, scripted, grok wakes, shipped. A new shipped verdict plays the green settle once on its row.

### 3.5 Efficiency (`Efficiency.dc.html`) — replaces `_savings`
1. **Handoff savings**: a 10 x 10 waffle (scripted `#c0caf5`, grok wakes `#73daca`) replaces the conic ring; the share figure; three counts; the definition and "how each count is made" texts unchanged.
2. **Jobs, lifetime**: done and failed with a two-part bar.
3. **Revise rounds before ship**: one square per shipped ticket, grouped by rounds (0 rounds green).
4. **Who is working now**: one cell per slot (bot colours, held dashed orange, idle dashed grey) and per-device meters.
5. **Subscription headroom** (proposal, chip `proposed`): per bot, 5-hour window, 7-day window, resets, headroom. Ships with `[not tracked yet]` until usage data exists (PR #100's tokens per harness is the first input).

### 3.6 System (`System.dc.html`) — replaces `_components, _no_waiter, _health, _log`
1. **Components**: a matrix, component kind by device, each cell a status block with state and age. Then the legend sentence, then "process details" (today's full table, same row ids so attention links still land).
2. **Version drift**: one row per running version with a block per component; anything not on the newest version is orange; unknown is red.
3. **Poll and Linear budget**: unchanged content, moved here.
4. **poller.log**: lines coloured by kind (start, lock contention orange, non-zero exit red).
5. **Rules and limits** (read-only): the thresholds the chart and health checks enforce, and per-project ship mode and round cap (placeholder until exposed).
6. **Queued jobs with no waiter**.

## 4. Global shell

**TopBar** (`TopBar.dc.html`, 116px with the strip): brand with a 2x3-cell pixel caret (live = green 1 Hz blink, stale = orange 0.4 Hz, lost = solid red); nav (current item: `#24283b` fill and a 2px green underline); link state; snapshot meta. Under it the **header band**: a 24px `field` strip whose brightness is running / total slots. Under that the **attention strip** when there is anything to attend to: static red block, count linking to Activity, then each item inline with its chip and link. Hidden when clear.

**StatusLine** (`StatusLine.dc.html`, 32px, sticky at the bottom in production): a green "factory" block; slots per device and queue depth; then one segment per vital with an 8px status block: poll, poll errors in 1 h (red when the existing rule marks it bad), linear, webhook age, feeder, components up, version with the count of components behind. Segments link to System or Lanes.

## 5. Tokens

Use the exact values in `reference/DESIGN-TOKENS-AND-DATA.md` section 3. They match the CSS variables already in
`lib/dashboard_page.py` (`--bg`, `--panel`, `--panel-hi`, `--field`, `--line`, `--line-hi`, `--text`, `--text-2`,
`--muted`, `--brand`, `--accent`, `--ok`, `--warn`, `--bad`, `--claude`, `--codex`, `--cursor`, `--grok`). Keep the
variables; add none unless a value in an artboard has no variable. Dimmest text allowed is `--muted`.

## 6. PixelStrip engine

One small vanilla-JS module inside the existing SCRIPT. Port `_bayer`, `_noise`, `_prep`, `_val`, `_pal` and `_draw`
from `PixelStrip.dc.html` unchanged.

- **Markup**: the server renders `<canvas class="px" data-px="<effect>" data-tint="<bot or hex>" data-progress=... aria-hidden="true">`. A no-script page shows a still frame made from CSS gradients only (the CSP blocks `data:` images).
- **Grid**: square cells, 4 CSS px by default, integer-snapped `fillRect`s, DPR capped at 2.
- **Contract**: an effect returns an **intensity 0..1 per cell**. The engine quantises it to five tones (background, dim, mid, lit, crest) through an 8x8 Bayer threshold. Palette per tint: dim = 30% and mid = 62% of the way from `#0e0e14` to the tint, lit = the tint, crest = 45% toward white.
- **Effects**: `field`, `fill` (progress), `scan`, `patch`, `spin3`, `decay`, `static`, `twinkle`, `conveyor` (speed), `meter` (used, slots), `caret` (rate), `off`.
- **One-shots** are overlays on any effect, fired by a real event: `beat` (heartbeat ripple from the left edge, 600 ms), `burst` (KPI change, 450 ms), `done` (green sweep, 600 ms, then still).
- **Budget**: one shared `requestAnimationFrame` loop for every strip on the page; 20 fps cap; skip a strip whose state has not changed and that has no live effect; pause when `document.hidden` and when off-screen (IntersectionObserver). Target well under 1 ms per frame for a full Lanes page.
- **Re-render safety**: the SCRIPT rebuilds panels from `status.json` every 1.5 s. Key each strip by job id (or a stable element id) and keep its animation state in a `Map`, so a rebuild does not restart it. Fire `beat` when a job's heartbeat age drops; never on first paint.
- **State to effect** is one lookup table, rendered server-side and used by the script, so the motion rules are testable:
  plan `field` · build `fill` · review `scan` · fix `patch` · claimed `spin3` · stale `decay` (decay amount follows heartbeat age, not a timer) · dead / overdue / stuck `static` red · idle `twinkle` · held `off` with a marching dashed border.

## 7. Components

**SlotTile** (`SlotTile.dc.html`; 180 to 240px wide, about 124px tall, background `--panel-hi`): 16px strip; line 1 bot chip, ticket id, and a state chip only for claimed / stale / dead; line 2 a 2x2 block glyph (one quadrant lit, stepping at 4 Hz) and a verb with a one-letter-per-tick light sweep; line 3 the plain action word, round pips with "n/m", and T+; line 4 a 3px heartbeat bar and "hb Ns". Idle = dashed grey box with a twinkle strip. Held = dashed orange border marching one cell at a time. Dead = red border and static red strip. The whole tile links to the ticket.
Verbs: the approved lists are in the file (plan: Pondering, Sketching, Scheming · build: Assembling, Stacking, Forging · review: Scrutinizing, Combing, Squinting · fix: Patching, Mending, Tinkering), rotating every 4 s. Never on stale or dead tiles.

**JourneyClimb** (`JourneyClimb.dc.html`; 1000 x 336px canvas on a 250 x 84 cell grid, plus caption and controls). Final design:
- Lanes top to bottom: shipped, ship, review, build, plan, ask, then a red floor for stop states. Each lane is 9 cells tall with a dotted rule under it. Lane labels in a 96px gutter.
- Each visit is a **bar** 5 cells tall in its lane with the state name inside (hidden when the bar is too narrow). Past bars: 30% tint fill with a full-tint top edge. Current bar: full tint, a cyan frame, a marching "work strip" on its bottom row, and a blinking cyan marker at its leading edge with three small sparks drifting up.
- Links between bars are one cell wide in the destination's colour; dotted when the chart has no such transition. A fall to a stop state is red.
- **Time axis**: a ruler with the start clock time at the left and "NOW · clock" at the now line. Two modes with a toggle: **even steps** (default; each visit's duration sits under it; the current one counts up) and **to scale** (widths follow duration with a 3-cell minimum; a tick every 15 minutes).
- **Ahead of now**: no state names. A faint dotted line climbs from the ticket to a flag in the shipped lane, the shipped lane glows faintly, and the ruler reads "ahead · N+ hops to shipped" where N is the shortest path to a shipped end state.
- **Motion**: the journey draws itself left to right once (3 s; each bar flashes as it lands); then a beat on every real heartbeat runs along the top edges of the bars toward now; the now line's dots climb; the dotted line drifts toward the flag; the flag waves. No expanding ring. A `calm` setting leaves only the work strip and the marker.
- **Selection**: picking a bar frames it and shows index, state, who, duration and reason in the caption. It accepts a selection from the page and reports picks to the page (section 3.3).

**Live run** (`RunTape.dc.html`): headline "working on" with a pixel spinner and the task in progress in the agent's own words; "last finished"; **Its plan** (the agent's checklist with done / now / pending marks and a fill strip of done / total); **Sub-agents** (one card each: 8px strip scanning while running and settled when finished, kind, description, status; "working alone so far" when none); four tiles: tokens in with cached share, tokens out, cost so far, files touched and commands. No step feed and no per-tool counts.

**FlowMap** (`FlowMap.dc.html`): the 20 states in phase columns with stop states on a bottom rail; only the path taken (coloured by who, numbered per hop, dotted when off-chart), the edges out of the current state (dashed) and optional dim "all edges" are drawn. Wires route through column gutters and row channels and never cross a node.

**Small motion pieces** (`Bits.dc.html`): KPI digit decrypt (three steps of ~160 ms, resolving left to right) with a burst; load meter; queue conveyor; caret; feed settle.

## 8. Porting to the codebase

- `lib/dashboard_page.py` holds the CSS, the server-side render functions (`_bar`, `_kpis`, `_lanes`, `_slot`, `_queue`, `_attention`, `_handoffs`, `_verdicts`, `_tickets`, `_log`, `_health`, `_components`, `_no_waiter`, `_savings`, `_jobs`, `_job_article`, `_jobview`), `_PANELS` (panels per page), `_PAGE_NAMES` (nav labels) and the SCRIPT with its per-page `RENDERERS`. Each panel needs both a server render and a script renderer that produce the same markup.
- `lib/dashboard.py` builds the snapshot; `lib/dashboard_flow.py` owns the chart, `job_flow` and the current SVG.
- Turn artboard inline styles into classes. Turn each `renderVals()` into the server render plus its script twin. Turn component timers into reactions to snapshot changes.
- Keep: escaping through `_e`, redaction, the `<noscript>` refresh, `aria-live` on the link state, the modal on Lanes if it is still wanted (the tile now links to the ticket page; ask Max before removing the modal).

## 9. Data: what is real, what is sample, what is missing

| Where | Field | In the snapshot today? | Notes |
|---|---|---|---|
| Lanes, Tickets, Activity, System, Efficiency (savings, jobs) | everything shown | Yes | Same fields the current panels use |
| Tickets board | phase per state | Derive | From the chart's groups (`g-*` classes today) |
| Efficiency | revise rounds per shipped ticket | Yes | Tickets `round` + state |
| System | version drift | Derive | Group components by `version` |
| Ticket detail | path, current state, time in state, next | Yes | `job_flow` |
| Ticket detail | **who did each visit** | Partly | Jobs carry a bot; states with no job (intake, questions, plan-check, stops) are "script" or unknown. Sample in the mock |
| Ticket detail | **duration and start time of each visit** | **No** | "The database records no transition times." Job-backed visits can use job `created_at`; the rest need recorded transitions. All durations in the mock except the current one are sample values. Until fixed: even-steps mode with no durations, and hide the to-scale toggle |
| Ticket detail | shortest path to shipped | Derive | Breadth-first search over the chart |
| Live run | tokens, cost, files touched, commands | PR #100 | Not merged yet |
| Live run | current task, plan, sub-agents | **No** | Needs the follow-up in `LIVE-RUN-FOLLOWUP.md`. All text in the mock is sample |
| Efficiency | subscription headroom | **No** | Placeholder ships |
| System | per-project ship mode and round cap | **No** | Placeholder ships |

Mock values that are illustrative only: every ticket id and time in `Main.dc.html` / `Playground.dc.html` / `Bits.dc.html`, device names in `Bits.dc.html`, the journey's durations and clock times, all Live run content.

## 10. Accessibility and tests

- Real elements (`a`, `button`, `table`); every canvas has `aria-hidden` or a text summary (`role="img"` + label); selection is on real buttons; colour is never the only signal (pattern, chip text, dashes, position).
- Text contrast 4.5:1; the dimmest text is `--muted`.
- Tests (extend `tests/test_dashboard_*.py`): markup and `data-px` per state; **no broken state maps to an animated effect**; reduced-motion CSS present; no external assets and CSP unchanged; server render equals script render for each panel; attention strip and status line on every page; row ids for System links unchanged; run log and journey render from one list (same count, same order).
