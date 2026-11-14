# How to read the `.dc.html` artboards

Each file in `artboards/` is one screen or one component from the design canvas. They are plain text.
You do not need the canvas runtime to port them: read them as annotated HTML.

## Anatomy of a file

```
<x-dc> ... </x-dc>                      the template: ordinary HTML with inline styles
<helmet> ... </helmet>                  page basics only (font link, body margin). Ignore when porting
<script type="text/x-dc" ...>           the logic: `class Component extends DCLogic`
  data-props='{...}'                    the component's inputs and their defaults
```

- **Inline `style="..."` is the design.** Every size, colour, gap and font value is there. Copy them into
  CSS classes in `lib/dashboard_page.py`; the values are exact.
- **`{{ name }}`** is a value from `renderVals()` in the logic class. It is a lookup, not an expression.
  `{{ item.x }}` inside a loop reads the loop item.
- **`<sc-for list="{{ rows }}" as="row">`** repeats its content once per item. **`<sc-if value="{{ flag }}">`** shows its content when the flag is true. Ignore the `hint-*` attributes.
- **`<dc-import name="PixelStrip" effect="fill" ...></dc-import>`** mounts the component in `PixelStrip.dc.html`.
  Every other attribute is a prop for that component (`strip-height` becomes `stripHeight`). Ignore `hint-size`.
- **`onClick="{{ pick }}"`** binds a handler returned by `renderVals()`.
- **The logic class** is a React-style class: `renderVals()` returns the template's inputs,
  `componentDidMount` starts timers and the canvas draw loop, `this.setState` re-renders.
- **`data-props`**: props with an `"editor"` are design levers Max could flip on the canvas (cell size, motion, and so on).
  Their `default` is the approved value.

## Which files matter

| File | Role | Status |
|---|---|---|
| `Lanes.dc.html`, `Tickets.dc.html`, `Job.dc.html`, `Activity.dc.html`, `Efficiency.dc.html`, `System.dc.html` | The six pages | **Approved. Build these.** |
| `TopBar.dc.html`, `StatusLine.dc.html` | Global shell, imported by every page | Approved |
| `PixelStrip.dc.html` | The pixel animation engine (all strip effects) | Approved. Port the drawing code as is |
| `SlotTile.dc.html` | One slot in a lane | Approved |
| `JourneyClimb.dc.html` | The ticket's journey through time | Approved (final after four rounds) |
| `RunTape.dc.html` | "Live run" panel: current task, plan, sub-agents, tokens and cost | Approved. Blocked on data, see `LIVE-RUN-FOLLOWUP.md` |
| `FlowMap.dc.html` | Full state chart, behind a collapsed section on the ticket page | Approved as secondary |
| `Main.dc.html`, `Playground.dc.html`, `HeaderBand.dc.html`, `Bits.dc.html` | Motion studies: every strip state, KPI decrypt, caret, load meter, conveyor, settle | Reference for motion. Not pages |
| `SlotCard.dc.html` | Earlier, larger slot card used by the motion studies | Superseded by `SlotTile` |
| `PhaseRail.dc.html` | Alternative "at a glance" header | Not adopted. Do not build |
| `canvas.json` | Board sizes and canvas layout | Use `w` for each page's design width (1440) |

## Things that are mock-only

- Each canvas component runs its own `requestAnimationFrame` loop. Production uses **one shared loop** for all strips (see `SPEC.md` section 6).
- Timers that fake events (a heartbeat every 15 s in `SlotTile`, a beat every 7 s in `JourneyClimb`, a task every 7 s in `RunTape`, KPI changes in `Bits`) are stand-ins for real events.
- Links between pages (`href="Job.dc.html"`) map to the real page files.
- Fonts load from Google Fonts on the canvas. Production keeps the vendored Geist and JetBrains Mono in `lib/fonts`.
