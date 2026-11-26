from __future__ import annotations

from lib.dashboard_pages.common import _e, _panel, _span
from lib.dashboard_static import read_asset
from lib.role_router import KNOWN_HARNESSES, WAITER_BOTS

CSS = read_asset("css/routing.css")


SCRIPT = read_asset("js/routing.js")

TITLE = "Role routing"
SOURCE_WORDS = {"roles": "roles", "budget_overrides": "budget override", "inherit": "inherit", "unset": "not set"}
NOTE = ("The first eligible candidate in each list runs. A roles effort above the budget's ceiling is cut to it; "
        "a budget override runs as written. Each runs line is the model and effort a new job gets once the bot's "
        "waiter-<bot>.yml argv is applied; CLI default means the harness picks.")


def _value(value: str | None, source: str) -> str:
    if value is None:
        return _span("rt-v default", source)
    return _span("rt-v", value) + _span("rt-src", source)


def _values(found: dict) -> str:
    return f"{_value(found['model'], found['model_source'])} · {_value(found['effort'], found['effort_source'])}"


def _runs(effective: dict) -> str:
    flags = [f"waiter {pinned} replaced" for pinned in effective["replaced"].values()]
    if not effective["applied"]:
        flags.append("routing.yml flags not added by this waiter")
    return f'<div class="rt-runs">runs {_values(effective)}{"".join(_span("rt-flag", flag) for flag in flags)}</div>'


def _head(cand: dict) -> str:
    head = " · ".join(part for part in (cand["harness"], cand["model"]) if part)
    if cand["effort"] is None:
        return _e(head)
    if cand["configured"] == cand["effort"]:
        return f"{_e(head + ' · ')}{_span('rt-e', cand['effort'])}"
    return f"{_e(head + ' · ')}{_span('rt-e clamped', cand['configured'] + ' → ' + cand['effort'])}"


def _candidate(cand: dict) -> str:
    return f"<li>{_head(cand)}{_runs(cand['effective'])}</li>"


def _fallback(row: dict) -> str:
    said = _span("muted", "falls back to factory.md " + ("reviewer" if row["role"] == "review" else "builder"))
    if not row["fallback"]:
        return said
    items = "".join(f"<li>{_e(entry['harness'])}: {_values(entry)}</li>" for entry in row["fallback"])
    return f'{said}<ul class="rt-fallback">{items}</ul>'


def _pool(row: dict) -> str:
    if not row["candidates"]:
        return _fallback(row)
    return f'<ol class="rt-pool">{"".join(_candidate(cand) for cand in row["candidates"])}</ol>'


def _role_row(row: dict) -> str:
    return (f'<tr><th scope="row">{_e(row["role"])}</th>'
            f'<td>{_e(SOURCE_WORDS[row["source"]])}</td><td>{_pool(row)}</td></tr>')


def _unread(waiters: dict) -> str:
    bots = [WAITER_BOTS[harness] for harness in KNOWN_HARNESSES if waiters.get(harness) == "missing"]
    if not bots:
        return ""
    said = f"waiter-<bot>.yml not read for {', '.join(bots)}; their unrouted values show unknown"
    return f'<p class="rt-waiters">{_e(said)}</p>'


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
        f"{_unread(matrix['waiters'])}"
        f'<p class="rt-note">{_e(NOTE)}</p>'
    )
    return _panel("routing", TITLE, inner, count=f"{matrix['budget']} budget · effort up to {matrix['ceiling']}")
