# Screenshots

One still frame of every approved page and component, at its design size.

**How these were made.** They were rendered from the artboard source in `../reference/artboards/` with a small
stand-in runtime in headless Chrome, using the repo's own fonts (`lib/fonts`). They are not exports from the design
canvas. Each is a single frame taken 1.5 to 5 seconds after load, so anything animated is caught mid-motion.
If an image and the canvas ever disagree, the canvas (and Max) win.

| File | Artboard | Size |
|---|---|---|
| `lanes.png` | `Lanes.dc.html` | 1440 wide |
| `tickets.png` | `Tickets.dc.html` | 1440 wide |
| `ticket-detail.png` | `Job.dc.html` (Full state chart collapsed) | 1440 wide |
| `activity.png` | `Activity.dc.html` | 1440 wide |
| `efficiency.png` | `Efficiency.dc.html` | 1440 wide |
| `system.png` | `System.dc.html` | 1440 wide |
| `journey-even-steps.png` | `JourneyClimb.dc.html`, default time axis | 1000 x 456 |
| `journey-to-scale.png` | `JourneyClimb.dc.html`, time axis to scale | 1000 x 456 |
| `live-run.png` | `RunTape.dc.html` | 1000 wide |
| `full-state-chart.png` | `FlowMap.dc.html` | 1000 x 352 |
| `slot-tile.png` | `SlotTile.dc.html` (fix, claude) | 200 x 124 |
| `slot-card-states.png` | `Main.dc.html`: every strip pattern and state | 1520 x 672 |
| `top-bar.png`, `status-line.png` | `TopBar.dc.html`, `StatusLine.dc.html` | 1440 wide |
| `header-band.png` | `HeaderBand.dc.html`: band at low, medium, full load | 1280 x 470 |
| `small-motion-pieces.png` | `Bits.dc.html`: caret, KPI decrypt, load meter, conveyor, settle | 1160 x 680 |

Stills cannot show motion. For timing and behaviour, read the animation code in the artboards and `../SPEC.md`
sections 2, 6 and 7. A screen recording of Lanes and the ticket page from the canvas would be a useful addition.
