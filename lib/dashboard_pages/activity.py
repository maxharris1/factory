from __future__ import annotations

import json
import re

from lib.dashboard_pages.common import (
    _BOTS, _JOB_STATE, _chip, _e, github_links, github_refs, _panel, _span, _thread_key, _ticket, duration,
    ticket_page_href,
)
from lib.max_actions import ACTIONS, Why
from lib.dashboard_static import read_asset


CSS = read_asset("css/activity.css")


_SCRIPT = read_asset("js/activity.js")


# --- activity page -----------------------------------------------------------------


_ATTENTION = {
    "stuck": "bad", "overdue": "bad", "dead": "bad", "needs-attention": "bad",
    "stale": "warn", "paused": "warn", "down": "bad", "no-waiter": "bad", "health": "bad",
}


_VERDICT = {"routed": "accent", "shipped": "ok", "stale": "muted", "rejected": "warn",
            "stuck": "bad", "blocked": "bad", "waiting": "warn", "unstuck": "accent", "other": "muted"}


_TONE_TINT = {"warn": "#ff9e64", "bad": "#f7768e", "muted": "#8b93b8"}
_RELAY_FILTERS = (("all", "all"), ("needs", "needs me"), ("shipped", "shipped"), ("wakes", "grok wakes"))
RELAY_WINDOW = 6
RELAY_PHONE_WINDOW = 4
_NEEDS_ME = {"waiting", "stopped", "stale"}
_RELAY_NOTE = "The relay shows the 12 tickets that moved most recently. A stopped ticket stays listed until it moves again."


_HEALTH_LINK = re.compile(r"health\.html#[A-Za-z0-9_-]+")


def _health_link(href: object) -> str:
    """Only a link into the Health tab (#92); anything else is the tab itself."""
    return str(href) if isinstance(href, str) and _HEALTH_LINK.fullmatch(href) else "health.html"


def _place_untimed(ordered: list[dict], verdicts: list[dict]) -> None:
    for index, step in enumerate(verdicts):
        if step["at"] is not None:
            continue
        previous = verdicts[index - 1] if index else None
        at = next(i for i, s in enumerate(ordered) if s is previous) + 1 if previous else 0
        ordered.insert(at, step)


_NOW_TITLE = ' title="where the ticket is now"'


def _bot_class(name: object) -> str:
    return f"rl-{name if name in _BOTS else 'other'}"


def _now_job(row: dict, strip: str) -> str:
    return (
        _span("rl-bot", row["recipient"]) + _span("rl-what", row["action"])
        + f'<span class="rl-px" aria-hidden="true">{strip}</span>'
    )


def _lasting(cls: str, lead: str, age: object) -> str:
    if age is None:
        return _span(cls, lead) if lead else ""
    return _span(cls, (f"{lead} " if lead else "") + "for " + duration(age))


def _now_running(at: dict) -> str:
    row = at["running"]
    px = _px()
    strip = px.px_strip(f"relay-now-{row['job']}", state=px.px_state(row), bot=row["recipient"], cell=2, height=10)
    return (
        f'<span class="rl-now {_bot_class(row["recipient"])} live"{_NOW_TITLE}>{_now_job(row, strip)}'
        f'{_span("rl-run", "running " + duration(row["elapsed_seconds"]))}</span>'
    )


def _now_stale(at: dict) -> str:
    row = at["running"]
    strip = _px().px_strip(f"relay-now-{row['job']}", state="stale", bot=row["recipient"], still=True, cell=2,
                           height=10)
    cause = _JOB_STATE.get(str(row.get("stale_cause")))
    return (
        f'<span class="rl-now stale"{_NOW_TITLE}><span class="rl-blk {_bot_class(row["recipient"])}">'
        f'{_now_job(row, strip)}</span>'
        + _chip(cause[0] if cause else row["job_state"], "warn" if row["job_state"] == "stale" else "bad")
        + _span("rl-late", "no heartbeat for " + duration(row["heartbeat_age_seconds"]))
        + "</span>"
    )


def _now_queued(at: dict) -> str:
    row = at["queued"]
    return (
        f'<span class="rl-now queued {_bot_class(row["recipient"])}"{_NOW_TITLE}>'
        f'{_span("rl-bot", row["recipient"])}{_span("rl-what", row["action"])}'
        f'{_span("rl-wait", "queued " + duration(row["age_seconds"]))}</span>'
    )


def _now_stopped(at: dict) -> str:
    now = at["now"]
    lead = f"in {now['stopped_in']}" if now["stopped_in"] else ""
    return f'<span class="rl-now"{_NOW_TITLE}>{_chip(now["state"], "bad")}{_lasting("rl-say", lead, now["age_seconds"])}</span>'


def _now_shipped(at: dict) -> str:
    now = at["now"]
    age = now["age_seconds"]
    return (
        f'<span class="rl-now"{_NOW_TITLE}>{_chip("shipped", "ok")}'
        + _span("rl-out", "handed off" if now["how"] == "handed-off" else "merged")
        + ("" if age is None else _span("rl-when", duration(age) + " ago"))
        + "</span>"
    )


def _now_waiting(at: dict) -> str:
    now = at["now"]
    return (
        f'<span class="rl-now"{_NOW_TITLE}>{_chip("waiting", "warn")}'
        + _lasting("rl-say", f"on {now['waiting_on']}", now["age_seconds"]) + "</span>"
    )


def _now_idle(at: dict) -> str:
    now = at["now"]
    return f'<span class="rl-now"{_NOW_TITLE}>' + _lasting("rl-when", f"in {now['state']}", now["age_seconds"]) + "</span>"


def _live_state(at: dict) -> object:
    return at["running"]["job_state"] if at["running"] else None


def _rests(at: dict) -> object:
    return at["now"]["kind"] if at["now"] else None


_ENDINGS = (
    ("running", lambda at: _live_state(at) == "running", _now_running),
    ("stale", lambda at: _live_state(at) in ("stale", "dead"), _now_stale),
    ("queued", lambda at: at["queued"] is not None, _now_queued),
    ("stopped", lambda at: _rests(at) == "stopped", _now_stopped),
    ("shipped", lambda at: _rests(at) == "shipped" and not at["said"], _now_shipped),
    ("waiting", lambda at: _rests(at) == "idle" and bool(at["now"]["waiting_on"]), _now_waiting),
    ("idle", lambda at: _rests(at) == "idle", _now_idle),
)


def _live_rows(snapshot: dict) -> tuple[dict[str, dict], dict[str, dict]]:
    running: dict[str, dict] = {}
    for row in snapshot["running"]:
        key = _thread_key(row)
        if key not in running or (row.get("elapsed_seconds") or 0) < (running[key].get("elapsed_seconds") or 0):
            running[key] = row
    queued: dict[str, dict] = {}
    for row in snapshot["queued"]:
        queued.setdefault(_thread_key(row), row)
    return running, queued


def relay_threads(snapshot: dict) -> list[dict]:
    running, queued = _live_rows(snapshot)
    found = []
    for item in snapshot["relay"]["threads"]:
        handoffs = [{"type": "handoff", "row": row, "at": row["age_seconds"], "tie": -index}
                    for index, row in enumerate(item["handoffs"])]
        verdicts = sorted(({"type": "verdict", "row": row, "at": row.get("age_seconds"), "tie": row["seq"]}
                           for row in item["verdicts"]), key=lambda s: s["row"]["seq"])
        ordered = sorted(
            (step for step in [*handoffs, *verdicts] if step["at"] is not None),
            key=lambda step: (-step["at"], step["type"] == "verdict", step["tie"]),
        )
        _place_untimed(ordered, verdicts)
        kinds = {(step["type"], step["row"]["kind"]) for step in ordered}
        key = _thread_key(item)
        at = {"running": running.get(key), "queued": queued.get(key), "now": item.get("now"),
              "said": bool(ordered) and (ordered[-1]["type"], ordered[-1]["row"]["kind"]) == ("verdict", "shipped")}
        name, ending = next(((name, draw) for name, when, draw in _ENDINGS if when(at)), (None, None))
        found.append({
            "key": key, "ref": item, "steps": ordered, "at": at, "ending": ending,
            "needs": name in _NEEDS_ME,
            "wakes": ("handoff", "bot_to_bot") in kinds,
            "shipped": name == "shipped" or ("verdict", "shipped") in kinds,
        })
    return found


def _relay_step(step: dict) -> str:
    row = step["row"]
    if step["type"] == "verdict":
        age = row.get("age_seconds")
        chip = _chip(row["chip"], _VERDICT.get(row["chip"], "muted")) if row["chip"] else _chip(row["kind"])
        return (
            f'<span class="rl-verdict" title="{_e(row["outcome"])}">{chip}'
            f'{_span("rl-out", row["words"] or row["outcome"])}'
            + ("" if age is None else _span("rl-when", duration(age) + " ago"))
            + "</span>"
        )
    bot = row["recipient"] if row["recipient"] in _BOTS else "other"
    b2b = " b2b" if row["kind"] == "bot_to_bot" else ""
    return (
        f'<span class="rl-step rl-{bot}{b2b}">{_span("rl-bot", row["recipient"])}{_span("rl-what", row["action"])}'
        f'{_span("rl-when", duration(row["age_seconds"]) + " ago")}</span>'
        + (_chip("failed", "bad") if row["kind"] == "failed" else "")
    )


_SEP = '<span class="rl-sep" aria-hidden="true">→</span>'


def _items(thread: dict) -> list[str]:
    ending = [thread["ending"](thread["at"])] if thread["ending"] else []
    return [*(_relay_step(step) for step in thread["steps"]), *ending]


def _more(cls: str, key: str, hidden: int) -> str:
    if hidden <= 0:
        return ""
    return (
        f'<button class="{cls}" type="button" aria-expanded="false" data-more="{_e(key)}" '
        f'title="show {hidden} earlier step{"" if hidden == 1 else "s"}">+{hidden} earlier</button>'
    )


def _windowed(key: str, items: list[str]) -> str:
    hidden = max(len(items) - RELAY_WINDOW, 0)
    phone = max(len(items) - RELAY_PHONE_WINDOW, 0)
    early = "".join(f'<span class="rl-early">{item}{_SEP}</span>' for item in items[:hidden])
    early_phone = "".join(f'<span class="rl-early-phone">{item}{_SEP}</span>' for item in items[hidden:phone])
    return _more("rl-more", key, hidden) + early + early_phone + _SEP.join(items[phone:])


def _relay_row(thread: dict) -> str:
    items = _items(thread)
    href = ticket_page_href(thread["ref"])
    return (
        f'<div class="rl-row" data-thread="{_e(thread["key"])}"><span class="rl-id">'
        f'<a class="rl-tk" href="{_e(href)}">{_e(_ticket(thread["ref"]))}</a>'
        f'{_more("rl-more rl-more-phone", thread["key"], len(items) - RELAY_PHONE_WINDOW)}</span>'
        f'<span class="rl-steps">{_windowed(thread["key"], items)}</span></div>'
    )


def _relay(snapshot: dict) -> str:
    threads = relay_threads(snapshot)
    buttons = "".join(
        f'<button class="rl-btn" type="button" aria-pressed="{"true" if key == "all" else "false"}" '
        f'data-relay="{key}">{text}</button>'
        for key, text in _RELAY_FILTERS
    )
    rows = "".join(_relay_row(t) for t in threads)
    body = f'<div class="rl-rows">{rows}</div>' if rows else '<p class="empty">no handoffs or verdicts yet</p>'
    legend = (
        '<div class="rl-legend">'
        '<span class="rl-key"><span class="rl-sw" aria-hidden="true"></span><span>handoff</span></span>'
        '<span class="rl-key"><span class="rl-sw b2b" aria-hidden="true"></span><span>bot-to-bot (grok wake)</span></span>'
        '<span class="rl-key"><span class="chip accent" aria-hidden="true">routed</span><span>verdict</span></span>'
        '<span class="rl-key"><span class="chip bad" aria-hidden="true">failed</span><span>failed handoff</span></span>'
        '<span class="rl-key"><span class="rl-sw now" aria-hidden="true"></span><span>where the ticket is now</span></span>'
        f'<a class="rl-find" href="job.html">find any of the {_e(snapshot["relay"]["tracked"])} tickets</a></div>'
        f'<p class="rl-note">{_RELAY_NOTE}</p>'
    )
    return _panel(
        "relay", "Relay",
        f'<div class="rl-filter" role="group" aria-label="Filter the relay">{buttons}</div>{body}{legend}',
        count=f'{len(threads)} of {snapshot["relay"]["tracked"]} tickets · newest activity first',
    )


def _px():
    from lib.dashboard_pages import shell

    return shell


def _attention_key(row: dict) -> str:
    return f"{row['kind']}:{row.get('component') or str(row['repo']) + '#' + str(row['ticket_id'])}"


_WHY: dict[Why, str] = {
    "awaiting-ok": "waits for your merge OK",
    "visual-qa-review": "waits for your visual QA review",
    "plan-size": "plan is over the size limit",
    "question": "asks you a question",
    "repeat-stop": "review repeated an earlier finding",
    "unsat": "review calls a criterion unsatisfiable",
    "capped": "hit the review round cap",
    "blocked": "blocked",
    "stuck": "stuck",
    "waiting-external": "waits on someone outside the factory",
}


def _comment_href(row: dict) -> str | None:
    """The comment that explains the wait (commentHref in the script)."""
    refs = github_refs(row)
    comment_id = str(row["wait"].get("comment_id"))
    if refs is None or not re.fullmatch(r"[0-9]+", comment_id):
        return None
    return f"https://github.com/{refs['repo']}/issues/{refs['pr'] or refs['issue']}#issuecomment-{comment_id}"


def _wait(row: dict) -> str:
    wait = row["wait"]
    href = _comment_href(row)
    link = "" if href is None else (
        f'<a href="{_e(href)}" rel="noopener noreferrer" target="_blank">comment ↗</a>'
    )
    text = _WHY.get(wait["why"], wait["why"]) + (f": {wait['detail']}" if wait.get("detail") else "")
    return f'<span class="at-why">{_span(None, text)}{link}</span>'


def _attention(snapshot: dict) -> str:
    attention = snapshot["attention"]

    def draw(row: dict) -> str:
        component = row.get("component")
        key = _attention_key(row)
        tone = _ATTENTION.get(row["kind"], "muted")
        strip = _px().px_strip(f"attention-{key}", effect="static", tint=_TONE_TINT[tone], cell=4, height=24)
        href = _health_link(row.get("link")) if component else ticket_page_href(row)
        return (
            f'<div class="at-row" data-attn="{_e(key)}"><span class="at-px" aria-hidden="true">{strip}</span>'
            + _chip(row["kind"], tone)
            + _span("at-subj", component or _ticket(row))
            + _span("at-what", row["detail"])
            + (_wait(row) if row.get("wait") else "")
            + f'<a class="at-go" href="{_e(href)}">{"open in System" if component else "open ticket"}</a>'
            + ("" if component else github_links(row))
            + "</div>"
        )

    items = sorted(attention["items"], key=lambda row: _px()._tone_rank(row["kind"]))
    count = attention["count"]
    inner = f'<div class="at-rows">{"".join(draw(row) for row in items)}</div>' if items else (
        '<p class="empty">all clear</p>'
    )
    label = f"{count} item" + ("" if count == 1 else "s") + (" · worst first" if items else "")
    return _panel("attention", "Needs attention", inner, count=label)


SCRIPT = _SCRIPT.replace("__ACT__", json.dumps(
    {"label": {name: spec.label for name, spec in ACTIONS.items()}, "why": _WHY,
     "head": [name for name, spec in ACTIONS.items() if spec.head_bound]},
))
