"""The dashboard pages: one served CSS, one page script and every page's HTML.

The pages live in lib/dashboard_pages/, one module per Mission Control page;
lib/dashboard_pages/README.md maps each page to its module. This module joins
their CSS and script parts in served order and re-exports every name.
"""

from __future__ import annotations

import base64
import functools
import json
from pathlib import Path

from lib.dashboard_flow import flowmap_layout
from lib.dashboard_pages import (
    activity, common, efficiency, lanes, shell, system, ticket_detail, tickets,
)
from lib.dashboard_pages.activity import (  # noqa: F401
    _ATTENTION, _HEALTH_LINK, _VERDICT, _attention, _health_link, _relay, relay_threads,
)
from lib.dashboard_pages.common import (  # noqa: F401
    _BOTS, _JOB_STATE, LIST_MAX, _b, _bot, _chip, _e, _feed, _job_kind, _job_label, _num, _panel,
    _short, _span, _state_chip, _td, _ticket, _when, anchor, duration,
)
from lib.dashboard_pages.efficiency import _savings  # noqa: F401
from lib.dashboard_pages.lanes import (  # noqa: F401
    _FEEDER_STATE, TILE_STATE, _feeder, _intake, _just_happened, _lanes, _linear_webhook,
    _queue_lane, _slot, intake_groups, lane_devices, lanes_script, probe_down, queue_speed,
)
from lib.dashboard_pages.shell import (  # noqa: F401
    _PAGE_NAMES, _PANELS, _PX_BAD, _PX_HEX, _PX_LINK, PAGES, PX_ACTIONS, PX_BOT_TINTS, PX_BROKEN,
    PX_EFFECTS, PX_FALLBACK, PX_ONESHOTS, PX_SCRIPT, PX_STATE, STATUS_ABSENT_LINK, STATUS_LINKS,
    _attention_strip, _band, _count, _fixed3, _page, _px_progress, _segment, _statusline,
    _topbar, header_load, px_frame, px_job_strip, px_script, px_state, px_strip, px_table,
    shell_table, status_segments,
)
from lib.dashboard_pages.system import (  # noqa: F401
    _ANCHOR, _BUDGET_STATE, _HEALTH_STATE, _LOG_KINDS, _POLL_STATE, _anchor_attr, _components,
    _drift, _health, _log, _log_row, _no_waiter, _rules, _versions, version_drift, version_groups,
)
from lib.dashboard_pages.ticket_detail import (  # noqa: F401
    _detail_model, _job_article, _jobview,
)
from lib.dashboard_pages.tickets import _active_jobs, _board, _tickets  # noqa: F401
from lib.dashboard_telemetry import (  # noqa: F401
    TELEMETRY_CSS, TELEMETRY_SCRIPT, rollup_panel,
)
from lib.health import CODE_COMPONENTS  # noqa: F401
from lib.pr_title import label  # noqa: F401

POLL_MS = 1500

# The Omarchy pair (Geist for text, JetBrains Mono for numbers and labels),
# vendored under lib/fonts with their OFL licenses; see lib/fonts/README.md.
# Each is (family, file, weight range).
FONT_DIR = Path(__file__).resolve().parent / "fonts"
FONTS = (
    ("Geist Variable", "geist-latin-wght-normal.woff2", "100 900"),
    ("JetBrains Mono Variable", "jetbrains-mono-latin-wght-normal.woff2", "100 800"),
)

CSS = "".join((
    common.CSS_BASE, shell.CSS, common.CSS_PANEL, lanes.CSS, common.CSS_PARTS, shell.CSS_NAV,
    system.CSS, ticket_detail.CSS, tickets.CSS, common.CSS_MISC, efficiency.CSS, activity.CSS, shell.CSS_PX,
    common.CSS_TAIL,
))
SCRIPT = "".join((
    common.SCRIPT_HEAD, shell.SCRIPT, lanes.SCRIPT, activity.SCRIPT, tickets.SCRIPT_TICKETS,
    system.SCRIPT_LOG, system.SCRIPT_HEALTH, efficiency.SCRIPT,
    ticket_detail.SCRIPT, common.SCRIPT_TELEMETRY, system.SCRIPT_COMPONENTS,
    common.SCRIPT_TAIL,
))


@functools.lru_cache(maxsize=1)
def font_faces() -> str:
    """The vendored fonts as a JSON list of {family, weight, data} for the page
    script, data being the woff2 file in base64. A missing or unreadable file
    is left out, and the page then uses the fallback fonts."""
    faces = []
    for family, name, weight in FONTS:
        try:
            data = (FONT_DIR / name).read_bytes()
        except OSError:
            continue
        if data[:4] != b"wOF2":
            continue
        faces.append(
            {"family": family, "weight": weight, "data": base64.b64encode(data).decode("ascii")}
        )
    return json.dumps(faces, separators=(",", ":"))


def _flowmap_json(chart: dict | None) -> str:
    return json.dumps(flowmap_layout(chart) if chart else None, separators=(",", ":")).replace("<", "\\u003c")


def page_script(chart: dict | None) -> str:
    return (
        lanes_script(SCRIPT).replace("__TELEMETRY__", TELEMETRY_SCRIPT)
        .replace("__PIXELSTRIP__", px_script())
        .replace("__SHELL__", json.dumps(shell_table(), separators=(",", ":")))
        .replace("__POLL_MS__", str(POLL_MS))
        .replace("__FLOWMAP__", _flowmap_json(chart))
        .replace("__BOARD__", json.dumps(tickets.board_constants(), separators=(",", ":")).replace("<", "\\u003c"))
        .replace("__FONTS__", font_faces())
    )


def render_pages(snapshot: dict) -> dict[str, str]:
    """Every page, by file name. Every value is escaped and already redacted."""
    script = page_script(snapshot.get("statechart"))
    return {name: _page(snapshot, name, css=CSS, script=script) for name in PAGES}
