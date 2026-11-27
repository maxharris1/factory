from __future__ import annotations

from lib.ci_lane import ci_lane
from lib.dashboard_pages import tickets
from lib.dashboard_pages.common import _e, _panel
from lib.dashboard_static import read_asset

CSS = read_asset("css/counts.css")

BLOCKED = "waiting-blocked"
BAD = ("Stop",)
_BEFORE = ({"key": "blocked", "label": "blocked by a dependency", "href": "index.html#intake"},)
_AFTER = {"Build": ({"key": "ci", "label": "ci", "href": "index.html#lanes"},)}
CELLS = _BEFORE + tuple(
    cell
    for phase in tickets.COLUMNS
    for cell in (
        {"key": phase, "label": tickets.COLUMN_NAMES.get(phase, phase).lower(), "href": "job.html"},
        *_AFTER.get(phase, ()),
    )
)


def tally(snapshot: dict) -> dict[str, int | None]:
    found: dict[str, int | None] = dict.fromkeys(cell["key"] for cell in CELLS)
    found.update({col["phase"]: len(col["cards"]) for col in tickets.board(snapshot)["columns"]})
    lane = ci_lane(snapshot)
    found["ci"] = lane["running"] + lane["queued"]
    items = (snapshot.get("feeder") or {}).get("items") or []
    found["blocked"] = sum(item["state"] == BLOCKED for item in items)
    return found


def _cell(cell: dict, count: int | None) -> str:
    cls = ' class="jc-zero"' if not count else ' class="jc-bad"' if cell["key"] in BAD else ""
    return (
        f'<a class="jc-cell" href="{_e(cell["href"])}" data-cell="{_e(cell["key"])}">'
        f'<b{cls}>{_e(count)}</b> {_e(cell["label"])}</a>'
    )


def _counts(snapshot: dict) -> str:
    found = tally(snapshot)
    return _panel("counts", "Where jobs are", "".join(_cell(cell, found[cell["key"]]) for cell in CELLS))
