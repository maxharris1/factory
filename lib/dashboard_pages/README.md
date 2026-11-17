# lib/dashboard_pages

The Mission Control pages, one module per page (#271). `lib/dashboard_page.py`
is the facade: it re-exports every name, joins the CSS and script parts into
`CSS` and `SCRIPT` in served order, and holds `font_faces`, `page_script` and
`render_pages`. `lib/dashboard.py`, `lib/dashboard_telemetry.py` and the tests
import from the facade only.

Each panel has a server render (Python, HTML) and a script twin (in a `SCRIPT`
part) that builds the same markup from `status.json`. Both live in the same
module, so a page ticket touches one module.

## Page to module

Name the module in a ticket's `Touches:` line.

| Page (nav label) | Module | Holds | Next ticket |
|---|---|---|---|
| every page: TopBar, header band, attention strip, status line, nav, PixelStrip | `shell.py` | `_topbar`, `_band`, `_attention_strip`, `status_segments`, `_statusline`, `PAGES`, `_PAGE_NAMES`, `_PANELS`, `_page`, the `px_*` table and `PX_SCRIPT`; the shell CSS and the "every page" script | none (shared) |
| every page: helpers and script runtime | `common.py` | `_e`, `duration`, `_chip`, `_panel`, `_feed`, `_state_chip`, …; the base CSS, the script head (fonts, helpers), the `__TELEMETRY__` slot and the script tail (renderers per page, poll loop) | none (shared) |
| `index.html` (Lanes) | `lanes.py` | `_feeder`, `_linear_webhook`, `_slot`, `lane_devices`, `_lanes`, `_queue`; their CSS, script and the job modal script | #140 |
| `health.html` (System) | `system.py` | `_components` (matrix and process details), `_drift`, `_health`, `_log`, `_rules`, `_no_waiter`; `_drift_row` and `_log_row` wrap two panels each in a `sys-row` | none |
| `job.html` (Tickets) | `tickets.py` | `_board` (filter row, Pipeline columns, Shipped shelf and Stopped group, from `board` and `phase_table`), `_tickets` (the table view), `_active_jobs` | #142 |
| `activity.html` (Activity) | `activity.py` | `_attention`, `_relay`, `relay_threads`, `_health_link`, `_VERDICT`; their CSS and script | none |
| `efficiency.html` (Efficiency) | `efficiency.py` | `_headroom`, `_rounds`, `_savings`, `_jobs_lifetime` (cost: `rollup_panel` in `lib/dashboard_telemetry.py`); `top_row` and `middle_row` wrap two panels each in an `ef-row` | #144 |
| `job.html#job-<id>` (ticket detail) | `ticket_detail.py` | `_job_article`, `_jobview`, `_flow_template`, `_legend`, `_meta`; the job page and flow CSS and script | #146, #149 |

## Relay threads

The Relay panel on `activity.html` merges `handoff_savings.recent` and
`poller.verdicts` into one thread per ticket (`relay_threads`, and
`relayThreads` in the script). A verdict's `age_seconds` is the first time
recorded for its comment: the job it queued (`jobs.created_at`), the job
that posted it (`jobs.finished_at`), the approval it recorded
(`ticket_state.approved_at`) or the ship hold it raised
(`ship_holds.held_at`). It is null when none of these names the comment.
A new shipped verdict plays the settle sweep once (`PX.strip` with
`settle: true`), never on first paint or under reduced motion. The strip
is keyed by the verdict (`repo#ticket:seq`), so each new shipped verdict
on a ticket sweeps, not just the first. The rule:

1. Each handoff and verdict is in exactly one thread, keyed by repo (any
   case) and ticket id.
2. In a thread, timed steps go oldest first. On a tie a handoff goes before
   a verdict; two verdicts keep handled order (`seq`).
3. A verdict with no time goes directly after the verdict handled just
   before it in the thread, or first when it is the thread's first verdict.
4. Threads go newest timed step first. Threads with no timed step go last,
   newest handled verdict first.

`tests/test_dashboard_activity.py` tests each case and holds the two
derivations together.

`_PANELS` in `shell.py` lists the panels each page draws, and `PAGES` in the
script tail (`common.py`) lists each page's script renderers.

## Served order

`CSS` and `SCRIPT` are the concatenation of the modules' parts, in the order of
the two `"".join(...)` lists in `lib/dashboard_page.py`. That order is the
served order. A new part goes into a module and into its list at the place it
is to be served. A part opens with a blank line in `SCRIPT` (its literal opens
with `r"""` and a newline) and with nothing in `CSS` (`"""\`).

The golden tests in `tests/golden/` pin every served byte for three snapshots,
one file per page (#633). Each page splits into its `<style>`, its `<script>`,
its `<main>` and the chrome around them (`tests/golden/pages.py`). A page's
file pins its `<main>`, its chrome and the CSS, script and placeholder values
its module owns: `test_lanes_golden.py` (`index.html`), `test_tickets_golden.py`
(`job.html`, with `ticket_detail.py`), `test_activity_golden.py`,
`test_efficiency_golden.py` (with the telemetry CSS and script) and
`test_system_golden.py` (`health.html`). `test_shell_golden.py` pins the
`common.py` and `shell.py` parts, the served order of the parts and how
`page_script` fills its placeholders, and no page's rendered bytes. A change
that alters a page's bytes on purpose records that page's file again, for
example `python -m tests.golden.test_lanes_golden`. The chrome is the shell's
`_topbar`, `_band`, `_attention_strip` and `_statusline`: a change to any of
them records all five page files, and a ticket that makes one names all five on
its Touches line (`tests/golden/test_chrome_golden.py`). A change to a shared
part or the served order records `python -m tests.golden.test_shell_golden`.

## Shared files

`common.py`, `shell.py`, `lib/dashboard_page.py` and
`tests/golden/test_shell_golden.py` are shared by every page: a ticket that
touches one of them conflicts with every other Mission Control ticket. A new
part in the facade's join lists also goes into `CSS_ORDER` or `SCRIPT_ORDER`
in `test_shell_golden.py`.

Imports run one way: `common` ← the page modules ← `shell` ← the facade. A
page module may import a sibling (`lanes` uses `activity._health_link`,
`ticket_detail` uses `tickets._active_jobs`). Nothing in the package imports
`lib.dashboard_page`. `shell` imports the page modules for `_PANELS`, so a
page module that draws a PixelStrip reaches `px_strip` through a
function-local import (`tickets._px`).

The board's phases come from the chart (`lib/dashboard_flow.py`
`ticket_phases`). The script derives the same table from each snapshot's
`statechart` (`phaseTable`, fed the constants from `tickets.board_constants`),
so a page built while the chart was missing recovers when it returns. The
server/script parity tests in `tests/test_dashboard_tickets_board.py` hold the
two derivations together.
