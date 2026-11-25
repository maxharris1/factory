from __future__ import annotations

from lib.dashboard_pages.common import _e, _panel, _span
from lib.dashboard_static import read_asset

CSS = read_asset("css/routing.css")


SCRIPT = read_asset("js/routing.js")

TITLE = "Role routing"
SOURCE_WORDS = {"roles": "roles", "budget_overrides": "budget override", "inherit": "inherit", "unset": "not set"}
NOTE = ("The first eligible candidate in each list runs. A roles effort above the budget's ceiling is cut to it; "
        "a budget override runs as written.")


def _candidate(cand: dict) -> str:
    head = " · ".join(part for part in (cand["harness"], cand["model"]) if part)
    if cand["effort"] is None:
        return f"<li>{_e(head)}</li>"
    if cand["configured"] == cand["effort"]:
        return f"<li>{_e(head + ' · ')}{_span('rt-e', cand['effort'])}</li>"
    return f"<li>{_e(head + ' · ')}{_span('rt-e clamped', cand['configured'] + ' → ' + cand['effort'])}</li>"


def _pool(row: dict) -> str:
    if not row["candidates"]:
        return _span("muted", "falls back to factory.md " + ("reviewer" if row["role"] == "review" else "builder"))
    return f'<ol class="rt-pool">{"".join(_candidate(cand) for cand in row["candidates"])}</ol>'


def _role_row(row: dict) -> str:
    return (f'<tr><th scope="row">{_e(row["role"])}</th>'
            f'<td>{_e(SOURCE_WORDS[row["source"]])}</td><td>{_pool(row)}</td></tr>')


def _routing(snapshot: dict) -> str:
    matrix = snapshot["routing"]
    if matrix["state"] == "missing":
        return _panel("routing", TITLE, '<p class="empty">config/routing.yml not found</p>')
    if matrix["state"] == "invalid":
        errors = "".join(f"<li>{_e(error)}</li>" for error in matrix["errors"])
        return _panel("routing", TITLE,
                      f'<p class="empty">config/routing.yml is invalid:</p><ul class="rt-errors">{errors}</ul>')
    head = "".join(f"<th>{name}</th>" for name in ("role", "source", "preference order"))
    rows = "".join(_role_row(row) for row in matrix["roles"])
    inner = (
        f'<div class="rt-scroll"><table class="rt-table"><thead><tr>{head}</tr></thead>'
        f"<tbody>{rows}</tbody></table></div>"
        f'<p class="rt-note">{_e(NOTE)}</p>'
    )
    return _panel("routing", TITLE, inner, count=f"{matrix['budget']} budget · effort up to {matrix['ceiling']}")
