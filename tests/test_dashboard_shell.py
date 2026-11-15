"""The global shell on every page (#139, Mission Control T2): the TopBar with
its pixel caret, the header band, the attention strip and the status line.

Artboards: docs/design/mission-control/reference/artboards/TopBar.dc.html,
HeaderBand.dc.html and StatusLine.dc.html. Every piece has a server render and
a page-script twin; the node tests run the real page script (the harness in
tests/test_dashboard_layout.py) and compare its markup with the server's.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

import lib.dashboard_page as page
from lib.dashboard import collect_snapshot, render_pages
from lib.health import CODE_COMPONENTS
from tests.test_dashboard import NOW, seeded
from tests.test_dashboard_layout import BAR, _no_script, _run_page, _section, _text, needs_node
from tests.test_dashboard_pixelstrip import _node

PAGES = ("index.html", "job.html", "activity.html", "efficiency.html", "health.html")
SCRIPT_PAGE = {"index.html": "main", "job.html": "job", "activity.html": "activity",
               "efficiency.html": "efficiency", "health.html": "health"}
NAV = [("index.html", "Lanes"), ("job.html", "Tickets"), ("activity.html", "Activity"),
       ("efficiency.html", "Efficiency"), ("health.html", "System")]
SHELL = ["caret", "band", "attention-strip", "statusline"]
# The harness clock starts 3 s after the snapshot; this puts it back, so
# every ticking age reads as the server wrote it.
AT_SNAPSHOT = [["advance", -3]]


@pytest.fixture
def base(tmp_path: Path) -> dict:
    return collect_snapshot(*seeded(tmp_path), now=NOW)


def _inner(html: str, ident: str) -> str:
    whole = _footer(html) if ident == "statusline" else _section(_no_script(html), ident)
    return whole[whole.index(">") + 1 : whole.rindex("<")]


def _footer(html: str) -> str:
    match = re.search(r'<footer class="statusline" id="statusline">.*?</footer>', html, flags=re.S)
    assert match
    return match.group(0)


def _script(tmp_path: Path, snapshot: dict, name: str = "index.html", actions: list | None = None,
            ids: list[str] | None = None) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    return _run_page(tmp_path, snapshot, SCRIPT_PAGE[name], ids or BAR,
                     actions=AT_SNAPSHOT if actions is None else actions)


def _segments(snapshot: dict) -> dict[str, dict]:
    return {seg["key"]: seg for seg in page.status_segments(snapshot)}


# --- 1. the attention strip ------------------------------------------------------------


def test_the_strip_lists_exactly_the_badge_items_on_every_page(base: dict) -> None:
    items = base["attention"]["items"]
    assert base["attention"]["count"] == len(items) == 3
    for name, html in render_pages(base).items():
        strip = _section(_no_script(html), "attention-strip")
        assert " hidden" not in strip[: strip.index(">")], name
        assert re.search(r'<b id="attention-count">3</b><span> need attention</span>', strip), name
        assert 'href="activity.html#attention"' in strip
        rows = re.findall(
            r'<div class="attn-item"><span class="chip ([a-z]+)">([^<]+)</span>'
            r'<a class="attn-who" href="([^"]+)">([^<]+)</a><span class="attn-what">([^<]*)</span></div>',
            strip,
        )
        assert [(kind, who, what) for _, kind, _, who, what in rows] == [
            ("stuck", "aut0#AUT-7", "stuck: job-timeout"),
            ("overdue", "cleffy#14", "cursor fix running, no heartbeat for 2h 00m"),
            ("paused", "tracy-harris-coaching#5", "waiting-max (clarify)"),
        ]
        assert {href for _, _, href, _, _ in rows} == {"job.html"}
        assert [cls for cls, *_ in rows] == ["bad", "bad", "warn"]


def test_one_item_reads_needs(base: dict) -> None:
    one = copy.deepcopy(base)
    one["attention"] = {"count": 1, "items": base["attention"]["items"][:1]}
    strip = _section(render_pages(one)["index.html"], "attention-strip")
    assert "<b id=\"attention-count\">1</b><span> needs attention</span>" in strip


def _clear(snapshot: dict) -> dict:
    clear = copy.deepcopy(snapshot)
    clear["attention"] = {"count": 0, "items": []}
    return clear


def test_the_strip_is_absent_when_the_count_is_zero(base: dict) -> None:
    for name, html in render_pages(_clear(base)).items():
        assert '<div class="attn" id="attention-strip" hidden></div>' in html, name
        assert "attention-count" not in _no_script(html)
        assert 'data-px-key="attention"' not in html


@needs_node
@pytest.mark.parametrize("name", PAGES)
def test_the_script_strip_is_the_server_strip_on_every_page(tmp_path: Path, base: dict, name: str) -> None:
    html = render_pages(base)[name]
    out = _script(tmp_path / "busy", base, name)
    assert out["html"]["attention-strip"] == _inner(html, "attention-strip")
    assert out["hidden"]["attention-strip"] is False
    clear = _clear(base)
    out = _script(tmp_path / "clear", clear, name)
    assert out["html"]["attention-strip"] == ""
    assert out["hidden"]["attention-strip"] is True


def _health_rows(*rows: dict, publish: float | None = 15) -> dict:
    return {
        "known": True, "error": None, "code_version": "abc1234def", "no_waiter": [], "down": 0,
        "stale_code": 0, "stale": 0, "publish_seconds": publish,
        "thresholds": {"beat_seconds": 15.0, "probe_seconds": 60.0, "stale_beats": 3, "down_beats": 8},
        "components": [dict(_row(), **row) for row in rows],
    }


def _row() -> dict:
    return {"component": "poller", "device": "box", "bot": "", "pid": 1, "state": "up", "reason": "",
            "last_seen_et": "x", "last_seen_age_seconds": 0, "interval_seconds": 15.0, "configured": True,
            "started_at_et": None, "started_at": None, "version": "abc1234def", "stale_code": False,
            "label": "poller @ box", "anchor": "health-poller-box"}


@needs_node
def test_the_script_strip_adds_what_an_overdue_snapshot_ages(tmp_path: Path, base: dict) -> None:
    """The strip lists the badge's items plus agedHealth's: the writer that
    went down, or the one overdue-snapshot item when no writer can alert."""
    writer = copy.deepcopy(base)
    writer["health"] = _health_rows(
        {}, {"component": "dashboard", "label": "dashboard @ box", "anchor": "health-dashboard-box"})
    out = _script(tmp_path / "writer", writer, "activity.html", actions=[["advance", 8 * 15 + 1]],
                  ids=BAR + ["attention"])
    assert out["text"]["attention-count"] == "4"
    strip = out["html"]["attention-strip"]
    assert strip.count('class="attn-item"') == 4
    assert ('<span class="chip bad">down</span><a class="attn-who" href="health.html#health-dashboard-box">'
            "dashboard @ box</a>") in strip
    assert "dashboard @ box" in out["text"]["attention"]

    lone = copy.deepcopy(base)
    lone["health"] = _health_rows({}, publish=None)
    out = _script(tmp_path / "lone", lone, "index.html", actions=[["advance", 3 * 15 + 1]])
    assert out["text"]["attention-count"] == "4"
    assert ('<span class="chip bad">health</span><a class="attn-who" href="health.html#components">'
            "health snapshot</a>") in out["html"]["attention-strip"]


@needs_node
def test_a_hostile_component_link_falls_back_to_the_system_page(tmp_path: Path, base: dict) -> None:
    hostile = copy.deepcopy(base)
    hostile["attention"] = {"count": 1, "items": [
        {"kind": "down", "repo": "", "ticket_id": "", "component": "<b>x</b>",
         "link": "javascript:alert(1)", "detail": "down \"now\""}]}
    html = render_pages(hostile)["index.html"]
    strip = _inner(html, "attention-strip")
    assert '<a class="attn-who" href="health.html">&lt;b&gt;x&lt;/b&gt;</a>' in strip
    assert "javascript" not in strip
    assert _script(tmp_path, hostile)["html"]["attention-strip"] == strip


# --- 2. the status line ----------------------------------------------------------------

def _devices(snapshot: dict) -> dict:
    out = copy.deepcopy(snapshot)
    out["slots"]["devices"] = [
        {"name": "box", "slot_max": 3, "held": 0, "enabled": True, "placeholder": False, "harnesses": []},
        {"name": "pc", "slot_max": 6, "held": 5, "enabled": True, "placeholder": False, "harnesses": []},
        {"name": "mac", "slot_max": 4, "held": 0, "enabled": False, "placeholder": False, "harnesses": []},
        {"name": "spare", "slot_max": 2, "held": 0, "enabled": True, "placeholder": True, "harnesses": []},
        {"name": "lab", "slot_max": None, "held": 1, "enabled": True, "placeholder": False, "harnesses": []},
    ]
    out["running"][0]["device"] = "pc"
    out["running"][1]["device"] = "attic"
    return out


def _set(path: str, value: object):
    def apply(snapshot: dict) -> dict:
        out = copy.deepcopy(snapshot)
        *head, last = path.split(".")
        node = out
        for key in head:
            node = node[key]
        node[last] = value
        return out
    return apply


def _webhook(age: int | None, warning: str | None = None):
    return _set("poller.linear_webhook", {
        "last_received_age_seconds": age, "last_event": "Issue", "listener_age_seconds": 30,
        "dirty": 0, "warning": warning, "note": None})


def _feeder(*states: str):
    return _set("feeder", {"eligible": 2, "fed": 11, "waiting_capacity": 0, "waiting_blocked": 0,
                           "items": [{"repo": "o/r", "issue": n, "state": s, "reason": None, "pr": None}
                                     for n, s in enumerate(states)]})


def _health(*rows: dict, code: str = "abc1234def", known: bool = True, error: str | None = None):
    def apply(snapshot: dict) -> dict:
        out = copy.deepcopy(snapshot)
        out["health"] = _health_rows(*rows)
        out["health"].update(code_version=code, known=known, error=error)
        return out
    return apply


def _waiter(n: int, **extra: object) -> dict:
    return {"component": "waiter", "bot": f"bot{n}", "label": f"waiter bot{n} @ box",
            "anchor": f"health-waiter-bot{n}-box", **extra}


# (case id, how to change the base snapshot, segment key, text, state, link)
SEGMENTS = [
    ("devices", _devices, "device:box", "box 0/3", None, None),
    ("device-held", _devices, "device:pc", "pc 5/6", None, None),
    ("device-disabled", _devices, "device:mac", "mac off", None, None),
    ("device-placeholder", _devices, "device:spare", "spare off", None, None),
    ("device-no-slot-max", _devices, "device:lab", "lab 1/—", None, None),
    ("device-unlisted", _devices, "device:attic", "attic 1/—", None, None),
    ("queue", _set("queue.totals.queued", 1234), "queue", "queue 1,234", None, None),
    ("poll-ok", _set("poller.health", {"state": "ok", "last_cycle_age_seconds": 50}),
     "poll", "poll ok · 50s", "ok", "health.html#health"),
    ("poll-late", _set("poller.health", {"state": "late", "last_cycle_age_seconds": 200}),
     "poll", "poll late · 3m 20s", "warn", "health.html#health"),
    ("poll-stale", _set("poller.health", {"state": "stale", "last_cycle_age_seconds": 4000}),
     "poll", "poll stale · 1h 06m", "bad", "health.html#health"),
    ("poll-unknown", _set("poller.health", None), "poll", "poll unknown", "muted", "health.html#health"),
    ("poll-errors-0", _set("poller.errors.last_hour", 0), "poll-errors", "0 poll errors / 1 h", "ok",
     "health.html#health"),
    ("poll-errors-1", _set("poller.errors.last_hour", 1), "poll-errors", "1 poll error / 1 h", "bad",
     "health.html#health"),
    ("poll-errors-bad", _set("poller.errors.last_hour", 45), "poll-errors", "45 poll errors / 1 h", "bad",
     "health.html#health"),
    ("linear-ok", _set("poller.linear_budget", None), "linear", "linear ok", "ok", "health.html#health"),
    ("linear-deferring", _set("poller.linear_budget.state", "deferring"), "linear", "linear deferring",
     "warn", "health.html#health"),
    ("linear-exhausted", _set("poller.linear_budget.state", "exhausted"), "linear", "linear exhausted",
     "bad", "health.html#health"),
    ("linear-rate-limited", _set("poller.linear_budget.state", "rate_limited"), "linear",
     "linear rate limited", "bad", "health.html#health"),
    ("webhook-ok", _webhook(371), "webhook", "webhook 6m 11s", "ok", "index.html#linear-webhook"),
    ("webhook-warning", _webhook(371, "no event for 6m"), "webhook", "webhook 6m 11s", "bad",
     "index.html#linear-webhook"),
    ("webhook-never", _webhook(None), "webhook", "webhook never", "warn", "index.html#linear-webhook"),
    ("webhook-never-warning", _webhook(None, "listener down"), "webhook", "webhook never", "bad",
     "index.html#linear-webhook"),
    ("webhook-absent", _set("poller.linear_webhook", None), "webhook", "no webhook", "muted",
     "index.html#kpis"),
    ("feeder-ok", _feeder("fed", "eligible"), "feeder", "feeder 11 fed", "ok", "index.html#feeder"),
    ("feeder-error", _feeder("fed", "error"), "feeder", "feeder 11 fed", "bad", "index.html#feeder"),
    ("feeder-absent", _set("feeder", None), "feeder", "no feeder", "muted", "index.html#kpis"),
    ("components-up", _health({}, _waiter(1)), "components", "2 / 2 up", "ok", "health.html#components"),
    ("components-stale", _health({}, _waiter(1, state="stale")), "components", "1 / 2 up", "warn",
     "health.html#components"),
    ("components-down", _health({}, _waiter(1, state="down"), _waiter(2, state="stale")), "components",
     "1 / 3 up", "bad", "health.html#components"),
    ("components-not-configured", _health({}, _waiter(1, state="down", configured=False)), "components",
     "1 / 1 up", "ok", "health.html#components"),
    ("components-unknown", _health(known=False), "components", "components unknown", "muted",
     "health.html#components"),
    ("components-unreadable", _health(known=False, error="cannot read orch.health.db"), "components",
     "components unknown", "bad", "health.html#components"),
    ("version-current", _health({}, _waiter(1)), "version", "abc1234 · 0 behind", "ok",
     "health.html#components"),
    ("version-behind", _health({"stale_code": True, "version": "0ld0ld0ld"},
                               _waiter(1, stale_code=True, version="0ld0ld0ld"), _waiter(2)),
     "version", "abc1234 · 2 behind", "warn", "health.html#components"),
    ("version-unknown-checkout", _health({}, code="unknown"), "version", "version unknown", "warn",
     "health.html#components"),
    ("version-unknown-component", _health({"version": "unknown"}, _waiter(1)), "version",
     "abc1234 · 0 behind", "ok", "health.html#components"),
]


@pytest.mark.parametrize(("case", "change", "key", "text", "state", "href"), SEGMENTS,
                         ids=[row[0] for row in SEGMENTS])
def test_each_segment_is_computed_from_the_snapshot(
    base: dict, case: str, change, key: str, text: str, state: str | None, href: str | None,
) -> None:
    snapshot = change(base)
    seg = _segments(snapshot)[key]
    shown = text if seg["label"] is None else f'{seg["label"]} {seg["text"]}'
    assert (shown, seg["state"], seg["href"]) == (text, state, href), case
    footer = _footer(render_pages(snapshot)["index.html"])
    if state is None:
        assert f'data-seg="{key}"><span class="sl-k">{seg["label"]}</span><span class="sl-v">{seg["text"]}' in footer
    else:
        assert (f'<a class="sl-vital {state}" href="{href}" data-seg="{key}"><span class="blk" '
                f'aria-hidden="true"></span><span>{text}</span></a>') in footer


@needs_node
@pytest.mark.parametrize(("case", "change"), [(row[0], row[1]) for row in SEGMENTS],
                         ids=[row[0] for row in SEGMENTS])
def test_the_script_status_line_is_the_server_status_line(tmp_path: Path, base: dict, case: str, change) -> None:
    snapshot = change(base)
    out = _script(tmp_path, snapshot)
    assert out["html"]["statusline"] == _inner(render_pages(snapshot)["index.html"], "statusline"), case


@needs_node
def test_the_script_ticks_the_ages_and_ages_the_components(tmp_path: Path, base: dict) -> None:
    snapshot = _webhook(371)(_set("poller.health", {"state": "ok", "last_cycle_age_seconds": 50})(
        _health({}, _waiter(1))(base)))
    out = _script(tmp_path, snapshot, actions=[["advance", 10 * 15]])
    line = out["html"]["statusline"]
    assert "<span>poll ok · 3m 23s</span>" in line  # 50 s + 3 s + 150 s
    assert "<span>webhook 8m 44s</span>" in line
    # The snapshot is overdue, so the page can no longer see either component.
    assert ('<a class="sl-vital warn" href="health.html#components" data-seg="components">'
            '<span class="blk" aria-hidden="true"></span><span>0 / 2 up</span></a>') in line


def test_the_status_line_is_on_every_page_after_main(base: dict) -> None:
    for name, html in render_pages(base).items():
        assert html.index("</main>") < html.index('<footer class="statusline" id="statusline">'), name
        footer = _footer(html)
        assert footer.count('<span class="sl-brand">factory</span>') == 1
        for href in re.findall(r'href="([^"]+)"', footer):
            assert re.fullmatch(r"(health|index|efficiency)\.html#[a-z-]+", href), href


def test_bad_and_warn_segments_are_red_or_orange_text_and_block() -> None:
    css = page.CSS
    assert ".sl-vital .blk{width:8px;height:8px;background:currentColor}" in css
    assert ".bad{color:var(--bad)}" in css and ".warn{color:var(--warn)}" in css
    # Only an ok segment keeps the quiet text colour with a green block.
    assert ".sl-vital.ok{color:var(--text-2)}.sl-vital.ok .blk{background:var(--ok)}" in css
    assert ".statusline{position:sticky;bottom:0;" in css
    assert "min-height:32px;\nbackground:var(--dim);border-top:1px solid var(--line)" in css


def test_behind_is_the_stale_code_rule() -> None:
    """version_drift counts exactly lib/health.py's stale_code components;
    an unknown version is listed apart and never counted as behind."""
    health = _health_rows(
        {"stale_code": True, "version": "0ld"},
        _waiter(1, stale_code=True, version="0ld"),
        _waiter(2, version="unknown"),
        _waiter(3, stale_code=True, version="0ld", configured=False),
        {"component": "tunnel", "label": "tunnel @ box", "version": "unknown"},
    )
    drift = page.version_drift(health)
    assert drift == {"newest": "abc1234def", "behind": ["poller @ box", "waiter bot1 @ box"],
                     "unknown": ["waiter bot2 @ box"]}
    flagged = [c["label"] for c in health["components"] if c["stale_code"] and c["configured"]]
    assert drift["behind"] == flagged
    assert set(page.shell_table()["code"]) == set(CODE_COMPONENTS)
    assert page.version_drift(None) == {"newest": "unknown", "behind": [], "unknown": []}


# --- the header band ----------------------------------------------------------------


@pytest.mark.parametrize(("change", "load"), [
    (lambda s: s, "0.000"),  # no device has a slot_max
    (_devices, "0.556"),  # (0 + 5) / (3 + 6): disabled, placeholder and no-slot devices left out
])
def test_band_brightness_is_running_over_total_slots(base: dict, change, load: str) -> None:
    snapshot = change(base)
    band = _section(render_pages(snapshot)["index.html"], "band")
    assert ('<canvas class="px" data-px="field" data-tint="#9ece6a" data-load="' + load
            + '" data-seed="21" data-px-key="band" aria-hidden="true" style="--px:#9ece6a;--l:' + load
            + ';height:24px">') in band


@needs_node
def test_the_script_band_is_the_server_band(tmp_path: Path, base: dict) -> None:
    snapshot = _devices(base)
    assert _script(tmp_path, snapshot)["html"]["band"] == _inner(render_pages(snapshot)["index.html"], "band")


# --- 3. the caret per link state ------------------------------------------------------

LINK = {"link-live": ("#9ece6a", 1), "link-stale": ("#ff9e64", 0.4), "link-lost": ("#f7768e", 0),
        "link-boot": ("#8b93b8", 0)}


def test_caret_effect_and_rate_per_link_state_are_in_the_table() -> None:
    states = page.px_table()["states"]
    for name, (tint, rate) in LINK.items():
        assert states[name] == {"effect": "caret", "tint": tint, "rate": rate}, name
    script = page.px_script()
    (blob,) = re.findall(r"var TABLE = (\{.*?\});\n", script)
    assert {k: v for k, v in json.loads(blob)["states"].items() if k.startswith("link-")} == {
        name: {"effect": "caret", "tint": tint, "rate": rate} for name, (tint, rate) in LINK.items()}


def test_the_page_starts_on_the_still_boot_caret(base: dict) -> None:
    for name, html in render_pages(base).items():
        caret = re.search(r'<span class="caret" id="caret" aria-hidden="true">(.*?)</span>', html).group(1)
        assert caret == ('<canvas class="px" data-px="caret" data-tint="#8b93b8" data-rate="0" '
                         'data-px-key="caret" aria-hidden="true" style="--px:#8b93b8;height:12px"></canvas>'), name


def _caret(tint: str, rate: float) -> str:
    return (f'<canvas class="px" data-px="caret" data-tint="{tint}" data-rate="{rate:g}" '
            f'data-px-key="caret" aria-hidden="true" style="--px:{tint};height:12px"></canvas>')


@needs_node
@pytest.mark.parametrize(("link", "generated", "advance", "label"), [
    ("link-live", NOW, -3, "LIVE"),
    ("link-stale", NOW - 200, -3, "STALE"),
    ("link-lost", NOW, 7, "LINK LOST"),
])
def test_update_link_swaps_the_caret_by_link_state(
    tmp_path: Path, base: dict, link: str, generated: float, advance: int, label: str,
) -> None:
    snapshot = copy.deepcopy(base)
    snapshot["generated_at_epoch"] = generated
    out = _script(tmp_path, snapshot, actions=[["advance", advance]])
    assert out["text"]["link-text"] == label
    assert out["html"]["caret"] == _caret(*LINK[link])
    assert out["html"]["caret"] == page.px_strip("caret", state=link, height=12)


ON_OFF = r"""
function sample(rate, still, reduced) {
  const s = Object.create(PX.Strip.prototype);
  Object.assign(s, {props: {effect: 'caret', tint: '#9ece6a', cell: 4, rate: String(rate), still: still},
    _el: canvas({}, 8, 12), _t0: 0, _beatSeen: 0, _burstSeen: 0, _beatAt: -1000, _burstAt: -1000, _doneAt: null,
    _sysStill: !!reduced});
  const seen = [];
  for (let ms = 0; ms <= 10000; ms += 50) { const start = s._el.ops.length; s._draw(ms);
    const ops = s._el.ops.slice(start); if (ops.length) seen.push([ms, ops.filter(o => o[0] === 'r').length > 1]); }
  return seen;
}
out.live = sample(1, false); out.stale = sample(0.4, false); out.lost = sample(0, false);
out.stillStale = sample(0.4, true); out.reduced = sample(0.4, false, true);
"""


def _runs(seen: list) -> list[int]:
    """Lengths in ms of each on or off run, dropping the cut first and last."""
    edges = [ms for (ms, on), (_, before) in zip(seen[1:], seen) if on != before]
    return [b - a for a, b in zip(edges, edges[1:])]


@needs_node
def test_live_blinks_at_1_hz_stale_at_0_4_hz_and_lost_is_static(tmp_path: Path) -> None:
    out = _node(tmp_path, ON_OFF)
    assert set(_runs(out["live"])) == {500}  # on 0.5 s, off 0.5 s: 1 Hz
    assert set(_runs(out["stale"])) == {1250}  # on 1.25 s, off 1.25 s: 0.4 Hz
    # Lost: one lit frame, then nothing changes, so nothing is redrawn.
    assert out["lost"] == [[0, True]]
    # A still caret (reduced motion) is one lit frame at any rate.
    assert out["stillStale"] == [[0, True]]
    assert out["reduced"] == [[0, True]]


@needs_node
@pytest.mark.parametrize("reduced", [False, True])
def test_static_carets_draw_once_and_the_loop_stops(tmp_path: Path, reduced: bool) -> None:
    out = _node(tmp_path, r"""
      const states = ['link-live', 'link-stale', 'link-lost', 'link-boot'];
      found = states.map(s => PX.strip('caret-' + s, {state: s, height: 12}));
      found.forEach(el => { el.clientWidth = 8; el.clientHeight = 12; });
      PX.bind();
      for (let t = 1050; t <= 5050; t += 50) frame(t);
      out.draws = found.map(draws);
      out.lit = found.map(el => rects(el) > draws(el));
      out.pending = rafQueue.length;
    """, reduced=reduced)
    if reduced:
        assert out["draws"] == [1, 1, 1, 1]
        assert out["pending"] == 0
    else:
        assert out["draws"][2:] == [1, 1]
        assert out["draws"][0] > 50 and out["draws"][1] > 50
        assert out["pending"] == 1
    assert out["lit"][2:] == [True, True]
    if reduced:
        assert out["lit"] == [True] * 4


@needs_node
def test_script_strips_with_rate_load_and_seed_are_the_server_strips(tmp_path: Path) -> None:
    calls = [
        ("caret", {"state": "link-live", "height": 12}),
        ("caret", {"state": "link-stale", "height": 12}),
        ("caret", {"state": "link-lost", "height": 12}),
        ("caret", {"state": "link-boot", "height": 12}),
        ("band", {"state": "band", "load": 0.69, "height": 24}),
        ("band", {"state": "band", "load": 5 / 9, "height": 24}),
        ("band", {"state": "band", "load": 7, "height": 24}),
        ("attention", {"state": "attention", "height": 16}),
        ("x", {"effect": "caret", "tint": "#7dcfff", "rate": 2.5, "seed": 3}),
    ]
    out = _node(tmp_path, "out.calls = " + json.dumps(calls) + ".map(c => serialize(PX.strip(c[0], c[1])));")
    assert out["calls"] == [page.px_strip(key, **kwargs) for key, kwargs in calls]


# --- 4. nav labels, the current item and the file names ----------------------------------


def test_nav_labels_current_item_and_file_names_on_every_page(base: dict) -> None:
    pages = render_pages(base)
    assert tuple(pages) == PAGES == page.PAGES
    for name, html in pages.items():
        nav = _section(html, "nav")
        links = re.findall(r'<a href="([^"]+)"( aria-current="page")?>([^<]+)</a>', nav)
        assert [(href, label) for href, _, label in links] == NAV, name
        assert [href for href, current, _ in links if current] == [name], name
        label = dict(NAV)[name].lower()
        assert f"<title>factory mission control · {label}</title>" in html


def test_current_item_style_is_the_artboards() -> None:
    css = page.CSS
    rule = re.search(r"\.nav a\[aria-current=page\]\{([^}]*)\}", css).group(1)
    assert "background:var(--panel-hi)" in rule and "box-shadow:inset 0 -2px 0 var(--brand)" in rule
    assert "color:var(--hdr-text)" in rule
    assert "--panel-hi:#24283b" in css and "--brand:#9ece6a" in css and "--hdr-text:#ced8ff" in css
    assert ".nav a{padding:6px 12px;color:var(--hdr-text-2);text-decoration:none;font:500 14px/1.2 var(--sans)}" in css


def test_anchors_stay_and_shell_links_only_name_the_five_files(base: dict) -> None:
    pages = render_pages(base)
    assert re.search(r'<article class="jobpage" id="job-r-codex-">', pages["job.html"])
    assert 'href="job.html#job-r-codex-"' in pages["index.html"]
    for name, html in pages.items():
        shell = html[html.index('<header class="topbar"') : html.index("</header>")] + _footer(html)
        for href in re.findall(r'href="([^"#]+)', shell):
            assert href in PAGES, (name, href)


def test_main_keeps_the_ticket_box_model() -> None:
    rule = re.search(r"\nmain\{([^}]*)\}", page.CSS).group(1)
    # align-content:start: main grows to push the status line down, rows do not stretch.
    for part in ("max-width:1600px", "padding:20px 24px 32px", "gap:16px", "flex:1 0 auto", "align-content:start"):
        assert part in rule


def test_topbar_markup_follows_the_artboard(base: dict) -> None:
    html = render_pages(base)["index.html"]
    top = html[html.index('<header class="topbar"') : html.index("</header>")]
    assert '<div class="brand"><span>Factory <b>Mission Control</b></span><span class="caret"' in top
    assert '<div class="link boot" id="link" role="status" aria-live="polite">' in top
    assert top.index('id="nav"') < top.index('id="link"') < top.index('class="meta"') < top.index('id="band"')
    assert top.index('id="band"') < top.index('id="attention-strip"')
    assert "read-only, redacted" in _text(top)
    css = page.CSS
    assert ".top-row{display:flex;flex-wrap:wrap;align-items:center;gap:8px 18px;min-height:56px;padding:8px 24px}" in css
    assert "min-height:36px;padding:6px 24px;" in css  # 56 + 24 + 36 = the artboard's 116 px


# --- every shell link lands ------------------------------------------------------------


def _without_webhook_or_feeder(snapshot: dict) -> dict:
    out = copy.deepcopy(snapshot)
    out["poller"]["linear_webhook"] = None
    out["feeder"] = None
    return out


@pytest.mark.parametrize("change", [lambda s: s, _without_webhook_or_feeder], ids=["full", "bare"])
def test_every_shell_link_lands_on_an_anchor_its_page_renders(base: dict, change) -> None:
    snapshot = change(base)
    pages = render_pages(snapshot)
    ids = {name: set(re.findall(r'\sid="([^"]+)"', _no_script(html))) for name, html in pages.items()}
    targets = {seg["key"]: seg["href"] for seg in page.status_segments(snapshot) if seg["href"]}
    assert set(targets) == set(page.STATUS_LINKS)
    for name, html in pages.items():
        shell = (html[html.index('<header class="topbar"') : html.index("</header>")]
                 + _footer(html))
        for href in re.findall(r'href="([^"]+#[^"]+)"', shell) + list(targets.values()):
            target, anchor = href.split("#")
            assert anchor in ids[target], (name, href)


@needs_node
def test_the_script_status_line_links_where_the_server_links_without_webhook_or_feeder(
        tmp_path: Path, base: dict) -> None:
    bare = _without_webhook_or_feeder(base)
    assert _script(tmp_path, bare)["html"]["statusline"] == _inner(render_pages(bare)["index.html"], "statusline")


def test_the_status_line_stays_stuck_to_the_bottom_at_every_width() -> None:
    positions = re.findall(r"\.statusline\{[^}]*?position:([a-z]+)", page.CSS)
    assert positions == ["sticky"]


def test_the_no_script_band_reflects_load(base: dict) -> None:
    def band(held: int) -> str:
        snapshot = copy.deepcopy(base)
        snapshot["running"] = []
        snapshot["slots"]["devices"] = [{"name": "box", "slot_max": 3, "held": held, "enabled": True,
                                         "placeholder": False, "harnesses": []}]
        return _section(render_pages(snapshot)["index.html"], "band")
    idle, full = band(0), band(3)
    assert 'data-load="0.000"' in idle and 'data-load="1.000"' in full
    assert "--l:0.000;" in idle and "--l:1.000;" in full
    rule = re.search(r"\n\.px\[data-load\]\{([^}]*)\}", page.CSS)
    assert rule and "var(--l" in rule.group(1)
