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
| every page: TopBar, header band, attention strip, status line, nav, PixelStrip | `shell.py` | `_topbar`, `_band`, `_attention_strip`, `status_segments`, `_statusline`, `NAV_PAGES` (the nav), `PAGES` (every written page), `_PAGE_NAMES`, `_PANELS`, `_page`, the `px_*` table and `PX_SCRIPT`; the shell CSS and the "every page" script | none (shared) |
| every page: helpers and script runtime | `common.py` | `_e`, `duration`, `_chip`, `_panel`, `_feed`, `_state_chip`, …; the base CSS, the script head (fonts, helpers), the `__TELEMETRY__` slot and the script tail (renderers per page, poll loop); `SCRIPT_GITHUB`, the one #563 sign-in (`GH`, `ghPost`, `ghStart`, `ghAuth` for the sign-in row, `ghResult` for a post's answer) the board, the ticket page, the settings page and the Activity page's attention actions share | none (shared) |
| `index.html` (Lanes) | `lanes.py` | `_feeder`, `_linear_webhook`, `_slot`, `lane_devices`, `_lanes`, `_queue`, `_qa_lane` (the qa lane after the queue: open lanes against the cap per runner and queued qa jobs per project, drawn only when either is nonzero); their CSS, script and the job modal script | #140 |
| `index.html` (CI lane, last in Lanes) | `ci.py` | `ci_lane_html` draws `lib/ci_lane.ci_lane`: one slot per self-hosted runner, then the GitHub-hosted, queued and failed groups, one card per ticket with its progress bar (`ciLaneEl` in the script draws the snapshot's `ci_lane`). `ci_popup`/`ci_extras` are the ticket page's CI dialog (`ticket.html#t=<addr>&ci=1`, `ciPopup`): the per-job rows and the last day's failures. Every github.com link is built from the repo, PR, short head, run and job ids, never a stored URL | #823 |
| `health.html` (System) | `system.py` | `_broken` (Broken now, from `broken_items`; hidden when nothing is broken), `_components` (matrix, the per-device list a phone shows instead, and process details, down rows first), `_versions` (the always-present mount holding `_drift` only when a component is behind; otherwise the Components count carries the `drift` anchor), `_log_row` (`_health` beside `_log` in a `sys-row`), `_rules`, `_no_waiter` (inside the always-present `waiters` mount, drawn only when it has rows) | none |
| `settings.html` (Settings) | `settings.py` | `_settings`: one card per `slots.devices[]` row (local or remote, enabled, slot_max, harnesses, held slots, `drain_state`) with that device's newest `settings.results` (`device_results`); signed in, each card's kept controls (`ST.cards`) POST `/api/settings` `{device, field, expect, value}` and show 202 or the error on that card. `_accounts` draws the Accounts panel, one card per `accounts[]` row (harness, `max_jobs`, `paused`, usage, headroom and floor, running jobs, one sign-in row per `signed_in_on` device) with that account's newest results (`account_results`). Signed in, the Add account form (`ST.addForm`) and each card's kept controls (`ST.accountCards`, `AC_OPS`) POST `/api/settings` `{target: "account", op, account, ...}`. A card with `read_only` set (`default` or `external`) has no controls. Ops and refusals are in `docs/ACCOUNTS.md` | none |
| `job.html` (Tickets) | `tickets.py` | `_board` (filter row, Pipeline columns with Stopped last, which drops an `abandoned` ticket a day after it closed (`STOPPED_WINDOW_SECONDS`), Shipped shelf, from `board` and `phase_table`; a card ends with one QA line per run at its head, `_qa_line`), `_tickets` (the table view); `redirectMoved` in `ticket_detail.py` sends an old `job.html#job-<id>` address for a running or queued job to that job's ticket page | #142, #656, #991 |
| `activity.html` (Activity) | `activity.py` | `_attention`, `_relay`, `relay_threads`, `_health_link`, `_VERDICT`; their CSS and script; the #822 why line and, signed in, the action buttons on each attention row (labels from `lib/max_actions.py`) | none |
| `efficiency.html` (Efficiency) | `efficiency.py` | `_headroom`, `_rounds`, `_savings`, `_jobs_lifetime` (cost: `rollup_panel` in `lib/dashboard_telemetry.py`); `top_row` and `middle_row` wrap two panels each in an `ef-row` | #144 |
| `ticket.html#t=<repo>#<ticket>` (Ticket, no nav item; Tickets is current) | `ticket_detail.py` | `resolve_ticket` (running before queued, exact repo before any case, then the tickets.json records: exact key, then the shown address), `record_row`, `TICKET_KINDS` (strip, tiles and sections per kind: running, queued, stopped, shipped, idle, drawn from the `TILES` and `SECTIONS` registries; a running ticket's Exposure section draws its job's six `EXPOSURE_FIELDS` from the row's `exposure`, "not recorded" when it has none), `ticket_view`; the address is `ticket_address` and the link `ticket_page_href` in `common.py`, which every ticket link on every page uses. The served file holds only the "No ticket chosen" panel; the script draws the ticket from the hash and fetches tickets.json for tickets with no job | #531, #655 |
| `qa/<run_id>/index.html` (QA evidence, served at `/qa/runs/<run_id>`, no nav item) | `qa_evidence.py` | `run_page(run_folder, run_id)`: each lane's scenarios and results from its `result.json` and `manifest.json`, with `<img>`, `<video>` or a link per evidence file, every link relative; a missing lane file or listed file shows "evidence recycled or missing". No script and no database. #875b (#910) writes it before the push; layout and routes in `docs/SPEC.md`, "QA evidence page" | #910 |

## Relay threads

The Relay panel on `activity.html` draws `relay.threads`, one thread per
ticket (`relay_threads`, and `relayThreads` in the script). The collector
(`_relay` in `lib/dashboard.py`) picks 12 tickets: stopped tickets first, the
most recently stopped first, then the newest movers. Each thread carries
its ticket's newest 40 handoffs and verdicts. A verdict reads as its `chip`
and `words` (`_VERDICT_WORDS`), with the raw outcome only in its `title`; an
outcome with no words shows its kind in a muted chip and its raw text. A
verdict's `age_seconds` is the first time
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
4. Threads keep the snapshot's order: newest activity (job or handled
   comment) first, tickets with no known time last.

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
`test_efficiency_golden.py` (with the telemetry CSS and script),
`test_system_golden.py` (`health.html`), `test_settings_golden.py` (`settings.html`) and `test_ticket_golden.py`
(`ticket.html`, its `<main>` and chrome only). `test_shell_golden.py` pins the
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
