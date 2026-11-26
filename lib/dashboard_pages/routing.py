from __future__ import annotations

from lib.dashboard_pages.common import _e, _panel, _span
from lib.dashboard_static import read_asset
from lib.role_router import DISPATCHER

CSS = read_asset("css/routing.css")


SCRIPT = read_asset("js/routing.js")

TITLE = "Role routing"
SOURCE_WORDS = {"roles": "roles", "budget_overrides": "budget override", "inherit": "inherit", "unset": "not set"}
NOTE = ("The first eligible candidate in each list runs. A roles effort above the budget's ceiling is cut to it; "
        "a budget override runs as written. Each runs line is the model and effort a new job gets once the bot's "
        "waiter-<bot>.yml argv is applied; CLI default means the harness picks. A repository's factory.md "
        "routing.slots override runs as written and is not shown.")
UNROUTED = ("A repository whose factory.md has no routing: block, or a factory.md slot naming no model, runs each "
            "waiter's own values: ")


def _value(value: str | None, source: str) -> str:
    if value is None:
        return _span("rt-v default", source)
    return _span("rt-v", value) + _span("rt-src", source)


def _values(found: dict) -> str:
    return f"{_value(found['model'], found['model_source'])} · {_value(found['effort'], found['effort_source'])}"


def _runs(found: dict, flags: list[str]) -> str:
    return f'<div class="rt-runs">runs {_values(found)}{"".join(_span("rt-flag", flag) for flag in flags)}</div>'


def _flags(cand: dict, builder: str | None) -> list[str]:
    effective = cand["effective"]
    flags = [f"waiter {pinned} replaced" for pinned in effective["replaced"].values()]
    if effective["applied"] is False:
        flags.append("routing.yml flags not added by this waiter")
    if cand.get("on_builder") is False:
        flags.append(f"skipped: the builder runs on {builder}")
    return flags


def _head(cand: dict) -> str:
    head = " · ".join(part for part in (cand["harness"], cand["model"]) if part)
    if cand["effort"] is None:
        return _e(head)
    if cand["configured"] == cand["effort"]:
        return f"{_e(head + ' · ')}{_span('rt-e', cand['effort'])}"
    return f"{_e(head + ' · ')}{_span('rt-e clamped', cand['configured'] + ' → ' + cand['effort'])}"


def _pool(items: list[str]) -> str:
    return f'<ol class="rt-pool">{"".join(items)}</ol>'


def _dispatched(row: dict) -> str:
    if not row["candidates"]:
        side = "reviewer" if row["role"] == "review" else "builder"
        return _span("muted", f"falls back to factory.md {side}: its model, else the waiter's")
    builder = row.get("builder")
    items = [f"<li>{_head(cand)}{_runs(cand['effective'], _flags(cand, builder))}</li>" for cand in row["candidates"]]
    if row.get("plans") != "build":
        return _pool(items)
    return f'{_pool(items)}<div class="rt-plans">{_e(f"no plan candidate on {builder}: the build pick plans")}</div>'


def _launched(row: dict) -> str:
    if row["launch"] == DISPATCHER:
        return _dispatched(row)
    pool = _pool([f"<li>{_head(cand)}</li>" for cand in row["candidates"]])
    if row["launch"] is None:
        return pool + _span("muted", "no factory launcher")
    return pool + _runs(row["runs"], ["routing.yml not applied"])


def _role_row(row: dict) -> str:
    return (f'<tr><th scope="row">{_e(row["role"])}</th>'
            f'<td>{_e(SOURCE_WORDS[row["source"]])}</td><td>{_launched(row)}</td></tr>')


def _waiters(waiters: dict) -> str:
    missing = [bot for bot, state in waiters.items() if state == "missing"]
    opaque = [bot for bot, state in waiters.items() if state == "opaque"]
    said = []
    if missing:
        said.append(f"waiter-<bot>.yml not read for {', '.join(missing)}; their unrouted values show unknown")
    if opaque:
        whose = "their" if len(opaque) > 1 else "its"
        said.append(f"waiter-<bot>.yml not interpreted for {', '.join(opaque)}; {whose} values show unknown")
    return "".join(f'<p class="rt-waiters">{_e(line)}</p>' for line in said)


def _unrouted(unrouted: list[dict]) -> str:
    if not unrouted:
        return ""
    entries = "; ".join(f"<span>{_e(entry['harness'])}: {_values(entry)}</span>" for entry in unrouted)
    return f'<p class="rt-unrouted">{_e(UNROUTED)}{entries}</p>'


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
        f"{_waiters(matrix['waiters'])}"
        f"{_unrouted(matrix['unrouted'])}"
        f'<p class="rt-note">{_e(NOTE)}</p>'
    )
    return _panel("routing", TITLE, inner, count=f"{matrix['budget']} budget · effort up to {matrix['ceiling']}")
