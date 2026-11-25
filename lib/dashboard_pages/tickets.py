"""job.html (Tickets): the Tickets board and, as its table view, the tickets table."""

from __future__ import annotations

import re
from collections import Counter

from lib.dashboard_flow import NOT_FACTORY_STEPS, PHASE_OVERRIDES, PHASES, PIPELINE, ticket_phase, ticket_phases
from lib.dispatch.constants import HANDED_OFF_EXTERNALLY
from lib.dashboard_pages.activity import _VERDICT
from lib.dashboard_pages.lanes import TILE_STATE
from lib.dashboard_pages.common import (
    _JOB_STATE, _bot, checks_link, checks_named, _chip, duration, _e, github_links, _panel, _short, _span, _td,
    _ticket, ticket_page_href,
)
from lib.dashboard_static import read_asset


CSS = read_asset("css/tickets.css")


SCRIPT_TICKETS = read_asset("js/tickets.js")


GLYPH_TINT = "#a9b1d6"
PHASE_GLYPH = {
    "Ask": {"effect": "twinkle", "seed": 71},
    "Plan": {"effect": "field", "seed": 72},
    "Build": {"effect": "fill", "progress": 0.6, "seed": 73},
    "Review": {"effect": "scan", "seed": 74},
    "Ship": {"effect": "off", "done": True, "seed": 75},
    "Stop": {"effect": "static", "tint": "#f7768e", "seed": 76},
}
WAITING = ("awaiting-ok", "waiting-max")
CARD_KINDS = {
    "live": {"group": 0, "source": "live", "age": "elapsed_seconds"},
    "stale": {"group": 0, "source": "live", "age": "elapsed_seconds"},
    "queued": {"group": 1, "source": "queued", "age": "age_seconds"},
    "idle": {"group": 2, "source": "ticket", "age": "state_age_seconds"},
}
CAP = 6
SHELF_NEWEST = 10
SHELF_PHONE = 5
SHELF_PAGE = 50
STOPPED_WINDOW_SECONDS = 24 * 3600
FIND_MAX = 8
FIND = (
    '<div class="tk-find" role="search"><label for="find-ticket">Find a ticket</label>'
    '<input id="find-ticket" type="text" name="ticket" placeholder="factory#470" autocomplete="off" spellcheck="false" '
    'role="combobox" aria-autocomplete="list" aria-controls="find-ticket-list" aria-expanded="false">'
    '<a class="tk-open" aria-disabled="true">open</a>'
    '<ul class="tk-matches" id="find-ticket-list" role="listbox" aria-label="Matching tickets" hidden></ul></div>'
)
JOB_CHIPS = {
    **{name: TILE_STATE[name]["chip"] for name in ("claimed", "stale", "dead")},
    "runner-gone": list(_JOB_STATE["runner-gone"]),
    "overdue": ["overdue", "bad"],
}
_WAIT_TINT = "#ff9e64"
_EMPTY = {"Ask": "nothing waiting on questions", "Stop": "nothing stopped"}
COLUMNS = (*PIPELINE, "Stop")
COLUMN_NAMES = {"Stop": "Stopped"}
_ROUND = re.compile(r"([0-9]+)/([0-9]+)")
QA_TONES = {"queued": "muted", "running": "accent", "PASS": "ok", "FAIL": "bad", "BLOCKED": "warn"}
_QA_RUN = re.compile(r"[0-9a-f]{32}")


def _phase_glyph(phase: str) -> dict:
    return {"tint": GLYPH_TINT, **PHASE_GLYPH[phase], "cell": 2, "height": 16}


def board_constants() -> dict:
    return {
        "columns": COLUMNS, "names": COLUMN_NAMES, "phases": PHASES, "overrides": PHASE_OVERRIDES,
        "not_factory_steps": sorted(NOT_FACTORY_STEPS),
        "chips": JOB_CHIPS, "kinds": CARD_KINDS, "cap": CAP, "shelf_newest": SHELF_NEWEST, "shelf_page": SHELF_PAGE,
        "shelf_phone": SHELF_PHONE, "find_max": FIND_MAX, "stopped_window": STOPPED_WINDOW_SECONDS,
        "qa_tones": QA_TONES,
        "glyphs": {phase: _phase_glyph(phase) for phase in PHASE_GLYPH},
    }


def phase_table(chart: dict | None) -> dict:
    phases = ticket_phases(chart)

    def states(phase: str) -> list[str]:
        return [name for name, found in phases.items() if found == phase]

    return {
        "states": phases,
        "columns": [
            {
                "phase": phase, "name": COLUMN_NAMES.get(phase, phase), "states": states(phase),
                "glyph": _phase_glyph(phase),
            }
            for phase in COLUMNS
        ] if phases else [],
    }


def _key(row: dict) -> str:
    return f"{row['repo']}#{row['ticket_id']}"


def _join(row: dict) -> str:
    return _key(row).casefold()


def _first(rows: list | None) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for row in rows or ():
        found.setdefault(_join(row), row)
    return found


def _kind(live: dict | None, queued: dict | None) -> str:
    if live:
        return "stale" if live.get("job_state") == "stale" else "live"
    return "queued" if queued else "idle"


def _rank(card: dict) -> tuple:
    kind = CARD_KINDS[card["kind"]]
    age = card[kind["source"]].get(kind["age"])
    return kind["group"], age is None, -(age or 0)


def _closed_long_ago(ticket: dict) -> bool:
    age = ticket.get("state_age_seconds")
    return ticket["state"] == "abandoned" and age is not None and age >= STOPPED_WINDOW_SECONDS


def _ship_rank(card: dict) -> tuple:
    at = card["ticket"].get("shipped_at")
    return at is None, -(at or 0), _key(card["ticket"])


def board(snapshot: dict, project: str | None = None) -> dict:
    table = phase_table(snapshot.get("statechart"))
    rows = snapshot["tickets"]
    counts = Counter(row["repo"] for row in rows)
    live: dict[str, dict] = {}
    for row in snapshot["running"]:
        best = live.get(_join(row))
        if best is None or (row.get("elapsed_seconds") or 0) < (best.get("elapsed_seconds") or 0):
            live[_join(row)] = row
    queued = _first(snapshot["queued"])
    verdicts = _first(snapshot["poller"].get("verdicts"))
    reasons = {key: row.get("reason") for key, row in _first(snapshot["stuck"]["tickets"]).items()}
    qa: dict[str, list[dict]] = {}
    for run in snapshot.get("qa", {}).get("runs", []):
        qa.setdefault(_join(run), []).append(run)
    generated = snapshot.get("generated_at_epoch")
    cards = [
        {
            "ticket": row,
            "phase": ticket_phase(table["states"], row["state"], row["stuck"]),
            "live": live.get(_join(row)),
            "queued": queued.get(_join(row)),
            "kind": _kind(live.get(_join(row)), queued.get(_join(row))),
            "verdict": verdicts.get(_join(row)),
            "reason": reasons.get(_join(row)),
            "qa": qa.get(_join(row), []),
            "ago": None if row.get("shipped_at") is None or generated is None else generated - row["shipped_at"],
        }
        for row in rows
        if project in (None, row["repo"])
    ]
    tracked = len(cards)
    cards = [card for card in cards if not _closed_long_ago(card["ticket"])]
    columns = []
    for col in table["columns"]:
        ranked = sorted((c for c in cards if c["phase"] == col["phase"]), key=_rank)
        columns.append({**col, "cards": ranked, "shown": ranked[:CAP], "more": ranked[CAP:]})
    busy = [col["phase"] for col in columns if any(c["kind"] in ("live", "stale") for c in col["cards"])]
    filled = [col["phase"] for col in columns if col["cards"]]
    tiles = sorted((c for c in cards if c["phase"] == "Shipped"), key=_ship_rank)
    handed = sum(c["ticket"].get("shipped_how") == "handed-off" for c in tiles)
    return {
        "projects": sorted(counts.items(), key=lambda item: (-item[1], _short(item[0]), item[0])),
        "total": len(rows),
        "tracked": tracked,
        "available": bool(table["columns"]),
        "open": sum(len(col["cards"]) for col in columns),
        "shipped": len(tiles),
        "columns": columns,
        "start": (busy + filled + [COLUMNS[0]])[0],
        "shelf": {
            "merged": len(tiles) - handed, "handed": handed, "tiles": tiles,
            "newest": tiles[:SHELF_NEWEST], "earlier": tiles[SHELF_NEWEST:],
        },
    }


def _px():
    from lib.dashboard_pages import shell

    return shell


def _glyph(key: str, glyph: dict) -> str:
    return f'<span class="tk-glyph" aria-hidden="true">{_px().px_strip(key, **glyph)}</span>'


def _round(ticket: dict, job: dict | None) -> str:
    rnd = job.get("round") if job else None
    match = _ROUND.fullmatch(rnd or "")
    if match:
        done = int(match.group(1))
        pips = "".join(
            '<span class="done"></span>' if i < done - 1 else '<span class="now"></span>' if i == done - 1
            else "<span></span>"
            for i in range(int(match.group(2)))
        )
        return f'<span class="tk-pips" aria-hidden="true">{pips}</span>{_span("tk-round", f"round {rnd}")}'
    if job:
        return _span("tk-round", "round —")
    return _span("tk-round", f"round {ticket['round']}") if ticket["round"] else ""


def _chips(live: dict | None) -> str:
    if not live:
        return ""
    state = live.get("stale_cause") or live.get("job_state")
    chips = [JOB_CHIPS.get(str(state))] + [JOB_CHIPS["overdue"] if live.get("overdue") else None]
    return "".join(_chip(text, tone) for text, tone in filter(None, chips))


def _in_state(ticket: dict) -> str:
    age = ticket.get("state_age_seconds")
    return _span("tk-in", f"in {ticket['state']}" + ("" if age is None else f" {duration(age)}"))


def _qa_href(halves: object) -> str | None:
    run = "".join(map(str, halves)) if isinstance(halves, list) and len(halves) == 2 else ""
    return f"/qa/runs/{run}" if _QA_RUN.fullmatch(run) else None


def _qa_line(run: dict) -> str:
    facts = [run["state"], run["scope"]]
    if run["lanes"]:
        facts.append(f"{run['lanes']} lane" + ("" if run["lanes"] == 1 else "s"))
    if run["passed"] is not None:
        facts.append(f"{run['passed']}/{run['total']} scenarios")
    label = f"QA {run['result']}" if run["result"] else "QA"
    shootout = _span("tk-tag", "shootout") if run["arm"] == "shootout" else ""
    href = _qa_href(run["run"])
    evidence = f'<a href="{href}">evidence</a>' if href else ""
    return (
        f'<div class="tk-run">{_chip(label, QA_TONES.get(run["result"] or run["state"], "muted"))}'
        f'{_span("tk-dev", run["runner"])}{shootout}{_span("tk-out", " · ".join(facts))}{evidence}'
        f'{_span("tk-t", duration(run["elapsed_seconds"]))}</div>'
    )


def _card(card: dict) -> str:
    ticket, live, kind = card["ticket"], card["live"], card["kind"]
    job = live or card["queued"]
    stop = card["phase"] == "Stop"
    waiting = ticket["state"] in WAITING
    ci = None if stop or job else ticket.get("ci_wait")
    key = f"ticket-{_key(ticket)}"
    px = _px()
    if stop:
        strip = px.px_strip(key, state="stuck", height=8)
    elif waiting or ci:
        strip = px.px_strip(key, effect="static", tint=_WAIT_TINT, height=8)
    elif kind == "queued":
        strip = px.px_strip(key, effect="conveyor", tint="bot", bot=job["recipient"], still=True, height=8)
    else:
        strip = px.px_job_strip(live, height=8) if live else ""
    ident = f'<a href="{_e(ticket_page_href(ticket))}">{_e(_ticket(ticket))}</a>'
    lines = ""
    if stop:
        state = (_chip("stuck", "bad") if ticket["stuck"] else "") + _in_state(ticket) + _chips(live)
        lines = f'<div class="tk-reason">{_e(card["reason"])}</div>' if card["reason"] else ""
    else:
        state = (
            _in_state(ticket) if kind == "idle"
            else _span(None, ticket["state"]) + (_span("tk-q", "queued") if kind == "queued" else _chips(live))
        ) + _round(ticket, job)
        wait_line = waiting and ticket.get("waiting_on")
        if wait_line:
            lines += f'<div class="tk-wait">waits on {_span(None, ticket["waiting_on"])}</div>'
        if kind == "queued":
            lines += (
                f'<div class="tk-run">{_span("tk-dev", "for")}{_bot(job["recipient"])}'
                f'{_span("tk-t", "waiting " + duration(job.get("age_seconds")))}</div>'
            )
        elif live:
            where = _span("tk-dev", f"@ {live['device']}") if live.get("device") else ""
            since = _span("tk-t", "T+ " + duration(live["elapsed_seconds"]))
            lines += f'<div class="tk-run">{_bot(live["recipient"])}{where}{since}</div>'
            if kind == "stale":
                hb = duration(live.get("heartbeat_age_seconds"))
                lines += f'<div class="tk-hb">{_e("no heartbeat for " + hb)}</div>'
        else:
            if ci:
                lines += (
                    f'<div class="tk-ci">{_chip("waiting on CI", "warn")}{_span("tk-out", checks_named(ci))}'
                    f'{checks_link(ticket)}{_span("tk-t", "waiting " + duration(ci["waiting_seconds"]))}</div>'
                )
            if no_ci := ticket.get("no_ci"):
                gate = (
                    f'{_chip("local gate passed", "ok")}{_span("tk-dev", no_ci["gate_head"])}'
                    if no_ci.get("gate_head")
                    else ""
                )
                lines += (
                    f'<div class="tk-ci">{_chip("no CI on this base", "muted")}'
                    f'{_span("tk-out", no_ci["base"])}{gate}</div>'
                )
            verdict = card["verdict"]
            if verdict and not (wait_line and verdict["kind"] == "waiting"):
                lines += (
                    f'<div class="tk-run">{_chip(verdict["kind"], _VERDICT.get(verdict["kind"], "muted"))}'
                    f'{_span("tk-out", verdict["outcome"])}</div>'
                )
    lines += "".join(_qa_line(run) for run in card["qa"])
    cls = "tk-card tk-stop" if stop else "tk-card tk-stale" if kind == "stale" else "tk-card"
    return (
        f'<div class="{cls}" data-ticket="{_e(_key(ticket))}"'
        + (' data-wait="ci">' if ci else ">")
        + (f'<div class="tk-strip" aria-hidden="true">{strip}</div>' if strip else "")
        + f'<div class="tk-body"><div class="tk-id">{ident}{_span("tk-tag", ticket["tracker"])}{github_links(ticket)}</div>'
        f'<div class="tk-state">{state}</div>{lines}</div></div>'
    )


def _phases(columns: list[dict], picked: str) -> str:
    buttons = ""
    for col in columns:
        n = len(col["cards"])
        count = "tk-phc tk-none" if not n else "tk-phc tk-stopped" if col["phase"] == "Stop" else "tk-phc"
        label = f"{col['name']}, {n} ticket" + ("" if n == 1 else "s")
        buttons += (
            f'<button class="tk-ph" type="button" data-pick="{_e(col["phase"])}" '
            f'aria-pressed="{"true" if col["phase"] == picked else "false"}" aria-label="{_e(label)}">'
            f'{_span("tk-phn", col["name"])}{_span(count, n)}</button>'
        )
    return f'<div class="tk-phases" role="group" aria-label="Phase">{buttons}</div>'


def _column(col: dict, picked: bool) -> str:
    phase = col["phase"]
    cards = "".join(_card(card) for card in col["shown"]) or (
        f'<div class="tk-empty">{_e(_EMPTY.get(phase, f"nothing in {phase.lower()}"))}</div>'
    )
    if col["more"]:
        cards += (
            f'<details class="tk-more"><summary>+{len(col["more"])} more</summary>'
            f'<div class="tk-more-list">{"".join(_card(card) for card in col["more"])}</div></details>'
        )
    count = "tk-count tk-stopped" if phase == "Stop" and col["cards"] else "tk-count"
    return (
        f'<div class="{"tk-col tk-picked" if picked else "tk-col"}" data-phase="{_e(phase)}"><div class="tk-head">'
        '<div class="tk-headrow">'
        f'{_glyph(f"phase-{phase}", col["glyph"])}{_span("tk-name", col["name"])}{_span(count, len(col["cards"]))}'
        f'</div>{_span("tk-states", " · ".join(col["states"]))}</div>{cards}</div>'
    )


def _button(text: str, attr: str, value: str, on: bool) -> str:
    return (
        f'<button class="tk-btn" type="button" aria-pressed="{"true" if on else "false"}" '
        f'{attr}="{_e(value)}">{_e(text)}</button>'
    )


def _tiles(cards: list[dict]) -> str:
    tiles = ""
    for card in cards:
        ticket, ago = card["ticket"], card["ago"]
        name, external = _ticket(ticket), ticket["state"] == HANDED_OFF_EXTERNALLY
        how = "handed off in Linear" if external else (
            "handed off" if ticket.get("shipped_how") == "handed-off" else "merged"
        )
        where = f" · {ticket['linear_state']}" if external and ticket.get("linear_state") else ""
        rnd = ticket["round"]
        rounds = "1 revise round" if rnd == 1 else f"{rnd} revise rounds"
        when = "" if ago is None else f" · {how} {duration(ago)} ago"
        age = "" if ago is None else _span("tk-ago", ("" if how == "merged" else f"{how} ") + f"{duration(ago)} ago")
        tiles += (
            f'<a class="tk-tile" href="{_e(ticket_page_href(ticket))}" data-ticket="{_e(_key(ticket))}" '
            f'title="{_e(f"{name}{when}{where} · {rounds}")}"><span class="tk-blk" aria-hidden="true"></span>'
            f'{_span("tk-tn", name)}{_span("tk-r", f"r{rnd}")}{age}</a>'
        )
    return f'<div class="tk-tiles">{tiles}</div>'


def _shelf(shelf: dict) -> str:
    if not shelf["tiles"]:
        return '<p class="empty">nothing shipped</p>'
    earlier, again, drawer = shelf["earlier"], shelf["newest"][SHELF_PHONE:], ""
    if again:
        left = len(earlier) - SHELF_PAGE
        page = (
            f'<div class="tk-page"><button class="tk-btn" type="button">show {SHELF_PAGE} more</button>'
            f'{_span("tk-note", f"{left} older not shown · newest first")}</div>'
        ) if left > 0 else ""
        drawer = (
            f'<details class="{"tk-earlier" if earlier else "tk-earlier tk-narrow"}"><summary>'
            f'{_span("tk-wide", f"{len(earlier)} earlier")}{_span("tk-narrow", f"{len(earlier) + len(again)} earlier")}'
            f'</summary><div class="tk-drawer"><div class="tk-narrow">{_tiles(again)}</div>'
            f'{_tiles(earlier[:SHELF_PAGE]) if earlier else ""}{page}</div></details>'
        )
    sub = _e(f"{shelf['merged']} merged · {shelf['handed']} handed off")
    return f'<p class="tk-sub">{sub}</p><div class="tk-shelf">{_tiles(shelf["newest"])}{drawer}</div>'


def _board(snapshot: dict) -> str:
    found = board(snapshot)
    buttons = _button(f"all {found['total']}", "data-project", "", True) + "".join(
        _button(f"{_short(repo)} {n}", "data-project", repo, False) for repo, n in found["projects"]
    )
    views = _button("board", "data-view", "board", True) + _button("table", "data-view", "table", False)
    unavailable = '<p class="empty">statechart unavailable</p>'
    columns = "".join(_column(col, col["phase"] == found["start"]) for col in found["columns"])
    return (
        '<div class="tk-filter" id="ticket-filter"><div class="tk-tools">'
        f'<div class="tk-group" role="group" aria-label="Project">{buttons}</div><span class="tk-grow"></span>'
        f'<div class="tk-group tk-view" role="group" aria-label="View">{views}</div></div>{FIND}</div>'
        + _panel(
            "pipeline", "Pipeline",
            _phases(found["columns"], found["start"]) + f'<div class="tk-board">{columns}</div>'
            if found["available"] else unavailable,
            count=f"{found['tracked']} tracked"
            + (f" · {found['open']} open · {found['shipped']} shipped" if found["available"] else ""),
        )
        + _panel(
            "shipped", "Shipped", _shelf(found["shelf"]) if found["available"] else unavailable,
            count=len(found["shelf"]["tiles"]) if found["available"] else None,
        )
    )


def _tickets(snapshot: dict) -> str:
    rows = snapshot["tickets"]
    if not rows:
        inner = '<p class="empty">no tracked tickets</p>'
    else:
        head = "".join(
            f"<th>{name}</th>"
            for name in ("ticket", "tracker", "state", "round", "gates", "stuck")
        )
        body = "".join(
            "<tr>"
            + _td(_ticket(row, full=True))
            + _td(row["tracker"])
            + _td(row["state"], "bad" if row["stuck"] else None)
            + _td(row["round"], "num")
            + _td(row["gate_count"], "num")
            + _td("yes" if row["stuck"] else "no", "bad" if row["stuck"] else None)
            + "</tr>"
            for row in rows
        )
        inner = f'<div class="tk-scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'
    return _panel("tickets", "Tickets", inner, count=f"{len(rows)} tracked", hidden=True)
