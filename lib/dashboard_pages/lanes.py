"""index.html (Lanes, #140): the lanes (queue lane, then one row of slot tiles per device) with
live counts in their header, Just happened and Intake. Copied from Lanes.dc.html, SlotTile.dc.html
and the queue studies in Bits.dc.html."""

from __future__ import annotations

import json
import math
from typing import NamedTuple

from lib.dashboard_pages.activity import _health_link
from lib.dashboard_pages.common import (
    _JOB_STATE, _bot, _chip, duration, _e, github_links, _job_label, _panel,
    model_effort, _short, _span, _ticket, _tile_label, _when, ticket_page_href,
)
from lib.dashboard_static import read_asset


CSS = read_asset("css/lanes.css")


# --- tables shared with the script (LANES) -------------------------------------------------

# A job tile per PX state (shell.PX_STATE carries the strip). `verbs` rotate while the job
# works; `text` is the fixed line 2 otherwise; `tone` colours it; `chip` is the state chip
# (none on healthy work); `hb` is the heartbeat bar's rule; `bad` gives the red border.
TILE_STATE: dict[str, dict] = {
    "plan": {"verbs": ["Pondering", "Sketching", "Scheming"], "tone": "live", "hb": "live"},
    "build": {"verbs": ["Assembling", "Stacking", "Forging"], "tone": "live", "hb": "live"},
    "review": {"verbs": ["Scrutinizing", "Combing", "Squinting"], "tone": "live", "hb": "live"},
    "fix": {"verbs": ["Patching", "Mending", "Tinkering"], "tone": "live", "hb": "live"},
    "clarify": {"verbs": ["Clarifying", "Untangling", "Answering"], "tone": "live", "hb": "live"},
    "update-from-main": {"verbs": ["Merging", "Syncing", "Reconciling"], "tone": "live", "hb": "live"},
    "claimed": {"text": "waiting to start", "tone": "", "chip": ["claimed", "warn"], "hb": "none"},
    "stale": {"text": "no heartbeat", "tone": "warn", "chip": ["stale", "warn"], "hb": "warn"},
    "dead": {"text": "runner dead", "tone": "bad", "chip": ["dead", "bad"], "hb": "bad", "bad": True},
    "overdue": {"text": "overdue", "tone": "bad", "hb": "bad", "bad": True},
    "stuck": {"text": "ticket stuck", "tone": "bad", "chip": ["stuck", "bad"], "hb": "bad", "bad": True},
    "unknown": {"text": None, "tone": "", "hb": "live"},
    "ci-running": {"verbs": ["Testing", "Checking", "Linting"], "tone": "live", "hb": "none"},
    "ci-queued": {"verbs": ["Queueing", "Waiting", "Lining up"], "tone": "warn", "hb": "none"},
    "ci-failed": {"text": "CI failed", "tone": "bad", "hb": "none", "bad": True},
}

GLYPH_ORDER = [2, 0, 1, 3]

# Queue speed by wait age, through the Bits rail's anchors (0:40 → 1, 6:10 → 2.5, 22:05 → 5).
QUEUE_SPEED = [[40, 1.0], [370, 2.5], [1325, 5.0]]

QUEUE_TINT = "#8b93b8"
BELT_TINT = "#9ece6a"
_BAD = "#f7768e"

INTAKE_GROUP = {
    "error": "trouble",
    "refused-intake": "trouble",
    "waiting-blocked": "blocked",
    "waiting-capacity": "next",
}
FED_TODAY_SECONDS = 86400

INTAKE = {
    "rows": 4,
    "columns": [
        {"key": "next", "label": "Next in line", "more": "more", "empty": "no ticket is waiting for capacity"},
        {"key": "blocked", "label": "Waiting on a blocker or files",
         "empty": "no ticket is waiting on a blocker or files",
         "groups": 4, "chips": 6, "none": "no blocker named", "count": "{n} waiting",
         "spread": "{n} more, spread over {m} other blockers",
         "spread_one": "{n} more, spread over {m} other blocker"},
        {"key": "fed", "label": "Fed today", "more": "more today", "empty": "no ticket was fed today"},
    ],
}

FUNNEL = [
    {"key": "eligible", "label": "Labelled ready"},
    {"key": "waiting", "label": "Waiting", "parts": [
        {"key": "waiting_blocked", "cls": "fb-blocked", "label": "on a blocker or files"},
        {"key": "waiting_capacity", "cls": "fb-capacity", "label": "for capacity"},
    ]},
    {"key": "waiting_capacity", "label": "Next in line"},
    {"key": "fed_today", "label": "Fed today"},
    {"key": "fed", "label": "Into the factory", "href": "#counts"},
]


def lanes_table() -> dict:
    return {"tiles": TILE_STATE, "glyph": GLYPH_ORDER, "speed": QUEUE_SPEED,
            "queue_tint": QUEUE_TINT, "belt_tint": BELT_TINT,
            "intake_group": INTAKE_GROUP, "fed_today": FED_TODAY_SECONDS, "funnel": FUNNEL,
            "intake": INTAKE}


def queue_speed(age: object) -> float:
    """Conveyor speed for a wait of `age` seconds: linear between the anchors, flat
    before the first, and on past the last at the last segment's slope (the engine caps 6)."""
    a = float(age) if isinstance(age, (int, float)) and not isinstance(age, bool) else 0.0
    pts = QUEUE_SPEED
    if a <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if a <= x1:
            return round(y0 + (y1 - y0) * (a - x0) / (x1 - x0), 3)
    (x0, y0), (x1, y1) = pts[-2], pts[-1]
    return round(min(6.0, y1 + (y1 - y0) * (a - x1) / (x1 - x0)), 3)


def verb_at(state: str, secs: int, seed: int) -> str:
    verbs = TILE_STATE[state]["verbs"]
    return verbs[math.floor((secs + seed * 1.7) / 4) % len(verbs)] + "…"


SCRIPT = read_asset("js/lanes.js")


# --- lanes --------------------------------------------------------------------------------


def lane_devices(snapshot: dict) -> tuple[list[dict], list[dict]]:
    """The lanes the script draws: (devices with their running jobs, grok
    wakes). Mirrors laneDevices in the page script."""
    slots = snapshot["slots"]
    host = slots["box"]["name"]
    devices = [
        {
            "name": row["name"],
            "slot_max": row["slot_max"],
            "held": row.get("held") or 0,
            "enabled": row["enabled"],
            "placeholder": row["placeholder"],
            "harnesses": row["harnesses"],
            "listed": True,
            "jobs": [],
        }
        for row in slots["devices"]
    ]
    by_name = {device["name"]: device for device in devices}
    wakes: list[dict] = []
    for row in snapshot["running"]:
        if row["device"] is None:
            wakes.append(row)
            continue
        if row["device"] not in by_name:
            device = {
                "name": row["device"],
                "slot_max": slots["box"]["slot_max"] if row["device"] == host else None,
                "held": 0,
                "enabled": True,
                "placeholder": False,
                "harnesses": [],
                "listed": row["device"] == host and not devices,
                "jobs": [],
            }
            devices.append(device)
            by_name[row["device"]] = device
        by_name[row["device"]]["jobs"].append(row)
    if not devices:
        devices.append(
            {"name": host, "slot_max": slots["box"]["slot_max"], "held": 0, "enabled": True,
             "placeholder": False, "harnesses": [], "listed": True, "jobs": []}
        )
    for device in devices:
        # #63 FU-1: a slot held in device_slots is in use even when its job
        # is not (or no longer) running, so a lane counts max(held, running).
        device["used"] = max(device["held"], len(device["jobs"]))
    return devices, wakes


def probe_down(snapshot: dict, name: str) -> bool:
    health = snapshot.get("health") or {}
    return any(
        c.get("component") == "device-probe" and c.get("device") == name and c.get("state") == "down"
        for c in health.get("components") or []
    )


def _device_chip(device: dict, down: bool) -> str:
    if not device["enabled"]:
        return _chip("DISABLED", "muted")
    if device["placeholder"]:
        return _chip("PLACEHOLDER", "warn")
    if not device["listed"]:
        return _chip("UNLISTED", "warn")
    if down:
        return _chip("PROBE DOWN", "bad")
    return ""


def _letters(text: str) -> str:
    return "".join(f"<span>{_e(ch)}</span>" for ch in text)


def _tone(cls: str) -> str:
    return f" {cls}" if cls else ""


def _slot(row: dict, seed: int = 1) -> str:
    from lib.dashboard_pages.shell import px_job_strip, px_state

    name = px_state(row)
    spec = TILE_STATE.get(name, TILE_STATE["unknown"])
    who = row["recipient"] if row["recipient"] in ("claude", "codex", "cursor", "grok") else "other"
    model = model_effort(row.get("exposure"))
    title = (
        f"{_ticket(row, full=True)} · {row['action']}"
        + (f" · round {row['round']}" if row.get("round") else "")
        + (f" · {model}" if model else "")
        + f" (job {row['job']})"
    )
    badge = _JOB_STATE.get(str(row.get("stale_cause"))) or (
        TILE_STATE.get(str(row.get("job_state")), {}).get("chip") if name == "overdue" else spec.get("chip")
    )
    line1 = _bot(row["recipient"]) + _span("ticket", _tile_label(row)) + (_chip(badge[0], badge[1]) if badge else "")
    if spec.get("verbs"):
        text = verb_at(name, 0, seed)
        cells = "".join(f'<i class="on"></i>' if q == GLYPH_ORDER[0] else "<i></i>" for q in range(4))
        line2 = (
            f'<span class="glyph" aria-hidden="true">{cells}</span>'
            f'<span class="verb{_tone(spec["tone"])}" role="img" aria-label="{_e(text)}">{_letters(text)}</span>'
        )
    else:
        text = row["action"] if spec["text"] is None else spec["text"]
        line2 = f'<span class="verb{_tone(spec["tone"])}" role="img" aria-label="{_e(text)}">{_letters(str(text))}</span>'
    rnd = str(row.get("round") or "")
    pips = ""
    parts = rnd.split("/")
    if len(parts) == 2 and parts[0].strip().isdigit() and parts[1].strip().isdigit():
        cur, cap = int(parts[0]), min(6, int(parts[1]))
        cells = "".join(
            f'<i class="{"done" if k < cur else "cur"}"></i>' if k <= cur else "<i></i>"
            for k in range(1, cap + 1)
        )
        pips = (
            f'<span class="round" title="round {_e(rnd)}"><span class="pips" aria-hidden="true">{cells}</span>'
            f"{_span(None, rnd)}</span>"
        )
    line3 = (
        ("" if name == "unknown" else _span("action", row["action"])) + pips
        + (_span("model", model) if model else "")
        + f'<span class="t">T+ <b>{duration(row["elapsed_seconds"])}</b></span>'
    )
    hb = spec["hb"]
    level = _tone(hb if hb in ("warn", "bad") else "")
    age = row.get("heartbeat_age_seconds")
    if hb == "none":
        width, hb_text = 0, "hb —"
    elif hb == "live":
        safe = max(0, int(age or 0))
        width, hb_text = max(8, 100 - safe * 6), f"hb {duration(safe)}"
    else:
        width, hb_text = 100, f"hb {duration(age)}"
    line4 = (
        f'<div class="hbar{level}" aria-hidden="true"><i style="width:{width}%"></i></div>'
        f'<span class="hbt{level}">{_e(hb_text)}</span>'
    )
    return (
        f'<a class="tile {who}{" bad" if spec.get("bad") else ""}" href="{_e(ticket_page_href(row))}" '
        f'title="{_e(title)}">{px_job_strip(row, 16)}<div class="tile-body">'
        f'<div class="tile-l1">{line1}</div><div class="tile-l2">{line2}</div>'
        f'<div class="tile-l3">{line3}</div><div class="tile-l4">{line4}</div></div></a>'
    )


def _held_tile(key: str, seed: int) -> str:
    from lib.dashboard_pages.shell import px_strip

    strip = px_strip(key, state="held", seed=seed, height=16)
    return f'<div class="tile empty px-held">{strip}<div class="lbl">held</div></div>'


def _idle_tile(key: str, off: bool, seed: int, height: int = 16) -> str:
    from lib.dashboard_pages.shell import px_strip

    strip = px_strip(key, state="held" if off else "idle", seed=seed, height=height)
    return f'<div class="tile empty free">{strip}<div class="lbl">{"off" if off else "idle"}</div></div>'


def _queue_lane(snapshot: dict) -> str:
    from lib.dashboard_pages.shell import px_strip

    rows = snapshot["queued"]
    oldest = max([row["age_seconds"] or 0 for row in rows], default=0)
    belt = px_strip("queue", effect="conveyor", tint=QUEUE_TINT, seed=20, height=12,
                    speed=queue_speed(oldest), still=not rows)
    if not rows:
        inner = belt + '<div class="sub">queue empty</div>'
    else:
        items = []
        for i, row in enumerate(rows):
            if row.get("no_waiter"):
                strip = px_strip(f"queue-{row['job']}", effect="static", tint=_BAD, seed=41 + i, height=12)
                chip = (
                    f'<a class="chip bad" href="{_e(_health_link(row.get("no_waiter_link")))}">'
                    f'no waiter for {_e(row["no_waiter"])}</a>'
                )
            else:
                strip = px_strip(f"queue-{row['job']}", effect="conveyor", tint=BELT_TINT, seed=41 + i,
                                 height=12, speed=queue_speed(row["age_seconds"]))
                chip = ""
            items.append(
                f'<li><a href="{_e(ticket_page_href(row))}">{_bot(row["recipient"])}'
                f'{_span("ticket", _job_label(row))}{_tier_chip(row)}</a>'
                f'{_span("what", row["action"])}'
                f'{_when(row["age_seconds"], "waiting ")}{chip}'
                f'<span class="belt" aria-hidden="true">{strip}</span></li>'
            )
        inner = belt + f'<ul class="qrail">{"".join(items)}</ul>'
    return (
        '<div class="lane" id="queue" data-device="queue"><div class="lane-head">'
        f'<div class="name">queue</div><div class="sub">{len(rows)} waiting</div></div>'
        f'<div class="lane-body">{inner}</div></div>'
    )


def _qa_lane(snapshot: dict) -> str:
    qa = snapshot.get("qa", {})
    runners, queued, cap = qa.get("runners", []), qa.get("queued", []), qa.get("cap")
    if not any(row["open"] for row in runners) and not queued:
        return ""
    items = []
    for row in runners:
        lanes = f"{row['open']} / {'?' if cap is None else cap} lanes"
        full = _chip("at cap", "warn") if cap is not None and row["open"] >= cap else ""
        items.append(f'<li>{_span("ticket", row["runner"])}{_span("what", lanes)}{full}</li>')
    for row in queued:
        waiting = f"{row['count']} waiting"
        items.append(f'<li>{_span("ticket", row["repo"])}{_span("what", waiting)}</li>')
    return (
        '<div class="lane" data-device="qa"><div class="lane-head"><div class="name">qa</div>'
        f'<div class="sub">{sum(row["count"] for row in queued)} waiting</div></div>'
        f'<div class="lane-body"><ul class="qrail">{"".join(items)}</ul></div></div>'
    )


def _lanes(snapshot: dict) -> str:
    from lib.dashboard_pages.ci import ci_lane_html
    from lib.dashboard_pages.shell import px_strip

    devices, wakes = lane_devices(snapshot)
    held_tiles = idle_tiles = 0
    rows = [_queue_lane(snapshot), _qa_lane(snapshot)]
    for index, device in enumerate(devices):
        down = probe_down(snapshot, device["name"])
        cap = device["slot_max"]
        slots = max(1, device["used"]) if cap is None else max(1, cap)
        meter = px_strip(
            f"meter-{device['name']}", effect="meter", tint=_BAD if down else "#9ece6a",
            used=device["used"], slots=slots, seed=21 + index, height=8,
            still=down or not device["enabled"],
        )
        harness = (
            f'<span class="harness">{_e(" · ".join(device["harnesses"]))}</span>'
            if device["harnesses"] else ""
        )
        tiles = []
        seed = 0
        for row in device["jobs"]:
            seed += 1
            tiles.append(_slot(row, seed))
        for _ in range(device["used"] - len(device["jobs"])):
            seed += 1
            tiles.append(_held_tile(f"slot-{device['name']}-{seed}", seed))
        free = max(0, (cap or 0) - device["used"])
        if not device["used"] and not free:
            free = 1
        held_tiles += device["used"] - len(device["jobs"])
        if device["enabled"]:
            idle_tiles += free
        for _ in range(free):
            seed += 1
            tiles.append(_idle_tile(f"slot-{device['name']}-{seed}", not device["enabled"], seed))
        rows.append(
            f'<div class="lane{"" if device["enabled"] else " disabled"}" data-device="{_e(device["name"])}">'
            f'<div class="lane-head"><div class="lh1"><span class="name">{_e(device["name"])}</span>'
            f'{_device_chip(device, down)}'
            f'<div class="lane-meter" aria-hidden="true" style="width:{12 * slots}px">{meter}</div></div>'
            f'<div class="lh2"><span class="sub">{device["used"]} / {"?" if cap is None else cap} slots</span>'
            f'{harness}</div></div>'
            f'<div class="tiles">{"".join(tiles)}</div></div>'
        )
    if wakes:
        rows.append(
            '<div class="lane" data-device="grok-wakes"><div class="lane-head">'
            f'<div class="name">grok wakes</div>{_chip("BOT-TO-BOT", "accent")}</div>'
            f'<div class="tiles">{"".join(_slot(row, i + 1) for i, row in enumerate(wakes))}</div></div>'
        )
    rows.append(ci_lane_html(snapshot))
    warn = ' class="warn"' if held_tiles else ""
    count = (
        f'<b class="ok">{len(snapshot["running"])}</b> running · <b{warn}>{held_tiles}</b> held · '
        f"<b>{idle_tiles}</b> idle · <b>{len(snapshot['queued'])}</b> queued"
    )
    return _panel("lanes", "Lanes and slots", f'<div class="lanes">{"".join(rows)}</div>', count_html=count)


# --- Just happened and Intake -------------------------------------------------------------


def _just_happened(snapshot: dict) -> str:
    rows = (snapshot.get("handoff_savings") or {}).get("recent", [])[:6]
    items = []
    for row in rows:
        wake = row["kind"] == "bot_to_bot"
        items.append(
            "<li>" + _bot(row["recipient"]) + _span("ticket", _ticket(row)) + github_links(row)
            + _span("what", "done" if wake else row["action"])
            + (_chip("grok wake", "wake") if wake else "")
            + (_chip("failed", "bad") if row["state"] == "failed" else "")
            + _when(row["age_seconds"], suffix=" ago") + "</li>"
        )
    inner = f'<ul class="feed">{"".join(items)}</ul>' if items else '<p class="empty">no finished jobs yet</p>'
    inner += '<p class="more"><a href="activity.html">all activity</a></p>'
    return _panel("just-happened", "Just happened", inner, count=f"{len(rows)} newest handoffs")


_FEEDER_STATE = {
    "eligible": "eligible",
    "waiting-capacity": "waiting for capacity",
    "waiting-blocked": "waiting on a blocker",
    "refused-intake": "refused at intake",
    "feeding": "feeding",
    "error": "feed error, retrying",
    "fed": "fed",
    "done": "fed, job queued",
    "closed": "PR closed, not fed",
    "split-parent": "split into parts, not fed",
    "abandoned": "abandoned, not fed",
}


def _linear_webhook(snapshot: dict) -> tuple[str | None, str]:
    webhook = snapshot["poller"].get("linear_webhook")
    if webhook is None:
        return None, ""
    if webhook["last_received_age_seconds"] is not None:
        received = f'{duration(webhook["last_received_age_seconds"])} ago' + (
            f' ({webhook["last_event"]})' if webhook["last_event"] else ""
        )
    elif webhook["listener_age_seconds"] is None:
        received = "never (listener has not started)"
    else:
        received = "never"
    notes = f'<p class="bad">warning: {_e(webhook["warning"])}</p>' if webhook["warning"] is not None else ""
    if webhook.get("note") is not None:
        notes += f'<p class="muted">{_e(webhook["note"])}</p>'
    return f"Linear webhook · last received {received} · dirty queue {webhook['dirty']}", notes


class IntakeGroups(NamedTuple):
    trouble: list[dict]
    next: list[dict]
    blocked: list[dict]
    fed: list[dict]


def intake_groups(items: list[dict]) -> IntakeGroups:
    groups: dict[str, list[dict]] = {"trouble": [], "next": [], "blocked": [], "fed": []}
    for item in items:
        age = item.get("fed_age_seconds")
        group = INTAKE_GROUP.get(item["state"]) or ("fed" if age is not None and age < FED_TODAY_SECONDS else None)
        if group:
            groups[group].append(item)
    groups["fed"].sort(key=lambda item: item["fed_age_seconds"])
    return IntakeGroups(**groups)


def _feeder(snapshot: dict) -> str:
    feeder = snapshot.get("feeder")
    if not feeder:
        return ""
    numbers = {**feeder, "fed_today": len(intake_groups(feeder["items"]).fed)}
    stages = []
    for stage in FUNNEL:
        parts = [(part, numbers.get(part["key"], 0)) for part in stage.get("parts", [])]
        count = sum(n for _, n in parts) if parts else numbers.get(stage["key"], 0)
        body = f'<span class="flabel">{_e(stage["label"])}</span><b class="fnum">{_e(count)}</b>'
        if parts:
            body += (
                '<span class="fbar" aria-hidden="true">'
                + "".join(f'<i class="{part["cls"]}" style="flex-grow:{_e(n)}"></i>' for part, n in parts)
                + '</span><span class="fsplit">'
                + "".join(f'<span>{_e(n)} {_e(part["label"])}</span>' for part, n in parts)
                + "</span>"
            )
        if "href" in stage:
            body = f'<a href="{_e(stage["href"])}">{body}</a>'
        stages.append(f'<li class="fstage">{body}</li>')
    return f'<ol class="funnel" id="feeder">{"".join(stages)}</ol>'


def _tier_chip(item: dict) -> str:
    tier, own = item.get("tier"), item.get("own_tier")
    if tier is None:
        return ""
    return _chip(f"tier {tier}" + ("" if own == tier else f" (own {'none' if own is None else own})"), "accent")


def _intake_ticket(item: dict) -> str:
    return _span("ticket", f"{_short(item['repo'])}#{item['issue']}")


def _intake_what(item: dict) -> str:
    return _span("what", item.get("reason") or _FEEDER_STATE.get(item["state"], item["state"]))


def _intake_row(item: dict, tail: str = "") -> str:
    title = item.get("title")
    title_html = f'<span class="ititle" title="{_e(title)}">{_e(title)}</span>' if title else ""
    return f"<li>{_intake_ticket(item)}{title_html}{_tier_chip(item)}{_intake_what(item)}{tail}</li>"


def _fed_row(item: dict) -> str:
    pr = _span(None, f"→ PR #{item['pr']}") if item.get("pr") else ""
    return _intake_row(item, tail=pr + _when(item["fed_age_seconds"], prefix="fed ", suffix=" ago"))


def _intake_rows(col: dict, items: list[dict], draw=_intake_row) -> str:
    rows = INTAKE["rows"]
    more = f'<p class="more">+{len(items) - rows} {_e(col["more"])}</p>' if items[rows:] else ""
    return f'<ul class="feed">{"".join(draw(x) for x in items[:rows])}</ul>{more}'


def _fed_rows(col: dict, items: list[dict]) -> str:
    return _intake_rows(col, items, _fed_row)


class BlockerGroups(NamedTuple):
    drawn: list[tuple[str | None, list[dict]]]
    more: int
    others: int


def _blocker_refs(item: dict) -> list[str | None]:
    refs = list(dict.fromkeys(entry["ref"] for entry in item.get("blocked_by") or []))
    return refs or [None]


def blocker_groups(items: list[dict], limit: int) -> BlockerGroups:
    groups: dict[str | None, list[dict]] = {}
    for item in items:
        for ref in _blocker_refs(item):
            groups.setdefault(ref, []).append(item)
    drawn = sorted(groups.items(), key=lambda group: -len(group[1]))[:limit]
    shown = {ref for ref, _ in drawn}
    left = [refs for refs in map(_blocker_refs, items) if not shown.intersection(refs)]
    return BlockerGroups(drawn, len(left), len({ref for refs in left for ref in refs}))


def _fill(text: str, n: int, m: int = 0) -> str:
    return text.replace("{n}", str(n)).replace("{m}", str(m))


def _blocker_chip(item: dict) -> str:
    hover = " · ".join(text for text in (item.get("title"), item.get("reason")) if text)
    title = f' title="{_e(hover)}"' if hover else ""
    return f'<span class="chip muted"{title}>{_e(_short(item["repo"]))}#{_e(item["issue"])}</span>'


def _blocked_groups(col: dict, items: list[dict]) -> str:
    groups = blocker_groups(items, col["groups"])
    chips = col["chips"]
    blocks = []
    for ref, members in groups.drawn:
        head = _span("ticket", col["none"] if ref is None else _short(ref))
        head += _span("what", _fill(col["count"], len(members)))
        rest = _span("what", f"+{len(members) - chips}") if members[chips:] else ""
        row = "".join(_blocker_chip(x) for x in members[:chips])
        blocks.append(f'<div class="iblock"><div class="ihead">{head}</div><div class="ichips">{row}{rest}</div></div>')
    spread = col["spread_one"] if groups.others == 1 else col["spread"]
    more = f'<p class="more">{_e(_fill(spread, groups.more, groups.others))}</p>' if groups.more else ""
    return "".join(blocks) + more


_INTAKE_BODY = {"next": _intake_rows, "blocked": _blocked_groups, "fed": _fed_rows}


def _intake_column(col: dict, items: list[dict]) -> str:
    body = _INTAKE_BODY[col["key"]](col, items) if items else f'<p class="empty">{_e(col["empty"])}</p>'
    return f'<div class="icol"><div class="igroup">{_e(col["label"])}</div>{body}</div>'


def _intake(snapshot: dict) -> str:
    feeder = snapshot.get("feeder")
    line, notes = _linear_webhook(snapshot)

    def panel(inner: str) -> str:
        return _panel("intake", "Intake", inner, count=line, count_id="linear-webhook")

    if not feeder:
        empty = "no feeder has run" if line else "no listener or feeder has run"
        return panel(f'<div class="intake">{notes}<p class="empty">{empty}</p></div>')
    groups = intake_groups(feeder["items"])
    entries = "".join(f"<li>{_intake_ticket(x)}{_intake_what(x)}</li>" for x in groups.trouble)
    trouble_html = f'<ul class="itrouble">{entries}</ul>' if groups.trouble else ""
    columns = "".join(_intake_column(col, getattr(groups, col["key"])) for col in INTAKE["columns"])
    return panel(f'<div class="intake">{notes}{_feeder(snapshot)}{trouble_html}</div><div class="icols">{columns}</div>')


def lanes_script(script: str) -> str:
    return script.replace("__LANES__", json.dumps(lanes_table(), separators=(",", ":")))
