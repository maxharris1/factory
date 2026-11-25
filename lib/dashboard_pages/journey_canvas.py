"""Journey canvas for the ticket page, ported from JourneyClimb.dc.html."""

from __future__ import annotations

import json

from lib.dashboard_flow import FLOWMAP_TINT, STOP_RED
from lib.dashboard_static import read_asset

LANE = {
    "intake": "ask",
    "questions": "ask",
    "waiting-max": "ask",
    "planning": "plan",
    "plan-check": "plan",
    "building": "build",
    "fixing": "build",
    "updating": "ship",
    "review": "review",
    "capped": "review",
    "ship": "ship",
    "awaiting-ok": "ship",
    "waiting-external": "ship",
    "merge-queue": "ship",
    "merged": "shipped",
    "post-merge": "shipped",
    "done": "shipped",
    "handed-off": "shipped",
    "refused": "stop",
    "abandoned": "stop",
    "stuck": "stop",
    "blocked": "stop",
}

TINT = FLOWMAP_TINT
STOP = STOP_RED

_SCRIPT = read_asset("js/journey_canvas.js")

SCRIPT = _SCRIPT.replace(
    "__JOURNEY__", json.dumps({"lane": LANE, "tint": TINT, "stop": STOP}, sort_keys=True)
)
