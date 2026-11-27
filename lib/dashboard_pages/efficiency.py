from __future__ import annotations

import math

from lib import device_pool
from lib.dashboard_pages.common import _b, _e, _num, _panel, _span, duration
from lib.dashboard_telemetry import rollup_panel
from lib.dashboard_static import read_asset

CSS = read_asset("css/efficiency.css")


SCRIPT = read_asset("js/efficiency.js")

HEADROOM_NOTE = "Headroom is what is left in the fullest window."
SHIPPED = ("merged", "done", "handed-off")
ROUND_BUCKETS = ("0 rounds", "1 round", "2 rounds", "3 rounds", "4 or more")
METER_CELLS = 20
WINDOWS = ("five_hour", "seven_day")
WINDOW_NAMES = {"five_hour": "5-hour", "seven_day": "7-day"}
SKEW = {
    "high": ("chip warn", "skew high"),
    "low": ("chip warn", "skew low"),
    "balanced": ("muted", "balanced"),
    "none": ("muted", "—"),
}
BALANCE_NOTE = (
    "Share is this account's part of its pool's recent placements. Skew flags a share more than "
    f"{round(device_pool.SKEW_THRESHOLD * 100)} points off an even split once the pool has "
    f"{device_pool.SKEW_MIN_PLACEMENTS} or more placements."
)


def _half_up(value: float) -> int:
    return math.floor(value + 0.5)


def _tenths(value: int) -> str:
    return f"{value // 10}.{value % 10}"


def _row(*panels: str) -> str:
    return f'<div class="ef-row">{"".join(panels)}</div>'


def _savings(snapshot: dict) -> str:
    savings = snapshot["handoff_savings"]
    share = savings["scripted_share"]
    lit = None if share is None else _half_up(share * 100)
    cells = "".join('<span class="s"></span>' if lit is not None and i < lit else "<span></span>"
                    for i in range(100))
    label = (
        "no handoffs yet" if lit is None
        else f"{lit} of 100 squares are scripted handoffs, {100 - lit} are grok wakes"
    )
    counts = "".join(
        f"<div>{_b(value, cls)}{_span(None, text)}</div>"
        for value, text, cls in (
            (savings["scripted"], "scripted handoffs", ""),
            (savings["scripted_in_flight"], "scripted in flight", ""),
            (savings["bot_to_bot"], "bot-to-bot (grok wakes)", "grok"),
            (savings["engine_handled"], "engine handled", ""),
            (savings["engine_escalated"], "engine escalated", ""),
        )
    )
    inner = (
        '<div class="ef-savings">'
        f'<div class="{"waffle none" if lit is None else "waffle"}" role="img" aria-label="{_e(label)}">{cells}</div>'
        f'<div class="ef-share"><b>{"—" if lit is None else f"{lit}%"}</b><span>scripted share</span></div>'
        f'<div class="ef-counts">{counts}</div></div>'
        f'<p class="definition" id="share-definition">{_e(savings["scripted_share_definition"])}</p>'
        "<details><summary>how each count is made</summary>"
        f'<p class="muted">{_e(savings["definition"])}</p></details>'
    )
    return _panel("savings", "Handoff savings", inner, count="scripted / (scripted + grok wakes)")


def _jobs_lifetime(snapshot: dict) -> str:
    totals = snapshot["queue"]["totals"]
    done, failed = totals["done"], totals["failed"]
    finished = done + failed
    rate = (
        f"{_half_up(100 * done / finished)}% of {_num(finished)} finished jobs succeeded" if finished
        else "no finished jobs yet"
    )
    inner = (
        f'<span class="ef-life">{_b(done, "ok")} jobs done</span>'
        '<span class="ef-dot" aria-hidden="true">·</span>'
        f'<span class="ef-life">{_b(failed, "bad")} failed</span>'
        + _span("ef-pct", rate)
    )
    return _panel("jobs-lifetime", "Lifetime", inner)


def _rounds(snapshot: dict) -> str:
    shipped = [row for row in snapshot["tickets"] if row["state"] in SHIPPED]
    total = len(shipped)
    count = f"{total} shipped tickets"
    if not shipped:
        return _panel("rounds", "Revise rounds before ship",
                      '<p class="empty">no shipped tickets yet</p>', count=count, extra="ef-wide")
    counts = [0] * len(ROUND_BUCKETS)
    for row in shipped:
        counts[min(row["round"], len(ROUND_BUCKETS) - 1)] += 1
    top = max(counts)
    bars = "".join(
        '<div class="ef-rb">'
        + _span("ef-k", label)
        + '<span class="ef-track" aria-hidden="true">'
        + f'<span class="{"ef-fill" if i else "ef-fill zero"}" style="width:{_tenths((2000 * n + top) // (2 * top))}%">'
        + "</span></span>"
        + f'<span class="ef-n">{_b(n, "" if i else "ok")} · {(200 * n + total) // (2 * total)}%</span>'
        + "</div>"
        for i, (label, n) in enumerate(zip(ROUND_BUCKETS, counts))
    )
    note = f"{counts[0]} of {total} shipped with no revise round"
    return _panel(
        "rounds", "Revise rounds before ship",
        f'<div class="ef-rounds">{bars}</div>'
        f'<div class="ef-foot"><p class="ef-note">{note}</p><a href="job.html">see shipped tickets</a></div>',
        count=count, extra="ef-wide",
    )


def _tone(room: float) -> str:
    return "ok" if room >= 0.5 else "warn" if room >= 0.2 else "bad"


def _view(entry: dict, now: float) -> dict:
    state = entry["state"]
    fresh = [w for w in WINDOWS if entry[w]["state"] == "ok"]
    planned = [w for w in WINDOWS if entry[w]["state"] != "not on plan"]
    if state == "no telemetry":
        windows = [(state, "muted")] * len(WINDOWS)
        resets = (state, "muted")
    else:
        windows = [(f"{_half_up(entry[w]['used'] * 100)}% used", None) if w in fresh
                   else ("not on this plan" if entry[w]["state"] == "not on plan" else entry[w]["state"], "muted")
                   for w in WINDOWS]
        ends = [entry[w]["resets_at"] for w in fresh if entry[w]["resets_at"] is not None]
        resets = (f"in {duration(min(ends) - now)}", None) if ends else ("no reading", "muted")
    view = {"entry": entry, "windows": windows, "resets": resets, "limit": entry["limit_label"],
            "pct": None, "tone": None, "tag": None, "lit": 0}
    if entry["limit_label"] or not fresh:
        reason = entry["limit_label"] or ("no telemetry" if state == "no telemetry" else "no reading")
        return {**view, "label": f"{_name(entry)} headroom: {reason}"}
    only = WINDOW_NAMES[fresh[0]] if len(fresh) == 1 and len(planned) > 1 else None
    pct = _half_up(entry["headroom"] * 100)
    lit = (2 * pct + 5) // 10
    window = "" if only is None else f", from the {only} window only"
    return {
        **view, "pct": pct, "lit": lit, "tone": "neutral" if only else _tone(entry["headroom"]),
        "tag": None if only is None else f"({only} only)",
        "label": f"{_name(entry)} headroom {pct} percent{window}: {lit} of {METER_CELLS} cells lit",
    }


def _cell(text: str, cls: str | None, tag: str = "td") -> str:
    return f'<{tag} class="{f"ef-v {cls}" if cls else "ef-v"}">{_e(text)}</{tag}>'


def _name(entry: dict) -> str:
    return entry["label"] or entry["account"]


def _about(entry: dict) -> str:
    age = entry["age_seconds"]
    return " · ".join(part for part in (
        entry["plan"],
        entry["source"] and f"via {entry['source']}",
        "never read" if age is None else f"read {duration(age)} ago",
    ) if part)


def _jobs(entry: dict) -> str:
    cap = "no max" if entry["max_jobs"] is None else f"max {entry['max_jobs']}"
    return f"{entry['running']} running · {cap}"


def _devices(entry: dict) -> str:
    parts = (
        ", ".join(f"{row['device']} {row['status']}" for row in entry["doctor"]),
        entry["running_on"] and f"running on {', '.join(entry['running_on'])}",
    )
    return " · ".join(part for part in parts if part) or "none"


def _who(entry: dict) -> str:
    harness = entry["harness"]
    return (
        f'<div class="ef-who">{_span("bot " + harness, harness)}{_span("ef-name", _name(entry))}</div>'
        f'<div class="ef-dev">{_e(_about(entry))}</div>'
    )


def _untracked_who(entry: dict) -> str:
    harness, plan = entry["harness"], entry["plan"]
    return (
        f'<div class="ef-who">{_span("bot " + harness, harness)}{_span("ef-name", entry["account"])}</div>'
        + ("" if plan is None else f'<div class="ef-dev">{_e(plan)}</div>')
    )


def _untracked_row(entry: dict) -> str:
    return (
        f'<tr class="ef-un"><td>{_untracked_who(entry)}</td>'
        f'<td class="ef-v muted" colspan="4">{_e("not tracked: " + entry["not_tracked"])}</td>'
        + _cell(_jobs(entry), "muted") + _cell(_devices(entry), "muted") + "</tr>"
    )


def _untracked_block(entry: dict) -> str:
    return (
        f'<div class="ef-un">{_untracked_who(entry)}'
        + _cell("not tracked: " + entry["not_tracked"], "muted", "div")
        + f'<div class="ef-dev">{_e(_jobs(entry) + " · " + _devices(entry))}</div></div>'
    )


def _number(view: dict, *, phone: bool) -> str:
    if view["limit"]:
        return _span("ef-lim bad", view["limit"])
    if view["pct"] is not None:
        return _span(f"ef-hn {view['tone']}", f"{view['pct']}%")
    return "" if phone else _span("ef-hn", "")


def _meter(view: dict) -> str:
    lit = view["lit"]
    cells = f'<span class="{view["tone"]}"></span>' * lit + "<span></span>" * (METER_CELLS - lit)
    return f'<span class="ef-meter" role="img" aria-label="{_e(view["label"])}">{cells}</span>'


def _account_row(view: dict) -> str:
    tag = "" if view["tag"] is None else _span("ef-tag", view["tag"])
    return (
        f"<tr><td>{_who(view['entry'])}</td>"
        + "".join(_cell(text, cls) for text, cls in (*view["windows"], view["resets"]))
        + f'<td><div class="ef-room">{_number(view, phone=False)}{_meter(view)}{tag}</div></td>'
        + _cell(_jobs(view["entry"]), None)
        + _cell(_devices(view["entry"]), None)
        + "</tr>"
    )


def _account_block(view: dict) -> str:
    label = "headroom" if view["tag"] is None else f"headroom {view['tag']}"
    columns = "".join(
        f'<div><div class="ef-wk">{name}</div>{_cell(text, cls, "div")}</div>'
        for name, (text, cls) in zip(("5-hour", "7-day", "resets"), (*view["windows"], view["resets"]))
    )
    return (
        f'<div class="ef-acct">{_who(view["entry"])}'
        f'<div class="ef-room">{_number(view, phone=True)}{_meter(view)}</div>'
        f'<div class="ef-hl">{_e(label)}</div><div class="ef-wins">{columns}</div>'
        f'<div class="ef-dev">{_e(_jobs(view["entry"]) + " · " + _devices(view["entry"]))}</div></div>'
    )


def _headroom(snapshot: dict) -> str:
    accounts = snapshot["usage"]["accounts"]
    views = [_view(e, snapshot["generated_at_epoch"]) for e in accounts if e["state"] != "not tracked"]
    untracked = [e for e in accounts if e["state"] == "not tracked"]
    head = "".join(f"<th>{name}</th>" for name in
                   ("account", "5-hour window", "7-day window", "resets", "headroom", "jobs", "devices"))
    rows = "".join(_account_row(view) for view in views) + "".join(_untracked_row(e) for e in untracked)
    blocks = "".join(_account_block(view) for view in views) + "".join(_untracked_block(e) for e in untracked)
    inner = (
        f'<div class="ef-scroll"><table class="ef-usage"><thead><tr>{head}</tr></thead>'
        f"<tbody>{rows}</tbody></table></div>"
        f'<p class="ef-hnote">{HEADROOM_NOTE}</p>'
        f'<div class="ef-accts">{blocks}</div>'
        f'<p class="ef-hnote-ph">{HEADROOM_NOTE}</p>'
    )
    return _panel("headroom", "Subscription headroom", inner, count="per account", extra="ef-wide")


def top_row(snapshot: dict) -> str:
    return _row(rollup_panel(snapshot))


def headroom_row(snapshot: dict) -> str:
    return _row(_headroom(snapshot))


def middle_row(snapshot: dict) -> str:
    return _row(_rounds(snapshot), _savings(snapshot))


def _room(window: dict) -> str:
    if window["used"] is None:
        return f'<td class="bl-v muted">{_e(window["state"])}</td>'
    return f'<td class="bl-v">{100 - _half_up(window["used"] * 100)}%</td>'


def _balance_row(entry: dict) -> str:
    share = entry["placement_share"]
    harness = entry["harness"]
    return (
        f'<tr><th scope="row">{_span("bot " + harness, harness)}{_span("bl-name", _name(entry))}</th>'
        + "".join(_room(entry[w]) for w in WINDOWS)
        + f'<td class="bl-v">{_num(entry["recent_placements"])}</td>'
        + f'<td class="bl-v">{"—" if share is None else f"{_half_up(share * 100)}%"}</td>'
        + f'<td class="bl-v">{_span(*SKEW[entry["skew"]])}</td></tr>'
    )


def _balance_pool(harness: str, entries: list[dict]) -> str:
    size = f"{len(entries)} account" + ("" if len(entries) == 1 else "s")
    return (
        f'<tbody><tr class="bl-pool"><th colspan="6" scope="rowgroup">{_e(f"{harness} · {size}")}</th></tr>'
        + "".join(_balance_row(entry) for entry in entries)
        + "</tbody>"
    )


def _balance(snapshot: dict) -> str:
    pools: dict[str, list[dict]] = {}
    for entry in snapshot["usage"]["accounts"]:
        pools.setdefault(entry["harness"], []).append(entry)
    if not pools:
        inner = '<p class="empty">no accounts</p>'
    else:
        head = "".join(f"<th>{name}</th>" for name in (
            "account", "5-hour headroom", "7-day headroom", "recent placements", "share", "skew"))
        inner = (
            f'<div class="bl-scroll"><table class="bl-table"><thead><tr>{head}</tr></thead>'
            + "".join(_balance_pool(harness, entries) for harness, entries in pools.items())
            + f'</table></div><p class="bl-note">{_e(BALANCE_NOTE)}</p>'
        )
    return _panel("balance", "Account balance", inner, count="recent placements per harness pool")


def balance_row(snapshot: dict) -> str:
    return _row(_balance(snapshot))
