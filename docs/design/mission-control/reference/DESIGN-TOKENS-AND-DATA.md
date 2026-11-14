> **Note for the factory.** This is the working spec the mock-ups were built from. Use section 3 (tokens),
> section 4 (recipes) and section 6 (the real data snapshot the mock-ups show). Sections 0 and 5 are instructions
> for the design-canvas file format and do not apply to production code. Where this file and `../SPEC.md` disagree,
> `../SPEC.md` wins (the ticket page was redesigned after this was written).

# Factory Mission Control — refactor design spec (for page mock-ups)

This is the single source of truth for the page mock-ups on the Design canvas.
Every page artboard is a `.dc.html` file in `/home/claude/factory-pixels/project/`.

## 0. Hard rules for whoever writes a page file

- Write ONLY your one page file. Do not publish, do not touch `canvas.json`, do not edit other files.
- Follow the Design type's `.dc.html` format exactly (skeleton in section 5). `<script src="./support.js"></script>` stays as is.
- Close every non-void element, quote every attribute. `{{hole}}` is a dotted lookup only, never an expression.
- Copy stays literal markup. Repeat rows as literal markup unless there are more than ~8 identical ones, then `<sc-for>` over an array from `renderVals()` (always with `hint-placeholder-count`).
- Logic: `class Component extends DCLogic`, classic JS, no imports, no constructor, no class fields. Read state as `(this.state || {})`.
  In the script never put two braces next to each other (`{ {` and `} }` with a space, or separate lines).
- No verification: do not render, screenshot, open, lint or re-read files to check them. Write carefully once.
- No invented numbers. Use only the data in section 6. A fact we do not have is a visible placeholder like `[not tracked yet]`.
- No emoji, no gradients as decoration, no rounded corners (square everywhere), no left-border accent cards.
- Real elements: `<a href>` for links, `<button type="button">` for buttons, `<table>` for tables. Decorative things get `aria-hidden="true"`.
- Text contrast: body text `#c0caf5` / `#a9b1d6`; the dimmest text allowed is `#8b93b8`. Never use `#414868` or `#565f89` for text.
- Layout: flex or grid with `gap`. Pages are fluid (no px width on the root): use `repeat(auto-fit, minmax(min(Npx, 100%), 1fr))` grids
  and `flex-wrap: wrap` rows with `flex: G 1 Bpx; min-width: 0` children so everything stacks on a phone. Wide tables go in an `overflow-x: auto` box.

## 1. What the product is

Mission control for a fleet of coding agents (claude, codex, cursor, grok) running in fixed slots on three devices (box, pc, mac).
Tickets come in from GitHub and Linear, a feeder turns them into jobs, bots plan / build / review / fix in rounds, and a ticket
ships (merge or handoff) or stops (stuck, blocked, capped, refused, abandoned). The dashboard is read-only and redacted.
The operator (Max) needs, in this order: what needs me, what is every bot doing, where is every ticket, is the tool itself healthy.

## 2. Refactor decisions (the "why")

1. **Colour = who.** Bot hues are used only for bots: claude amber, codex blue, cursor purple, grok teal.
   Phases no longer borrow bot hues (today "Ask" is codex blue, "Build" is claude amber). A phase is a column position plus a pattern.
2. **Pattern = what.** Each kind of work has one pixel pattern everywhere (slot strips, ticket cards, the state map):
   plan = drifting dither field, build = bricks filling, review = scanner, fix = patch pass, claimed = 3x3 spinner,
   waiting on a human = still amber, done = settled green, trouble = static red dither.
3. **Status = green / orange / red** (`ok`, `warn`, `bad`) and nothing else. Cyan `accent` means "the path taken / you are here / selected".
4. **Motion encodes truth.** Only active work moves. Broken never moves. Done settles once. Waiting on a human is still.
5. **Attention is global.** The "needs attention" items sit in a strip under the top bar on every page, not behind a badge on another page.
6. **Tool vitals are global.** A tmux-style status line at the bottom of every page carries poll, Linear, webhook, feeder, components, version.
7. **Lanes are real lanes.** Each device is one horizontal row of slot tiles, so the whole floor fits above the fold.
8. **Tickets are the unit, jobs are steps.** The Jobs list and the tickets table merge into one Tickets board with a column per phase.
9. **Feeds are grouped by ticket.** Handoffs and verdicts merge into relay threads: who did what, in order, and how it ended.
10. **Health becomes System:** component matrix (kind x device), version drift, poll and Linear budget, poller.log, rules and limits.

Navigation: **Lanes · Tickets · Activity · Efficiency · System** (was Lanes · Jobs · Activity · Efficiency · Health).
Artboard files: `Lanes.dc.html`, `Tickets.dc.html`, `Job.dc.html`, `Activity.dc.html`, `Efficiency.dc.html`, `System.dc.html`.

## 3. Tokens (exact, from the live CSS)

| token | value | use |
|---|---|---|
| bg | `#1a1b26` | page background |
| dim | `#13141c` | status line, recessed areas |
| panel | `#1f2230` | panels |
| panel-hi | `#24283b` | slot tiles, hover rows, selected nav |
| field | `#0e0e14` | pixel strips, log / code wells |
| sel | `#292e42` | selected / current fills |
| line | `#24283b` | hairlines between rows |
| line-hi | `#414868` | dashed borders, empty meters (never text) |
| text | `#c0caf5` | primary text |
| text-2 | `#a9b1d6` | secondary text |
| muted | `#8b93b8` | labels, captions (dimmest text allowed) |
| brand / ok | `#9ece6a` | brand, healthy |
| accent | `#7dcfff` | path taken, now, selected |
| warn | `#ff9e64` | stale, held, waiting on a human |
| bad | `#f7768e` | stuck, dead, down, overdue |
| claude | `#e0af68` | bot |
| codex | `#7aa2f7` | bot |
| cursor | `#bb9af7` | bot |
| grok | `#73daca` | bot |
| ink | `#0c0e10` | text on a filled colour |
| hdr-text | `#ced8ff`, hdr-text-2 `#b7bfea`, hdr-brand `#acdc7e` | top bar only |

Elevation is a hairline, never a shadow: `box-shadow: 0 0 0 1px rgba(255,255,255,0.07)`.
Type: sans `'Geist', ui-sans-serif, system-ui, sans-serif` 14px/1.5 for prose and nav;
mono `'JetBrains Mono', ui-monospace, Menlo, monospace` for everything that is data, labels, chips, headings of panels.
Numbers use `font-variant-numeric: tabular-nums`.

Chip tints (rgb triplets for `rgba()`): ok 158,206,106 · accent 125,207,255 · warn 255,158,100 · bad 247,118,142 ·
claude 224,175,104 · codex 122,162,247 · cursor 187,154,247 · grok 115,218,202 · muted 139,147,184.

## 4. Recipes (copy these)

**Panel**
```html
<section style="min-width: 0; padding: 14px 16px 16px; background: #1f2230; box-shadow: 0 0 0 1px rgba(255,255,255,0.07)">
<h2 style="display: flex; justify-content: space-between; align-items: baseline; gap: 8px; margin: 0 0 12px; font-family: 'JetBrains Mono', ui-monospace, Menlo, monospace; font-size: 13px; line-height: 1.3; font-weight: 600; letter-spacing: -0.01em; color: #c0caf5"><span style="display: flex; align-items: center; gap: 9px"><span aria-hidden="true" style="width: 7px; height: 7px; background: #9ece6a"></span>Panel title</span><span style="font-weight: 400; font-size: 12px; color: #8b93b8">count or note</span></h2>
...
</section>
```

**Chip** (status or bot; text colour = the token, border 40%, fill 9%; lowercase; bots and states)
```html
<span style="display: inline-block; padding: 0 6px; border: 1px solid rgba(224,175,104,0.4); background: rgba(224,175,104,0.09); color: #e0af68; font-family: 'JetBrains Mono', ui-monospace, Menlo, monospace; font-size: 11px; line-height: 1.6; font-weight: 500; letter-spacing: 0.02em; white-space: nowrap">claude</span>
```
Rule: a healthy running job shows NO "running" chip (the motion says it). Chips appear for anything that is not normal.

**Section label** (small caps label above a group)
`font-family: mono; font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: #8b93b8`

**Row in a feed / list**: `display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 8px; padding: 7px 6px; border-bottom: 1px solid #24283b`.
Ticket ids are mono 13px weight 600 `#c0caf5`; "what" text is 12px `#a9b1d6`; "when" is mono 12px `#8b93b8` pushed right with `margin-left: auto`.

**Stat tile**: `padding: 10px 14px 12px; background: #1a1b26; box-shadow: hairline` with a number (mono 600 30px/1.15, letter-spacing -0.03em)
and a label (mono 500 12px `#8b93b8`). Number colour: text by default, ok / warn / bad when it means that.

**Well** (logs, code): `padding: 10px 12px; background: #0e0e14; box-shadow: hairline; font: mono 12px/1.5; color: #a9b1d6; white-space: pre-wrap; overflow-wrap: anywhere`.

**Table**: `width: 100%; border-collapse: collapse`; `th`: mono 500 12px `#8b93b8`, left aligned; `th, td`: `padding: 6px 12px 6px 0; border-bottom: 1px solid #24283b; vertical-align: top`; numeric cells right aligned mono tabular.

**Pixel block** (static status square, no canvas): `<span aria-hidden="true" style="width: 8px; height: 8px; background: #9ece6a"></span>`.

**Round pips** (round 2 of 3): three 6x6 px squares, gap 2px; done rounds `#c0caf5`, current `#7dcfff`, remaining `#414868`.

## 5. Page skeleton and shared components

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Lanes</title>
<script src="./support.js"></script>
</head>
<body>
<x-dc>
<helmet>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600&amp;family=JetBrains+Mono:wght@400;500;600;700&amp;display=swap">
<style>
body{margin:0}
a{color:#c0caf5;text-decoration:none}a:hover{color:#9ece6a}
</style>
</helmet>
<div style="min-height: 100vh; box-sizing: border-box; display: flex; flex-direction: column; background: #1a1b26; color: #c0caf5; font-family: 'Geist', ui-sans-serif, system-ui, sans-serif; font-size: 14px; line-height: 1.5">
<dc-import name="TopBar" current="lanes" hint-size="100%,116px"></dc-import>
<main style="flex: 1 0 auto; box-sizing: border-box; width: 100%; max-width: 1600px; margin: 0 auto; padding: 20px 24px 32px; display: flex; flex-direction: column; gap: 16px">
<!-- panels -->
</main>
<dc-import name="StatusLine" hint-size="100%,32px"></dc-import>
</div>
</x-dc>
<script type="text/x-dc" data-dc-script data-props='{"$preview":{"width":1440,"height":1100}}'>
class Component extends DCLogic {
  renderVals() {
    return {};
  }
}
</script>
</body>
</html>
```
`$preview.height` = your best estimate of the page height at 1440 wide (round up; too tall is fine, clipped is not). Report it.

### Shared components (already written by the lead; just import them)

**TopBar** — `<dc-import name="TopBar" current="lanes" hint-size="100%,116px"></dc-import>`
`current`: `lanes | tickets | activity | efficiency | system`. Renders the brand with a pixel caret, the nav, link state, snapshot meta,
the header band (dither field, brightness = running / total slots) and the attention strip. 116px tall. Do not rebuild any of it.

**StatusLine** — `<dc-import name="StatusLine" hint-size="100%,32px"></dc-import>` — the bottom vitals line. No props.

**PixelStrip** — a canvas of square cells; always wrap it in a sized block element.
`<dc-import name="PixelStrip" effect="fill" tint="#e0af68" progress="0.6" cell="4" strip-height="16" seed="7" hint-size="100%,16px"></dc-import>`
Props: `effect` = `field` (plan) | `fill` (build; uses `progress` 0..1) | `scan` (review) | `patch` (fix) | `spin3` (claimed) | `decay` (stale) |
`static` (trouble; never moves) | `twinkle` (idle) | `conveyor` (queue; `speed` 1..6) | `meter` (`used`, `slots`; lit cells shimmer) |
`caret` (`rate` Hz, 0 = solid) | `off`. `tint` = hex. `cell` = px per cell (2..8). `strip-height` = px. `seed` = any number (vary it per strip).
`still="true"` freezes it. `done="true"` plays the green settle and stays still. `load` 0..1 scales `field` brightness.
The strip fills its parent's width. A 16x16 "phase glyph" is a PixelStrip in a `width: 16px` block with `cell="2" strip-height="16"`.
Phase glyph effects: Ask = `twinkle` tint `#a9b1d6`; Plan = `field`; Build = `fill` progress 0.6; Review = `scan`; Ship = `off` with `done="true"`; Stop = `static` tint `#f7768e`. Glyph tint is `#a9b1d6` unless noted.

**SlotTile** — one slot in a lane. Fills its parent's width (designed for 180 to 240px), about 124px tall.
`<dc-import name="SlotTile" mode="fix" bot="claude" ticket="Cleffy#107" round="2/3" elapsed="4m 06s" hb="5s" seed="2" hint-size="100%,124px"></dc-import>`
Props: `mode` = `plan | build | review | fix | claimed | stale | dead | idle | held`; `bot` = `claude | codex | cursor | grok`;
`ticket`, `round`, `elapsed`, `hb` are display strings; `progress` 0..1 (build fill); `seed` number. Job tiles link to `Job.dc.html`.

Links between pages: `<a href="Job.dc.html">`, `<a href="Tickets.dc.html">` and so on (they work in Play).

## 6. Data snapshot (real, from the live dashboard; use exactly this)

**Bar**: 2 need attention · LIVE · snapshot 2s ago · 20:48:45 UTC · read-only, redacted.

**KPIs**: running 9 · queued 0 · done 174 · failed 74 · stuck / overdue 1.

**Intake**: Linear webhook last received 6m 11s ago (Attachment.update) · dirty queue 0.
Feeder: eligible 11 · fed 11 · waiting for capacity 0. Fed (all "fed, job queued"):
factory#106 → PR #112, factory#107 → PR #111, factory#108 → PR #113, factory#109 → PR #114, factory#115 → PR #116,
cleffy#104 → PR #107, factory#119 → PR #123, factory#120 → PR #124, factory#121 → PR #125, factory#122 → PR #126, factory#128 → PR #129.

**Lanes** (13 slots, 10 used, 9 running). All jobs are kind PR and state running.
- **box** · ONLINE · 3 / 3 slots · harness claude · codex · cursor
  1. claude · factory#124 · build · round 1/1 · T+ 4m 06s · hb 6s
  2. claude · Cleffy#107 · fix · round 2/3 · T+ 4m 06s · hb 5s
  3. held
- **pc** · ONLINE · 5 / 6 slots · harness claude · codex · cursor
  1. claude · factory#129 · plan · round — · T+ 1m 13s · hb 13s
  2. codex · factory#114 · review · round 1/3 · T+ 7m 40s · hb 10s
  3. codex · factory#126 · review · round 1/3 · T+ 7m 37s · hb 7s
  4. claude · factory#113 · fix · round 2/3 · T+ 4m 07s · hb 6s
  5. codex · factory#123 · review · round 1/1 · T+ 4m 06s · hb 6s
  6. idle
- **mac** · ONLINE · 2 / 4 slots · harness claude · codex
  1. claude · factory#112 · fix · round 3/3 · T+ 4m 07s · hb 6s
  2. claude · factory#125 · build · round 1/1 · T+ 4m 06s · hb 6s
  3. idle
  4. idle
- **Queue**: 0 waiting ("queue empty").
- Running now by bot: claude 6, codex 3, cursor 0, grok 0.

**Needs attention (2)**
1. `stuck` · cleffy#103 · planning: cancelled: superseded by sanctioned intake (Factory 2026-11-13)
2. `down` · waiter cursor-bot @ box · down (never seen)

**Tickets (34 tracked)** — repo#id, tracker, state, live job if any. Projects: factory 26, cleffy 6, aut0 2.
- Ask (intake, questions, waiting-max): none.
- Plan (planning, plan-check): factory#129 planning (live: claude @ pc, plan, round —, T+ 1m 13s) · cleffy#103 planning, STUCK.
- Build (building, fixing): aut0#AUT-2284 building (linear, no job running) · aut0#AUT-2286 building (linear, no job running) ·
  factory#124 building (live: claude @ box, build, 1/1) · factory#125 building (live: claude @ mac, build, 1/1) ·
  Cleffy#107 fixing (live: claude @ box, fix, 2/3) · factory#113 fixing (live: claude @ pc, fix, 2/3) · factory#112 fixing (live: claude @ mac, fix, 3/3).
- Review (review, capped): factory#114 review (live: codex @ pc, 1/3) · factory#126 review (live: codex @ pc, 1/3) ·
  factory#123 review (live: codex @ pc, 1/1) · factory#100 review (round 4, no job running; last verdict: routed · queued-review).
- Shipped — handed-off (9): cleffy#98, cleffy#99, factory#110, factory#111, factory#116, factory#65, factory#89, factory#95, factory#97.
- Shipped — merged (12): cleffy#96, cleffy#97, factory#104, factory#72, factory#74, factory#77, factory#81, factory#82, factory#87, factory#88, factory#98, factory#99.
- Stopped (refused, abandoned, blocked, capped): none. (cleffy#103 is flagged stuck while in planning.)
- Revise rounds before ship, per shipped ticket (21): merged = cleffy#96 0, cleffy#97 0, factory#104 1, #72 0, #74 2, #77 1, #81 0, #82 2, #87 2, #88 2, #98 3, #99 3;
  handed-off = cleffy#98 1, cleffy#99 0, factory#110 1, #111 1, #116 1, #65 0, #89 0, #95 1, #97 1.
  Histogram: 0 rounds = 7, 1 round = 8, 2 rounds = 4, 3 rounds = 2.

**State machine (20 states)** — phase: states.
Ask: intake (gloss "ticket"), questions, waiting-max · Plan: planning, plan-check · Build: building, fixing (gloss "revise") ·
Review: review, capped · Ship: ship, awaiting-ok ("waiting on a human OK"), waiting-external, merged, post-merge, handed-off ("ready for review", end), done ("shipped", end) ·
Stop: refused (end), abandoned (end), blocked (end), stuck (end).

**Job detail sample — Cleffy#107** (claude, PR, on box): now fixing · round 2/3 · 4m 08s in fixing · T+ 4m 06s · hb 5s.
Path: intake → questions → planning → plan-check → building → planning → review → fixing → stuck → fixing.
Hops: intake → questions: brief and acceptance criteria posted · questions → planning: kind=go, queue plan ·
planning → plan-check: trusted builder posts kind=plan · plan-check → building: every acceptance criterion mapped ·
building → planning: (no direct transition on the chart) · planning → review: (no direct transition on the chart) ·
review → fixing: REVISE, under the round cap · fixing → stuck: device offline 60 minutes or job dies twice (T6) or CI red three times ·
stuck → fixing: (no direct transition on the chart; verdict: authorized-unstick-fix).
Can come next from fixing: review (ready, CI green) · waiting-max (a product question) · stuck (device offline 60 minutes or job dies twice (T6) or CI red three times).

**Handoffs (12, newest first)** — kind · bot · ticket · step · when
scripted codex factory#114 review 4s ago · scripted claude factory#112 fix 42s ago · scripted claude factory#129 plan 1m 40s ago ·
bot-to-bot grok factory#124 done 2m 47s ago · scripted codex factory#124 review 3m 22s ago · scripted claude factory#124 build 6m 22s ago ·
bot-to-bot grok factory#116 done 6m 48s ago · scripted codex factory#116 review 7m 45s ago · scripted claude factory#100 fix 8m 02s ago ·
scripted claude factory#114 build 15m 04s ago · scripted claude factory#126 build 19m 41s ago · bot-to-bot grok factory#125 stuck 24m 04s ago.

**Verdicts (12, newest first, no timestamps)** — kind · ticket · detail
routed factory#129 queued-plan-build · routed factory#112 queued-review · shipped factory#124 handed-off · routed factory#100 queued-review ·
routed factory#124 queued-review · shipped factory#116 handed-off · routed factory#129 queued-plan · other cleffy#107 authorized-unstick-fix ·
other factory#125 authorized-unstick-build · other factory#124 authorized-unstick-build · other factory#123 authorized-unstick-review · other factory#116 authorized-unstick-review.

**Relay threads** (handoffs + verdicts merged per ticket, oldest step first; "⇒" is a verdict)
- factory#114: claude build (15m 04s ago) → codex review (4s ago)
- factory#112: claude fix (42s ago) ⇒ routed · queued-review
- factory#129: ⇒ routed · queued-plan → claude plan (1m 40s ago) ⇒ routed · queued-plan-build
- factory#124: ⇒ other · authorized-unstick-build → claude build (6m 22s ago) ⇒ routed · queued-review → codex review (3m 22s ago) → grok done (2m 47s ago, bot-to-bot) ⇒ shipped · handed-off
- factory#116: ⇒ other · authorized-unstick-review → codex review (7m 45s ago) → grok done (6m 48s ago, bot-to-bot) ⇒ shipped · handed-off
- factory#100: claude fix (8m 02s ago) ⇒ routed · queued-review
- factory#126: claude build (19m 41s ago)
- factory#125: grok stuck (24m 04s ago, bot-to-bot) ⇒ other · authorized-unstick-build
- cleffy#107: ⇒ other · authorized-unstick-fix
- factory#123: ⇒ other · authorized-unstick-review

**Handoff savings**: scripted share 65% · 115 scripted handoffs · 8 scripted in flight · 62 bot-to-bot (grok wakes).
Definition: "Scripted share is the part of handoffs a script routed straight to the next bot, scripted / (scripted + grok wakes), rather than one bot waking another."
How each count is made: "scripted = jobs for claude, codex or cursor that `orch wait` claimed and the bot finished with an orch:v1 handoff comment. scripted_in_flight = such jobs claimed and still running. bot_to_bot = jobs routed to grok: each one wakes Grok Bot to read the ticket and hand off by hand (stops, clarify, refusals, ship failures). The database does not record bot messages sent outside the factory, so grok wakes are the measurable stand-in for bot-to-bot handoffs."
Jobs lifetime: 174 done · 74 failed.

**Poll and Linear budget**: poll OK · last cycle 50s ago · linear OK · 1,250 req/h · 1,500,000 complexity/h ·
45 poll errors in 1 h (shown as bad) · 273 poll errors in 24 h · 0 Linear deferrals in 1 h · 0 Linear limit hits in 1 h · last poller activity 39s ago.
Recent poll errors: poll TypeError 50s ago · poll TypeError 2m 15s ago · poll TypeError 3m 19s ago.

**poller.log** (written 4s before the snapshot; last 50 lines, redacted). Repeating excerpt:
```
2026-11-13 13:08:59 EST poller exit rc=1
2026-11-13 13:09:19 EST poller start
orch poll: another orch poll owns /home/box/.local/state/factory/orch.db.lock
2026-11-13 13:09:19 EST poller exit rc=1
2026-11-13 13:09:59 EST poller start
orch poll: another orch poll owns /home/box/.local/state/factory/orch.db.lock
2026-11-13 13:10:00 EST poller exit rc=1
2026-11-13 13:11:20 EST poller start
orch poll: another orch poll owns /home/box/.local/state/factory/orch.db.lock
2026-11-13 13:11:20 EST poller exit rc=1
2026-11-13 13:14:00 EST poller start
orch poll: another orch poll owns /home/box/.local/state/factory/orch.db.lock
2026-11-13 13:14:00 EST poller exit rc=1
```

**Components (13 · 1 down)** — component · bot · device · state · ago · pid · version · started (ET)
poller · — · box · UP · 1s · 3821465 · 1a6d0c7e74da · Nov 13 1:42:01 PM
waiter · claude-bot · box · UP · 1s · 3821833 · 1a6d0c7e74da · Nov 13 1:42:05 PM
waiter · claude-bot · mac · UP · 1s · 3821833 · 1a6d0c7e74da · Nov 13 1:42:05 PM
waiter · claude-bot · pc · UP · 1s · 3821833 · 1a6d0c7e74da · Nov 13 1:42:05 PM
waiter · codex-bot · box · UP · 1s · 3821906 · 1a6d0c7e74da · Nov 13 1:42:06 PM
waiter · codex-bot · mac · UP · 1s · 3821906 · 1a6d0c7e74da · Nov 13 1:42:06 PM
waiter · codex-bot · pc · UP · 1s · 3821906 · 1a6d0c7e74da · Nov 13 1:42:06 PM
waiter · cursor-bot · box · DOWN never seen · — · — · unknown · —
linear-listener · — · box · UP · 1s · 2773622 · 16d70391ddd4 · Nov 13 8:55:45 AM
tunnel · — · box · UP · 1s · 2773622 · 16d70391ddd4 · Nov 13 8:55:45 AM
dashboard · — · box · UP · 1s · 124815 · 1a6d0c7e74da · Nov 13 3:50:27 PM
device-probe · — · mac · UP · 1s · 3821465 · 1a6d0c7e74da · Nov 13 1:42:01 PM
device-probe · — · pc · UP · 1s · 3821465 · 1a6d0c7e74da · Nov 13 1:42:01 PM
Legend: "up: a beat within 3 intervals (15 s each; device probes every 60 s) · stale: within 8 · down: older, stopped or never seen · unknown: this snapshot is too old to tell".
Version drift: 10 components on 1a6d0c7e74da; 2 (linear-listener, tunnel, started 8:55:45 AM) on 16d70391ddd4; 1 unknown (cursor waiter).
Queued jobs with no waiter: none ("every queued job has a live waiter").

**Rules visible in the chart** (for the System page "Rules and limits", read-only):
heartbeat every 15 s (device probes every 60 s) · up within 3 intervals, stale within 8, down older ·
stuck when: device offline 60 minutes, or a job dies twice (T6), or CI red three times ·
plan-check sends work back when an acceptance criterion is unmapped; gaps a second time → stuck ·
REVISE under the round cap → fixing; REVISE at the cap → capped (Max can okay one more round) ·
ship modes: merge, handoff, or after an approver's OK · 3rd decline from an external approver → stuck ·
Linear budget 1,250 req/h and 1,500,000 complexity/h.
Projects: maxharris1/factory (github), maxharris1/cleffy (github), aut0-code/aut0 (linear). Per-project ship mode and round cap: `[not shown by the dashboard today]`.
