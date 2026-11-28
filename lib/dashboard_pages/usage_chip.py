from __future__ import annotations

from lib.dashboard_pages import efficiency
from lib.dashboard_pages.common import _e, duration
from lib.dashboard_static import read_asset

CSS = read_asset("css/usage_chip.css")
SCRIPT = read_asset("js/usage_chip.js")

OPEN = '<a class="uchip" id="usage-chip" href="efficiency.html#headroom"'


def _segment(harness: str, views: list[dict], now: float) -> dict:
    fresh = [view for view in views if view["entry"]["headroom"] is not None]
    if not fresh:
        return {"harness": harness, "pct": None}
    view = min(fresh, key=lambda v: v["entry"]["headroom"])
    entry = view["entry"]
    fullest = max((entry[w] for w in efficiency.WINDOWS if entry[w]["state"] == "ok"), key=lambda w: w["used"])
    return {
        "harness": harness,
        "pct": efficiency._half_up(entry["headroom"] * 100),
        "tone": "bad" if view["limit"] else view["tone"],
        "resets": None if fullest["resets_at"] is None else duration(fullest["resets_at"] - now),
    }


def _line(entry: dict, view: dict | None) -> str:
    limit = f" · {entry['limit_label']}" if entry["limit_label"] else ""
    if view is None:
        return f"{entry['harness']} {entry['account']}: not tracked{limit}"
    (w5, _), (w7, _) = view["windows"]
    return f"{entry['harness']} {efficiency._name(entry)}: 5-hour {w5}, 7-day {w7}{limit}"


def chip_model(snapshot: dict) -> dict:
    now = snapshot["generated_at_epoch"]
    accounts = snapshot["usage"]["accounts"]
    views = [None if e["state"] == "not tracked" else efficiency._view(e, now) for e in accounts]
    tracked = [view for view in views if view is not None]
    pools: dict[str, list[dict]] = {}
    for view in tracked:
        pools.setdefault(view["entry"]["harness"], []).append(view)
    return {
        "segments": [_segment(harness, pool, now) for harness, pool in pools.items()],
        "phone": _segment("usage", tracked, now) if tracked else None,
        "title": "\n".join(["Subscription headroom", *(_line(e, v) for e, v in zip(accounts, views))]),
    }


def _segment_html(segment: dict, phone: bool = False) -> str:
    seg = "uc-seg uc-phone" if phone else "uc-seg"
    harness = _e(segment["harness"])
    name = f'<span class="uc-name">{harness}</span>' if phone else f'<span class="uc-name {harness}">{harness}</span>'
    pct = segment["pct"]
    if pct is None:
        return f'<span class="{seg} stale">{name}<span class="uc-stale">stale</span></span>'
    reset = "" if segment["resets"] is None else f'<span class="uc-reset">{segment["resets"]}</span>'
    return (
        f'<span class="{seg} {segment["tone"]}">{name}'
        f'<span class="uc-bar" aria-hidden="true"><i style="width:{pct}%"></i></span><b>{pct}%</b>{reset}</span>'
    )


def _usage_chip(snapshot: dict) -> str:
    model = chip_model(snapshot)
    if not model["segments"]:
        return f"{OPEN} hidden></a>"
    title = _e(model["title"])
    segments = "".join(map(_segment_html, model["segments"])) + _segment_html(model["phone"], phone=True)
    return f'{OPEN} title="{title}" aria-label="{title}">{segments}</a>'
