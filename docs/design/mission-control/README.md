# Factory Mission Control — UI refactor handoff

Start here. This folder is everything needed to rebuild the approved dashboard design in
`maxharris1/factory`. Max approved the design on 2026-11-13 from a live design canvas.

## What is in this folder

| File | What it is | How to use it |
|---|---|---|
| `README.md` | This file | Read first |
| `SPEC.md` | The design: principles, pages, components, motion, data | The source of intent. Read fully before scoping |
| `TICKETS.md` | Proposed ticket split, in order, with acceptance criteria | Turn into factory tickets; adjust the split if the code says otherwise |
| `LIVE-RUN-FOLLOWUP.md` | Comment for PR #100: telemetry needed by the Live run panel | Post on PR #100 to scope the data follow-up |
| `reference/artboards/*.dc.html` | The approved mock-ups and components, as source | **Normative for look and motion.** Exact markup, inline styles, sizes, colours and animation code |
| `reference/HOW-TO-READ-ARTBOARDS.md` | How the `.dc.html` format works | Read before opening any artboard |
| `reference/DESIGN-TOKENS-AND-DATA.md` | Exact tokens, recipes, and the data snapshot the mock-ups use | Copy tokens and recipes from here |
| `reference/ORIGINAL-ANIMATION-PLAN.md` | The first animation plan (pre-design) | Background only. Where it disagrees with `SPEC.md`, `SPEC.md` wins |
| `screenshots/` | One still of every approved page and component | Compare a build against these; see its README for how they were made |

## Rules of the road

1. **One-to-one means:** same layout, spacing, colours, type, copy structure, and the same animation behaviour as the artboards. Copy numeric values from the artboard source; do not round or "improve" them.
2. **The artboards are mock-ups, not production code.** They run on a design-canvas runtime. Port the markup, CSS values and canvas drawing code into `lib/dashboard_page.py` as server-rendered HTML plus the inline SCRIPT, following the patterns already in that file. `SPEC.md` section 8 says how.
3. **Real data only.** Several mock-ups use sample values. `SPEC.md` section 9 lists every one and where the real value comes from. Never ship a sample value. If the data does not exist yet, ship the placeholder text the spec gives and file the data ticket.
4. **The dashboard stays read-only, redacted, and under its CSP** (`default-src 'none'`, inline style and script only). No external assets, no `data:` images, no new dependencies.
5. **Work serially.** Almost every ticket edits `lib/dashboard_page.py`. Do not run two of these tickets at once.
6. **Motion rules are requirements, not taste.** They are tested (`SPEC.md` sections 2 and 10).

## What was and was not verified

- Max reviewed the canvas visually and approved it. The screenshots were rendered from the artboard source with a stand-in runtime, not exported from the canvas; all 16 rendered without script errors. Treat the artboard source plus Max's approval as the reference, and ask Max when anything seems to disagree.
- The live dashboard was read on 2026-11-13 around 20:48 UTC (structure, CSS and data of all five pages). The repo was read at `main` `e9d3683` and PR #100 at branch `mh/job-telemetry`.
- No production code was written or run for this packet.

## Canvas

The live canvas (private to Max): https://claude.ai/artifact/GBzPQKBzzCAiBog9dqmrp5
Pages on it: *Page mock-ups* (the six approved pages), *State map explorations* (journey, phase rail, state map, live run), *Motion studies*, *Components*.
