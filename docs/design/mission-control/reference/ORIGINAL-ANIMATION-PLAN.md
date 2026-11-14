# Factory dashboard: pixel "thinking" animations — FINAL plan (not filed; for design workshop)

Final 2026-11-13 ~3:25 PM ET with Max's choices folded in. Code read at `/home/box/factory` @ `1a6d0c7`. Nothing has been submitted to the factory.

## 1. How Omarchy's landing animation works

The hero is one full-bleed `<canvas>` (2D context, opaque, DPR capped at 2). The screen is cut into square cells sized so
the OMARCHY wordmark is exactly 81×19 cells, and every frame each cell is either filled with a solid `fillRect` or left as
background: no anti-aliasing, no gradients, so it reads as chunky pixels. Whether a cell lights is decided by a slow
drifting value-noise field + a per-cell sine twinkle, compared against an **8×8 Bayer ordered-dither** threshold — that
dither is what gives the "cubic bit" texture. A soft "cursor" wanders on Lissajous curves (and follows the mouse) and
brightens nearby cells; it periodically drops a ripple shaped like a 15×15 pixel icon that expands and fades. The intro
"etch" of the wordmark is a separate WASM terminal-text-effects engine (decrypt, rain, wipe, etc.) drawing block glyphs
(█ ▀ ▄ ▌ ░ ▒ ▓) into the same grid.

Details worth copying:
- **Palette: 5 green steps on near-black.** bg `#0e0e14`, dim `#39482e`, mid `#678549`, lit `#9ece6a`, hover `#bbdd97`,
  crest `#daecc6`. These are read from CSS variables, so the theme can swap them. The lit value is the Tokyo Night green
  we already use as `--brand`.
- **Timing:** it runs on rAF but skips frames under 25 ms, so about 40 fps max. Motion is slow: the noise drifts about
  1 cell/s and the twinkle repeats every ~6 s. A ripple lives 0.65–1.2 s, with one every 2–3 s.
- **Being polite:** an IntersectionObserver stops the loop when the canvas is off-screen. With prefers-reduced-motion it
  draws one still frame. Mouse tracking only runs on fine-pointer devices.

The good news is that the dashboard is already Omarchy-styled. `lib/dashboard_page.py` uses the Tokyo Night palette,
square corners, and the vendored Geist and JetBrains Mono fonts. It has a blinking block caret, a pulse dot, a shimmer on
the heartbeat bar, and a "breathe" halo on the flow chart. What it lacks is the pixel grid and any per-state motion.

## 2. How terminal agents show "working"

| Tool | What it looks like | Notes |
|---|---|---|
| Claude Code | A star glyph cycles `· ✢ ✳ ✶ ✻ ✽` and back, next to a whimsical gerund ("Pondering…", "Combobulating…"). The verb has a shimmer sweep, followed by elapsed time and tokens. | The glyph shows it's alive, the verb shows its personality, and the counters show progress. |
| Codex CLI | "Working" with a light band sweeping across the letters, then `(12s • esc to interrupt)`. | The shimmer *is* the spinner. Very calm. |
| Gemini CLI | A braille spinner `⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏` plus rotating joke phrases. | Classic cli-spinners "dots" at about 80 ms per frame. |
| opencode | A "Knight Rider" scanner: a row of blocks/diamonds with a lit head and a fading trail sweeping back and forth (braille for small spinners). | Users complained that it loops for 20 minutes with no sign of progress. **Lesson: always pair motion with a real progress signal.** |
| Aider / others | A sweeping `░▒▓█` bar or a block bounce. | Same scanner family. |

These all share three ideas. A tiny glyph loop says "alive". A sweep or shimmer says "working". Real numbers (elapsed
time, tokens, heartbeat) say "progressing".

## 3. What the factory UI has today

Everything lives in `lib/dashboard_page.py`: the inline CSS, the server-side Python render, and the inline SCRIPT, whose
`renderLanes`, `renderQueue` and `renderKpis` rebuild panels from `status.json` every 1.5 s. Data comes from
`lib/dashboard.py` and the statechart from `lib/dashboard_flow.py`. The CSP is `default-src 'none'` with inline script
and style allowed, so canvas and CSS work but external or `data:` images don't. That's fine for us.

Components:
- **Bar:** brand plus caret, nav, the needs-attention badge, and the link dot (live/stale/lost/boot).
- **Factory status:** KPI tiles (running, queued, done, failed, stuck/overdue), the Linear webhook block, the feeder list.
- **Lanes and slots:** a device card per machine (ONLINE, DISABLED, PLACEHOLDER, UNLISTED; `used / max slots`), each with
  slot cards. A running job shows its bot chip, PR/ticket, job_state chip (claimed / running / stale heartbeat / runner
  dead), OVERDUE, action, round, T+ elapsed, heartbeat age and a heartbeat bar (ok/warn/bad). Other slots are `held`
  (dashed orange) or `idle`/`off` (dashed grey). There's also a "grok wakes" lane.
- **Queue rail** (waiting age, "no waiter" chip), **Needs attention**, **Handoffs**, **Verdicts**, **Tickets table**,
  **Log**, **Health/Components** (up/stale/down), **Savings** ring and meter.
- **Jobs page:** a path-taken SVG trail plus the full statechart, with the current node highlighted and breathing.

States we can key animation on:
- Job actions: plan, build, review, fix, clarify.
- job_state: claimed, running, stale, dead.
- Ticket states (chart): intake, questions, planning, plan-check, building, review, fixing, ship, awaiting-ok,
  handed-off, merged, post-merge, done, waiting-max, waiting-external, stuck, blocked, capped, refused, abandoned.
- Others: overdue, paused, feeder (eligible, waiting-capacity, feeding, fed, error), components (up/stale/down), devices.

## 4. Where to add animation

| # | Component | State | Proposed pixel animation | Why |
|---|---|---|---|---|
| 1 | **Slot card** (top strip, ~6 cells tall, full width) | running · **plan** | A slow dithered noise field in **purple** (Cursor/plan legend), with occasional "idea" ripples. | Planning is thinking, so give it the Omarchy hero field in miniature. |
| 2 | Slot card | running · **build** | Bottom-up "fill" columns, like stacking bricks, tinted with the bot colour (Claude amber, Codex blue). The fill height tracks T+ against the timeout. | Building is assembling. The height carries real progress, which fixes opencode's complaint. |
| 3 | Slot card | running · **review** | A Knight-Rider scanner: a lit 2-cell head with a dithered 6-cell trail sweeping across a row, in yellow. | Review reads like scanning lines. |
| 4 | Slot card | running · **fix** | The scanner, but the head "patches" a cell as it passes (cells flip dim → lit), in orange. | Revising means touching specific spots. |
| 5 | Slot card | **claimed, not started** | A 3×3 block spinner cycling around the ring (the pixel version of braille dots), in warn orange. Nothing else moves. | Alive but not working yet. |
| 6 | Slot card | **stale heartbeat** | The strip freezes, and every ~2 s some cells "decay" to dim. | It's slowing down, so you see it before it dies. |
| 7 | Slot card | **dead / OVERDUE / stuck** | **Static** red checkerboard dither with no motion, plus the existing red ring. | Our motion rule: a broken thing never looks busy. |
| 8 | Slot card, **heartbeat pulse** | each new heartbeat | A one-shot ripple of lit cells from the left edge (~600 ms), like Omarchy's ripple. | A real event, not a fake loop, so you can *see* the job beat. |
| 9 | Slot card **action label** | any running job | A Codex-style shimmer sweep on the action word, with a Claude-style 6-frame block glyph `▖▘▝▗` in front of it. | Cheap, CSS-only, and the clearest "terminal agent" nod. |
| 10 | **Idle slot** | idle | Very faint dither twinkle: 1–2 cells lit at a time, about 0.3 Hz. | Shows it's free capacity without shouting. Off-state stays fully static. |
| 11 | **Held slot** | held | A dashed border "marching" in 1-cell steps (`steps()`). | Reserved, about to be used. |
| 12 | **Device card header** | online, with N jobs | A mini 8-cell "load meter" of N/max lit cells, where lit cells shimmer. It goes static grey when disabled and turns red with no shimmer when the device probe is down. | Shows device health at a glance. |
| 13 | **KPI tiles** | value changes | A pixel "decrypt" roll of the digits when a number changes (~400 ms), plus a one-shot cell burst in the tile colour. | Mirrors Omarchy's etch decrypt effect, and only fires on real change. |
| 14 | **Queue rail** item | queued | A 1-row "conveyor" of dots shifting left once per second, getting faster as the wait gets longer. | Waiting looks different from working. |
| 15 | Queue item | **no waiter** | Static red, no motion. | Rule 1. |
| 16 | **Jobs-page trail** | current node | Replace the SVG `breathe` halo with a dither halo of pixel cells. Hops already taken "etch" in once when the node changes. | Keeps the chart consistent with the new style. |
| 17 | **Brand caret** in the bar | live, stale, lost | The caret becomes a 2×3-cell block. Live: steady blink. Stale: a slower blink, in orange. Lost: solid red with no blink. | Global liveness in one glyph. |
| 18 | **Header band** (optional) | whole factory | A thin full-width Omarchy field (~24 px tall) whose brightness tracks `running / total slots`. | The "signature" piece. Optional because it's the most CPU. |
| 19 | **Handoff/verdict feed** | new APPROVE, merge, handed-off | A one-shot green "settle" that sweeps across the row, then stops. | A finished thing should feel finished. |

Top picks for a first PR are rows 1–9, the slot card, since that's where Max looks. Rows 13 and 17 are cheap extras.

## 5. One shared primitive: `PixelStrip`

One small vanilla-JS module inside the existing SCRIPT, with no framework and no dependency. The CSP already allows it.

- **Markup:** the server renders `<canvas class="px" data-px="build" data-tint="claude" data-progress="0.42" aria-hidden="true">`
  with a CSS fallback background, so the no-script page still looks right as a dithered still frame.
- **One shared rAF loop** drives every `.px` canvas on the page. There is never one loop per card.
- **Grid:** cells are 4 CSS px (configurable) and drawn as integer-snapped `fillRect`s at DPR ≤ 2, with
  `imageSmoothingEnabled=false`.
- **Effects** are small pure functions `(t, col, row, params) -> level 0..4`. A level maps to a 5-step palette
  (bg, dim, mid, lit, crest) generated per tint by mixing the tint into `--field`. That's the same 5-tone idea as Omarchy,
  but with our bot and state colours. A shared 8×8 Bayer matrix plus a seeded value-noise table (one per page) give the
  dither texture.
- **Effects list:** `field` (plan), `fill` (build), `scan` (review/fix), `spin3` (claimed), `decay` (stale),
  `static` (dead/stuck), `twinkle` (idle), `ripple` (one-shot), `settle` (one-shot).
- **Budget:**
  - Cap at 20 fps. These are status lights, not a hero, and Omarchy itself caps at about 40.
  - Skip any canvas whose state hasn't changed and that has no live effect.
  - Pause when `document.hidden`, reusing the existing visibilitychange hook, and when off-screen (IntersectionObserver).
  - Target well under 1 ms per frame for ~12 strips.
- **Reduced motion:** if `prefers-reduced-motion` is set, draw one representative frame per state and still show
  one-shot events as an outline flash. That fits the existing reduced-motion block, which already kills CSS animation.
- **Re-render safety:** `renderLanes` rebuilds DOM every 1.5 s. Strips are keyed by job id and their state is stored in a
  `Map`, so a rebuild doesn't restart the animation. Heartbeat ripples fire when `heartbeat_age` drops.
- **CSS-only helpers** for the tiny bits (rows 9, 11, 17) use `steps()` keyframes and need no canvas.

## 6. Motion rules (to write into the module docstring and tests)

1. **Only active work moves.** That means running, claimed, queued and live. Idle gets at most a faint twinkle.
2. **Broken never moves.** Stuck, blocked, dead, overdue, capped, refused, no waiter and down are static red or orange
   dither. Motion would imply "it's fine".
3. **Done settles.** It plays one ~600 ms green sweep and then stays still forever.
4. **Waiting on a human is still and amber,** with no loop. That covers waiting-max, awaiting-ok, questions and paused.
5. **Motion encodes truth.** Fill height is elapsed/timeout, a ripple means a real heartbeat, and conveyor speed is the
   real wait age. There are no decorative loops on data panels.
6. **Calm speed.** No effect cycles faster than ~6 Hz, and there's no full-panel flashing.
7. Colour = **who** (bot tint), pattern = **what** (plan/build/review/fix), red/static = **trouble**.

## 7. Ticket split

**PR A: pixel primitive and slot cards** (the main one)
- `lib/dashboard_page.py`: CSS (`.px`, fallback dithers, reduced-motion), the server `_slot()` emits the canvas plus data
  attributes, and in the SCRIPT the `PixelStrip` loop, its effects, and `renderLanes` keeping the strips.
- `lib/dashboard.py`: only if it needs an explicit per-slot `phase` (plan/build/review/fix) beyond `action`.
- `tests/test_dashboard_omarchy.py` and `tests/test_dashboard_live.py`: markup and data attributes per state; dead,
  stuck and overdue get `data-px="static"`; reduced-motion CSS is present; still no external assets.
- Covers placements 1–11.

**PR B: bar, KPIs, devices and queue**
- `lib/dashboard_page.py`: pixel caret (17), KPI decrypt and burst on change (13), device load meter (12), queue conveyor
  and no-waiter static (14–15), feed settle (19).
- Tests in `test_dashboard_mission_control.py` and `test_dashboard_layout.py`.

**PR C (optional): signature header band and flow-chart halo**
- Placements 16 and 18 in `lib/dashboard_page.py` plus `lib/dashboard_flow.py` for the halo class.
  `test_dashboard_job_flow.py`.

All three touch the same big file, so they should run **one after another, not in parallel**: A, then B, then C.
Otherwise they'll conflict.

## 8. Decisions (Max, 2026-11-13 ~3:24 PM ET)

1. **Colour by bot.** Tint = who (Claude amber, Codex blue, plan purple); pattern = what step; static red = trouble.
2. **Yes to the header band** (row 18): the thin full-width Omarchy field whose brightness tracks running / total slots. PR C is no longer optional.
3. **Playful verbs.** Running cards rotate Claude Code–style gerunds per action next to the block glyph and shimmer, e.g. plan: "Pondering… Sketching… Scheming…", build: "Assembling… Stacking… Forging…", review: "Scrutinizing… Combing… Squinting…", fix: "Patching… Mending… Tinkering…". The plain action word stays visible as a small label so state is never ambiguous; verbs rotate slowly (~every 4 s), never on stuck/dead cards.
4. **All three PRs, queued in order:** A (primitive + slot cards), then B (bar, KPIs, devices, queue, feed), then C (header band + flow-chart halo). One after another because they share `lib/dashboard_page.py`.
5. Still open, defaulted unless Max says otherwise: one motion level everywhere (no separate kiosk mode); normal Claude-builds / Codex-reviews flow; check for open PRs touching `lib/dashboard_page.py` before filing.

## 9. Brief for a design workshop (e.g. Claude Design)

Goal: mock up the animated states so they can be judged before any code.

- **Look:** Omarchy landing hero (https://omarchy.org/): square cells, no anti-aliasing, 8×8 Bayer ordered dither, 5 tone steps on near-black `#0e0e14`. Tokyo Night palette; green `#9ece6a` is our brand. Square corners, Geist + JetBrains Mono.
- **Please design:**
  1. A slot card (about 320×140 px) in each state: plan, build (at 20% / 60% / 95% of timeout), review, fix, claimed, stale heartbeat, dead/overdue/stuck, idle, held. Show the ~24 px pixel strip on top, bot chip, PR number, verb + action label, round, T+ elapsed, heartbeat bar.
  2. The heartbeat ripple and the "done" green settle as short frame sequences.
  3. The header band at low, medium and full load.
  4. KPI digit "decrypt", device load meter (N of max cells), queue conveyor, pixel caret (live / stale / lost).
- **Constraints:** cell size ~4 px; at most 20 fps; nothing cycles faster than ~6 Hz; broken states never move; reduced-motion shows one still frame per state; no external images (canvas or CSS only); colours must stay readable on the dark theme.
- **Questions to explore:** best cell size for card strips vs the header band; how strong the bot tint should be against the green field; verb list tone; whether the build fill reads clearly as progress.
