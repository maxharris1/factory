"""The shell every page shares (#139): TopBar, header band, attention strip, status line,
the panels per page and the page frame; and PixelStrip (#138, docs/design/mission-control/SPEC.md
section 6): px_strip renders a strip canvas from the PX_STATE table and PX_SCRIPT draws every
strip on the page under one animation loop."""

from __future__ import annotations

import json
import math
import re
from decimal import Decimal, ROUND_HALF_UP
from typing import NamedTuple

from lib.dashboard_telemetry import TELEMETRY_CSS
from lib.health import CODE_COMPONENTS
from lib.pr_title import label
from lib.dashboard_pages.activity import _ATTENTION, _attention, _attention_key, _health_link, _relay
from lib.dashboard_pages.common import _chip, duration, _e, _span, _ticket, ticket_page_href
from lib.dashboard_pages.efficiency import _jobs_lifetime, balance_row, headroom_row, middle_row, top_row
from lib.dashboard_pages.evals import _evals
from lib.dashboard_pages.lanes import _just_happened, lane_devices, _lanes, _needs_max_and_intake
from lib.dashboard_pages.routing import _routing
from lib.dashboard_pages.settings import _accounts, _settings
from lib.dashboard_pages.system import (
    _BUDGET_STATE, _broken, _components, _log_row, _no_waiter, _POLL_STATE, _rules, _versions,
    version_drift,
)
from lib.dashboard_pages.ticket_detail import _ticketview
from lib.dashboard_pages.tickets import _board, _tickets
from lib.dashboard_static import read_asset


CSS = read_asset("css/shell.css")


CSS_NAV = read_asset("css/shell_nav.css")


CSS_PX = read_asset("css/shell_px.css")


SCRIPT = read_asset("js/shell.js")


# The PixelStrip engine (#138, docs/design/mission-control/SPEC.md section 6):
# the drawing methods of reference/artboards/PixelStrip.dc.html, copied onto
# Strip.prototype, under one shared requestAnimationFrame loop. These lines
# differ from the artboard: decay's amount follows the strip's progress
# (heartbeat age / timeout) instead of a timer, and decay is therefore still;
# a caret with rate 0 is still too (#139: the lost link is static), and a
# still caret is always lit, so a 0.4 Hz caret never freezes dark.
PX_SCRIPT = read_asset("js/pixelstrip.js")


def px_script() -> str:
    return PX_SCRIPT.strip().replace("__PX__", json.dumps(px_table(), separators=(",", ":")))


# --- server-side rendering ----------------------------------------------------------
#
# Each page's panels are rendered on the server with the same markup the script
# builds, so a page reads the same without script (it then reloads every
# refresh_seconds) and the script only has to replace their contents.

# In nav order (#78).
NAV_PAGES = ("index.html", "job.html", "activity.html", "efficiency.html", "health.html")


PAGES = (*NAV_PAGES, "evals.html", "settings.html", "ticket.html")


_NAV_PARENT = {"evals.html": "efficiency.html", "settings.html": "health.html", "ticket.html": "job.html"}


_PAGE_NAMES = {
    "index.html": ("main", "Lanes"),
    "job.html": ("job", "Tickets"),
    "activity.html": ("activity", "Activity"),
    "efficiency.html": ("efficiency", "Efficiency"),
    "evals.html": ("evals", "Evals"),
    "health.html": ("health", "System"),
    "settings.html": ("settings", "Settings"),
    "ticket.html": ("ticket", "Ticket"),
}


# --- PixelStrip (#138, SPEC section 6) ----------------------------------------------
#
# One table maps a state to its strip effect and tint; the page script gets
# the same table (px_table) so the server and the script cannot drift. An
# effect is animated when it changes from frame to frame.
PX_EFFECTS = {
    "field": True,
    "fill": True,
    "scan": True,
    "patch": True,
    "spin3": True,
    "decay": False,
    "static": False,
    "twinkle": True,
    "conveyor": True,
    "meter": True,
    "caret": True,
    "off": False,
}


PX_ONESHOTS = {"beat": 600, "burst": 450, "done": 600}


PX_BOT_TINTS = {"claude": "#e0af68", "codex": "#7aa2f7", "cursor": "#bb9af7", "grok": "#73daca"}


PX_FALLBACK = "#9ece6a"


_PX_BAD = "#f7768e"


PX_BROKEN = ("dead", "overdue", "stuck", "blocked", "capped", "refused", "no_waiter", "down")


_PX_LINK = {"live": ("#9ece6a", 1), "stale": ("#ff9e64", 0.4), "lost": (_PX_BAD, 0), "boot": ("#8b93b8", 0),
            "paused": ("#8b93b8", 0)}


PX_STATE: dict[str, dict[str, str | float]] = {
    "plan": {"effect": "field", "tint": "bot"},
    "build": {"effect": "fill", "tint": "bot"},
    "review": {"effect": "scan", "tint": "bot"},
    "fix": {"effect": "patch", "tint": "bot"},
    "clarify": {"effect": "field", "tint": "bot"},
    "update-from-main": {"effect": "patch", "tint": "bot"},
    "claimed": {"effect": "spin3", "tint": "bot"},
    "stale": {"effect": "decay", "tint": "bot"},
    **{name: {"effect": "static", "tint": _PX_BAD} for name in PX_BROKEN},
    "idle": {"effect": "twinkle", "tint": PX_FALLBACK},
    "held": {"effect": "off", "tint": PX_FALLBACK, "frame": "px-held"},
    "waiting": {"effect": "static", "tint": "#ff9e64"},
    "unknown": {"effect": "off", "tint": PX_FALLBACK},
    # #139: the brand caret per link state (rate 1 blinks at 1 Hz, 0 is solid).
    **{f"link-{name}": {"effect": "caret", "tint": tint, "rate": rate} for name, (tint, rate) in _PX_LINK.items()},
    "band": {"effect": "field", "tint": PX_FALLBACK, "seed": 21},
    "attention": {"effect": "static", "tint": _PX_BAD, "seed": 5},
}


PX_ACTIONS = {name: name for name in ("plan", "build", "review", "fix", "clarify", "update-from-main")}


_PX_HEX = re.compile(r"#[0-9a-f]{6}")


def px_table() -> dict:
    return {
        "effects": PX_EFFECTS,
        "oneshots": PX_ONESHOTS,
        "states": PX_STATE,
        "actions": PX_ACTIONS,
        "bots": PX_BOT_TINTS,
        "fallback": PX_FALLBACK,
    }


def px_state(row: dict) -> str:
    """The PX_STATE key for a running job row of the snapshot. Broken rows win
    over the action, and anything the table does not know is "unknown" (still)."""
    job_state = row.get("job_state")
    if job_state == "dead":
        return "dead"
    if row.get("overdue"):
        return "overdue"
    if row.get("stuck"):
        return "stuck"
    if job_state in ("stale", "claimed"):
        return str(job_state)
    if job_state == "running" and row.get("action") in PX_ACTIONS:
        return PX_ACTIONS[row["action"]]
    return "unknown"


def _px_progress(state: str, row: dict) -> float | None:
    timeout = row.get("timeout_seconds") or 1
    if state == "build":
        return (row.get("elapsed_seconds") or 0) / timeout
    if state == "stale":
        return (row.get("heartbeat_age_seconds") or 0) / timeout
    return None


def px_frame(state: str) -> str:
    """The class for the box around a strip in this state ("px-held": the
    marching dashed border), or ""."""
    return PX_STATE.get(state, {}).get("frame", "")


def _fixed3(value: float) -> str:
    """0..1 to three places, rounded half up like the script's toFixed(3)."""
    return str(Decimal(max(0.0, min(1.0, float(value)))).quantize(Decimal("0.001"), ROUND_HALF_UP))


def px_strip(
    key: object,
    *,
    state: str = "unknown",
    effect: str | None = None,
    tint: str | None = None,
    bot: object = None,
    progress: float | None = None,
    heartbeat: float | None = None,
    load: float | None = None,
    rate: float | None = None,
    seed: float | None = None,
    speed: float | None = None,
    used: int | None = None,
    slots: int | None = None,
    burst: int | None = None,
    still: bool = False,
    cell: int | None = None,
    done: bool = False,
    height: int = 16,
) -> str:
    """A PixelStrip canvas (the script draws it; without script its CSS
    gradients are the still frame). `state` picks the effect, tint, rate and
    seed from PX_STATE; the keywords override them. Same markup as PX.strip."""
    entry = PX_STATE.get(state, PX_STATE["unknown"])
    effect = str(effect or entry["effect"])
    tint = tint or entry["tint"]
    rate = entry.get("rate") if rate is None else rate
    seed = entry.get("seed") if seed is None else seed
    if tint == "bot":
        tint = str(bot) if bot in PX_BOT_TINTS else PX_FALLBACK
    if tint not in PX_BOT_TINTS and not _PX_HEX.fullmatch(str(tint)):
        tint = PX_FALLBACK
    height = max(2, min(96, round(height)))
    attrs = [
        ("class", "px"),
        ("data-px", effect if effect in PX_EFFECTS else "off"),
        ("data-tint", tint),
    ]
    style = f"--px:{f'var(--{tint})' if tint in PX_BOT_TINTS else tint};"
    if progress is not None:
        value = _fixed3(progress)
        attrs.append(("data-progress", value))
        style += f"--p:{value};"
    if heartbeat is not None:
        attrs.append(("data-hb", str(max(0, int(heartbeat)))))
    if load is not None:
        value = _fixed3(load)
        attrs.append(("data-load", value))
        style += f"--l:{value};"
    if rate is not None:
        attrs.append(("data-rate", f"{float(rate):g}"))
    if seed is not None:
        attrs.append(("data-seed", f"{float(seed):g}"))
    for name, value in (("speed", speed), ("used", used), ("slots", slots), ("burst", burst)):
        if value is not None:
            attrs.append((f"data-{name}", f"{float(value):g}"))
    if still:
        attrs.append(("data-still", "true"))
    if cell is not None:
        attrs.append(("data-cell", str(max(2, min(16, int(cell))))))
    if done:
        attrs.append(("data-done", "true"))
    attrs += [("data-px-key", str(key)), ("aria-hidden", "true"), ("style", f"{style}height:{height}px")]
    return "<canvas " + " ".join(f'{name}="{_e(value)}"' for name, value in attrs) + "></canvas>"


def px_job_strip(row: dict, height: int = 16) -> str:
    """The strip of a running job row, keyed by its job id (PX.jobStrip)."""
    state = px_state(row)
    return px_strip(
        f"job-{row.get('job')}",
        state=state,
        bot=row.get("recipient"),
        progress=_px_progress(state, row),
        heartbeat=None if state == "claimed" else row.get("heartbeat_age_seconds"),
        height=height,
    )


# --- the shell on every page (#139) -----------------------------------------------------
#
# The TopBar (brand and its caret, nav, link state, snapshot meta), the header
# band, the attention strip and the status line. Each has a script twin in
# SCRIPT that builds the same markup from status.json.


def _topbar(snapshot: dict, page: str) -> str:
    current = ' aria-current="page"'
    home = _NAV_PARENT.get(page, page)
    links = "".join(
        f'<a href="{name}"{current if name == home else ""}>{_PAGE_NAMES[name][1]}</a>' for name in NAV_PAGES
    )
    return (
        '<header class="topbar" id="topbar"><div class="top-row">'
        '<div class="brand"><span>Factory <b>Mission Control</b></span>'
        f'<span class="caret" id="caret" aria-hidden="true">{px_strip("caret", state="link-boot", height=12)}'
        "</span></div>"
        f'<nav class="nav" id="nav" aria-label="Pages">{links}</nav>'
        '<div class="link boot" id="link" role="status" aria-live="polite">'
        '<span class="dot" aria-hidden="true"></span>'
        '<span id="link-text">STATIC SNAPSHOT</span></div>'
        f'<div class="meta">snapshot <span id="age">{_e(snapshot["generated_at"])}</span>'
        ' · <span id="clock"></span> · read-only, redacted</div>'
        "</div>"
        f'<div class="band" id="band">{_band(snapshot)}</div>'
        '<div class="fresh-bar" id="fresh-bar" hidden><span class="fresh-blk" aria-hidden="true"></span>'
        '<b id="fresh-head" aria-hidden="true"></b><span id="fresh-text" aria-hidden="true"></span>'
        '<span class="vh" id="fresh-say" role="alert"></span></div>'
        f"{_attention_strip(snapshot, page)}"
        "</header>"
    )


def header_load(snapshot: dict) -> float:
    """Running / total slots over the enabled, non-placeholder devices that
    have a slot_max (the header band's brightness), 0 with no slots."""
    devices, _ = lane_devices(snapshot)
    used = total = 0
    for device in devices:
        if device["enabled"] and not device["placeholder"] and device["slot_max"]:
            used += device["used"]
            total += device["slot_max"]
    return max(0.0, min(1.0, used / total)) if total else 0.0


def _band(snapshot: dict) -> str:
    return px_strip("band", state="band", load=header_load(snapshot), height=24)


_TONE_RANK = {"bad": 0, "warn": 1}


class Tally(NamedTuple):
    kind: str
    n: int
    tone: str


def _tone_rank(kind: str) -> int:
    return _TONE_RANK.get(_ATTENTION.get(kind), 2)


def strip_model(items: list[dict]) -> dict:
    count = len(items)
    need = " needs attention" if count == 1 else " need attention"
    counts: dict[str, int] = {}
    for row in items:
        counts[row["kind"]] = counts.get(row["kind"], 0) + 1
    tally = sorted(
        (Tally(kind, n, _ATTENTION.get(kind, "muted")) for kind, n in counts.items()),
        key=lambda entry: (_tone_rank(entry.kind), -entry.n),
    )

    def worse(pair: tuple[int, dict]) -> tuple:
        index, row = pair
        age = row.get("age_seconds")
        aged = isinstance(age, (int, float)) and not isinstance(age, bool) and math.isfinite(age)
        return (_tone_rank(row["kind"]), 0 if aged else 1, -age if aged else 0, index)

    worst = [row for _, row in sorted(enumerate(items), key=worse)[:2]]
    largest = sorted(tally, key=lambda entry: -entry.n)[:2]
    rest = count - sum(t.n for t in largest)
    return {
        "count": count,
        "need": need,
        "tally": tally,
        "worst": worst,
        "more": count - len(worst),
        "short": " · ".join([f"{t.n} {t.kind}" for t in largest] + ([f"+{rest}"] if rest else [])),
        "label": f"{count}{need}: " + ", ".join(f"{t.n} {t.kind}" for t in tally) + ". Open Activity.",
    }


def _attention_strip(snapshot: dict, page: str) -> str:
    items = (snapshot.get("attention") or {}).get("items") or []
    root = '<div class="attn" id="attention-strip" role="region" aria-label="Needs attention"'
    if not items:
        return f"{root} hidden></div>"
    model = strip_model(items)

    def blk(key: str) -> str:
        return (f'<span class="attn-blk" aria-hidden="true">'
                f'{px_strip(key, state="attention", height=16)}</span>')

    def count(href: str) -> str:
        return (f'<a class="attn-count" href="{href}">'
                f'<b id="attention-count">{model["count"]}</b>{_span(None, model["need"])}</a>')

    if page == "activity.html":
        return (f'{root}><div class="attn-here">{blk("attention")}{count("#attention")}'
                f'{_span("attn-below", "listed below")}</div></div>')
    def item(row: dict) -> str:
        if row.get("component"):
            href, who = _health_link(row.get("link")), row["component"]
        else:
            href, who = ticket_page_href(row), _ticket(row)
        return (
            f'<div class="attn-item" data-attn="{_e(_attention_key(row))}">'
            + _chip(row["kind"], _ATTENTION.get(row["kind"], "muted"))
            + f'<a class="attn-who" href="{_e(href)}">{_e(who)}</a>'
            + _span("attn-what", row["detail"])
            + "</div>"
        )

    tally = '<span aria-hidden="true">·</span>'.join(
        _span(f"attn-n {tone}", f"{n} {kind}") for kind, n, tone in model["tally"])
    more = (f'<a class="attn-more" href="activity.html#attention">+{model["more"]} more →</a>'
            if model["more"] else "")
    return (
        f'{root}><div class="attn-row"><div class="attn-head">{blk("attention")}'
        f'{count("activity.html#attention")}</div>'
        f'<div class="attn-tally">{tally}</div><span class="attn-rule" aria-hidden="true"></span>'
        + "".join(item(row) for row in model["worst"])
        + f'{more}</div>'
        f'<a class="attn-phone" href="activity.html#attention" aria-label="{_e(model["label"])}">'
        f'{blk("attention-phone")}<b>{_e(str(model["count"]) + model["need"])}</b>'
        f'{_span("attn-short", model["short"])}<span class="attn-go" aria-hidden="true">→</span></a></div>'
    )


# Where each status-line segment links (System anchors or Lanes).
STATUS_LINKS = {
    "poll": "health.html#health",
    "poll-errors": "health.html#health",
    "linear": "health.html#health",
    "webhook": "index.html#linear-webhook",
    "feeder": "index.html#feeder",
    "components": "health.html#components",
    "version": "health.html#drift",
    "ok-count": "health.html#health",
}


# A webhook or feeder segment with no block of its own links to the panel that would hold it.
STATUS_ABSENT_LINK = "index.html#intake"


def shell_table() -> dict:
    """What the page script's shell needs from here (SHELL in SCRIPT)."""
    return {"links": STATUS_LINKS, "absent": STATUS_ABSENT_LINK, "code": sorted(CODE_COMPONENTS)}


def _segment(key: str, text: str, state: str | None = None, label: str | None = None,
             age: object = None, prefix: str | None = None, href: str | None = None) -> dict:
    return {"key": key, "label": label, "text": text, "state": state,
            "href": href or STATUS_LINKS.get(key), "age": age, "prefix": prefix}


def _count(value: object) -> str:
    return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else str(value)


def status_segments(snapshot: dict) -> list[dict]:
    """The status line, one segment per figure, each computed from the
    snapshot: devices, their slots summed and queue (plain), then the vitals
    with a state (ok, warn, bad, muted) and a link, then the count of ok
    vitals. Mirrors statusSegments in SCRIPT."""
    out = []
    devices, _ = lane_devices(snapshot)
    for device in devices:
        if not device["enabled"] or device["placeholder"]:
            value = "off"
        else:
            value = f'{device["used"]}/{"—" if device["slot_max"] is None else device["slot_max"]}'
        out.append(_segment(f'device:{device["name"]}', value, label=str(device["name"])))
    live = [device for device in devices if device["enabled"] and not device["placeholder"]]
    caps = [device["slot_max"] for device in live]
    total = sum(caps) if caps and None not in caps else "—"
    out.append(_segment("slots", f'{sum(device["used"] for device in live)}/{total}', label="slots"))
    out.append(_segment("queue", _count(snapshot["queue"]["totals"]["queued"]), label="queue"))
    poller = snapshot["poller"]
    health = poller.get("health") or {"state": "unknown"}
    label, cls = _POLL_STATE.get(health["state"], _POLL_STATE["unknown"])
    poll = f"poll {label.lower()}"
    cycle = health.get("last_cycle_age_seconds")
    if cycle is None:
        out.append(_segment("poll", poll, cls))
    else:
        out.append(_segment("poll", f"{poll} · {duration(cycle)}", cls, age=cycle, prefix=f"{poll} · "))
    errors = (poller.get("errors") or {}).get("last_hour") or 0
    plural = "" if errors == 1 else "s"
    out.append(_segment("poll-errors", f"{_count(errors)} poll error{plural} / 1 h", "bad" if errors else "ok"))
    budget = poller.get("linear_budget") or {"state": "ok"}
    blabel, bcls = _BUDGET_STATE.get(budget["state"], ("OTHER", "muted"))
    out.append(_segment("linear", f"linear {blabel.lower()}", bcls))
    webhook = poller.get("linear_webhook")
    if not webhook:
        out.append(_segment("webhook", "no webhook", "muted", href=STATUS_ABSENT_LINK))
    elif webhook.get("last_received_age_seconds") is None:
        out.append(_segment("webhook", "webhook never",
                            "bad" if webhook.get("warning") is not None else "warn"))
    else:
        age = webhook["last_received_age_seconds"]
        out.append(_segment("webhook", f"webhook {duration(age)}",
                            "bad" if webhook.get("warning") is not None else "ok",
                            age=age, prefix="webhook "))
    feeder = snapshot.get("feeder")
    if not feeder:
        out.append(_segment("feeder", "no feeder", "muted", href=STATUS_ABSENT_LINK))
    else:
        failing = any(item.get("state") == "error" for item in feeder.get("items") or [])
        out.append(_segment("feeder", f"feeder {_count(feeder['fed'])} fed", "bad" if failing else "ok"))
    components = snapshot.get("health") or {}
    if not components.get("known"):
        out.append(_segment("components", "components unknown",
                            "bad" if components.get("error") else "muted"))
    else:
        rows = [row for row in components.get("components") or [] if row.get("configured") is not False]
        up = sum(1 for row in rows if row["state"] == "up")
        down = any(row["state"] == "down" for row in rows)
        out.append(_segment("components", f"{up} / {len(rows)} up",
                            "bad" if down else "warn" if up < len(rows) else "ok"))
    drift = version_drift(components)
    if drift["newest"] == "unknown":
        out.append(_segment("version", "version unknown", "warn"))
    else:
        behind = len(drift["behind"])
        out.append(_segment("version", f'{drift["newest"][:7]} · {behind} behind',
                            "warn" if behind else "ok"))
    ok = sum(1 for seg in out if seg["state"] == "ok")
    if ok:
        out.append(_segment("ok-count", f"{ok} ok", "ok"))
    return out


def _statusline(snapshot: dict) -> str:
    parts = ['<span class="sl-brand">factory</span>']
    filled = False
    for seg in status_segments(snapshot):
        if seg["state"] is None:
            queue = seg["key"] == "queue"
            label = f'<span class="sl-k" data-short="q">{_e(seg["label"])}</span>' if queue else _span("sl-k", seg["label"])
            parts.append(
                f'<span class="{"sl-seg sl-queue" if queue else "sl-seg"}" data-seg="{_e(seg["key"])}">'
                f'{label}{_span("sl-v", seg["text"])}</span>'
            )
            continue
        if not filled:
            parts.append(
                '<span class="sl-fill"></span><span class="sl-age" data-seg="age" hidden>'
                '<span class="blk" aria-hidden="true"></span><span class="sl-t" data-short=""></span></span>'
            )
            filled = True
        parts.append(
            f'<a class="sl-vital {seg["state"]}" href="{_e(seg["href"])}" data-seg="{_e(seg["key"])}">'
            f'<span class="blk" aria-hidden="true"></span>{_span(None, seg["text"])}'
            f'{_span("vh", ", " + seg["state"])}</a>'
        )
    return f'<footer class="statusline" id="statusline" aria-label="Status line">{"".join(parts)}</footer>'


_PANELS = {
    "index.html": (_needs_max_and_intake, _lanes, _just_happened),
    "activity.html": (_attention, _relay),
    "efficiency.html": (top_row, middle_row, balance_row, _jobs_lifetime),
    "evals.html": (_evals,),
    "health.html": (_broken, _components, _versions, _log_row, _settings, headroom_row, _accounts, _routing, _rules, _no_waiter),
    "job.html": (_board, _tickets),
    "settings.html": (_settings, _accounts),
    "ticket.html": (_ticketview,),
}


def _page(snapshot: dict, name: str) -> str:
    page, label = _PAGE_NAMES[name]
    refresh = int(snapshot["refresh_seconds"])
    panels = "".join(draw(snapshot) for draw in _PANELS[name])
    heading = "" if name == "ticket.html" else f'<h1 class="vh">{_e(label)}</h1>'
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="en"><head><meta charset="utf-8">',
            # Without script the page reloads itself; with script it polls
            # status.json and never reloads.
            f'<noscript><meta http-equiv="refresh" content="{refresh}"></noscript>',
            '<meta name="viewport" content="width=device-width,initial-scale=1">',
            '<meta name="robots" content="noindex">',
            '<meta name="color-scheme" content="dark">',
            f"<title>factory mission control · {label.lower()}</title>",
            '<link rel="stylesheet" href="/app.css"></head>',
            f'<body data-page="{page}" '
            f'data-generated="{float(snapshot["generated_at_epoch"])!r}" '
            f'data-refresh="{refresh}">',
            '<a class="skip" id="skip" href="#hud">Skip to content</a>',
            _topbar(snapshot, name),
            f'<main id="hud" tabindex="-1">{heading}{panels}</main>',
            _statusline(snapshot),
            '<script src="/app.js" defer></script>',
            "</body></html>",
        ]
    ) + "\n"
