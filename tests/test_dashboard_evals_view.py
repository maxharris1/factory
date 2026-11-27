from __future__ import annotations

import http.client
import importlib.util
import json
import os
import re
import threading
from collections.abc import Callable
from datetime import date, timedelta
from html import escape as html_escape
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib import dashboard, dashboard_evals
from lib.dashboard import EVALS, write_snapshot_pages
from lib.dashboard_evals import KEEP, METRIC_KEYS, collect_weekly, evals_json
from lib.dashboard_page import render_pages, render_static_assets
from lib.dashboard_pages.evals import EVAL_ROWS, _evals
from lib.dashboard_pages.shell import _PAGE_NAMES
from lib.dashboard_static import read_asset
from tests import test_dashboard_layout as layout
from tests.test_dashboard import run_orch, seeded, snapshot_for
from tests.test_dashboard_layout import BAR, _run_page, needs_node

ROOT = Path(__file__).resolve().parents[1]

GOOD = "weekly-2026-11-23.json"
BAD = "weekly-2026-11-16.json"
SINCE = "2026-11-16T13:00:00Z"
UNTIL = "2026-11-23T13:00:00Z"


def report(metrics: dict | None = None) -> dict:
    return {
        "window": {"since": SINCE, "until": UNTIL},
        "windowed_metrics": ["poll-errors"],
        "metrics": {"tickets-succeeded": 3} if metrics is None else metrics,
        "followups": [],
        "qa": {"unrecorded": "no qa table"},
    }


def write_week(directory: Path, name: str, payload: object = None) -> Path:
    path = directory / name
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        text = payload if isinstance(payload, str) else json.dumps(payload or report())
        path.write_text(text, encoding="utf-8")
    return path


def weeks(bundle: dict) -> list[str]:
    return [week["week"] for week in bundle["weeks"]]


def _oversize(directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dashboard_evals, "MAX_SNAPSHOT_BYTES", 1024)
    write_week(directory, BAD, dict(report(), followups=["x" * 2000]))


Seed = Callable[[Path, pytest.MonkeyPatch], object]
SKIPS: dict[str, Seed] = {
    "bad date": lambda d, _: write_week(d, "weekly-2026-13-40.json"),
    "bad name": lambda d, _: write_week(d, "weekly-x.json"),
    "backup name": lambda d, _: write_week(d, GOOD + ".bak"),
    "symlink": lambda d, _: (d / BAD).symlink_to(write_week(d.parent, "outside.json")),
    "fifo": lambda d, _: os.mkfifo(d / BAD),
    "hard link": lambda d, _: os.link(write_week(d.parent, "outside.json"), d / BAD),
    "oversize": _oversize,
    "not json": lambda d, _: write_week(d, BAD, "{"),
    "not utf-8": lambda d, _: write_week(d, BAD, b'{"metrics": {}, "x": "\xff"}'),
    "nan": lambda d, _: write_week(d, BAD, '{"metrics": {"poll-errors": NaN}}'),
    "infinity": lambda d, _: write_week(d, BAD, '{"metrics": {"poll-errors": Infinity}}'),
    "deep nesting": lambda d, _: write_week(d, BAD, "[" * 100_000),
    "top-level list": lambda d, _: write_week(d, BAD, "[]"),
    "metrics list": lambda d, _: write_week(d, BAD, '{"metrics": []}'),
}


@pytest.mark.parametrize("seed", SKIPS.values(), ids=SKIPS)
def test_each_bad_file_is_skipped_and_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: Seed
) -> None:
    evals = tmp_path / "evals"
    evals.mkdir()
    write_week(evals, GOOD)
    seed(evals, monkeypatch)
    bundle = collect_weekly(evals)
    assert (bundle["state"], weeks(bundle), bundle["skipped"]) == ("ok", ["2026-11-23"], 1)


def test_the_newest_twelve_weeks_are_kept_newest_first(tmp_path: Path) -> None:
    dated = [(date(2026, 11, 23) - timedelta(weeks=n)).isoformat() for n in range(KEEP + 2)]
    for day in reversed(dated):
        write_week(tmp_path, f"weekly-{day}.json")
    bundle = collect_weekly(tmp_path)
    assert bundle == {
        "schema": 1,
        "state": "ok",
        "skipped": 0,
        "weeks": [
            {"week": day, "since": SINCE, "until": UNTIL, "metrics": {"tickets-succeeded": 3}}
            for day in dated[:KEEP]
        ],
    }


def test_the_bundle_cap_drops_the_oldest_weeks_and_counts_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for day in ("2026-11-02", "2026-11-09", "2026-11-16", "2026-11-23"):
        write_week(tmp_path, f"weekly-{day}.json")
    full = collect_weekly(tmp_path)
    two = dict(full, weeks=full["weeks"][:2], skipped=2)
    monkeypatch.setattr(dashboard_evals, "MAX_BUNDLE_BYTES", len(evals_json(two)))
    assert collect_weekly(tmp_path) == two


def test_values_are_normalized_without_skipping_the_week(tmp_path: Path) -> None:
    write_week(
        tmp_path,
        GOOD,
        '{"window": {"since": "last monday", "until": "' + UNTIL + '\\n"}, "metrics": {'
        '"tickets-succeeded": true, "poll-errors": "7", "stuck-jobs": {"unrecorded": "x"},'
        '"failed-jobs": [1], "cycle-time-p50-hours": 1e999, "cap-rate": 0.25,'
        '"first-pass-approve-rate": null, "wasted-wakes": 4, "secret-metric": 1,'
        '"max-interventions": 1' + "0" * 400 + '}}',
    )
    (week,) = collect_weekly(tmp_path)["weeks"]
    assert week == {
        "week": "2026-11-23",
        "since": None,
        "until": None,
        "metrics": {
            "tickets-succeeded": "unrecorded",
            "poll-errors": "unrecorded",
            "stuck-jobs": "unrecorded",
            "failed-jobs": "unrecorded",
            "cycle-time-p50-hours": "unrecorded",
            "cap-rate": 0.25,
            "first-pass-approve-rate": None,
            "wasted-wakes": 4,
            "max-interventions": "unrecorded",
        },
    }


def test_latest_links_text_reports_and_temps_are_not_candidates(tmp_path: Path) -> None:
    write_week(tmp_path, GOOD)
    (tmp_path / "weekly-2026-11-23.txt").write_text("report\n", encoding="utf-8")
    (tmp_path / "latest.json").symlink_to(GOOD)
    (tmp_path / "latest.txt").symlink_to("weekly-2026-11-23.txt")
    write_week(tmp_path, ".weekly-2026-11-30.json.tmp")
    write_week(tmp_path, "notes.json")
    bundle = collect_weekly(tmp_path)
    assert (weeks(bundle), bundle["skipped"]) == (["2026-11-23"], 0)


def test_no_dir_and_no_snapshots(tmp_path: Path) -> None:
    empty = {"schema": 1, "weeks": [], "skipped": 0}
    assert collect_weekly(tmp_path / "missing") == dict(empty, state="no dir")
    assert collect_weekly(write_week(tmp_path, "file")) == dict(empty, state="no dir")
    evals = tmp_path / "evals"
    evals.mkdir()
    assert collect_weekly(evals) == dict(empty, state="no snapshots")
    write_week(evals, GOOD, "[]")
    assert collect_weekly(evals) == dict(empty, state="no snapshots", skipped=1)


def test_status_json_excludes_evals(tmp_path: Path) -> None:
    evals = tmp_path / "evals"
    evals.mkdir()
    write_week(evals, GOOD)
    bundle = collect_weekly(evals)
    out = tmp_path / "out"
    snapshot = snapshot_for(tmp_path)
    paths = write_snapshot_pages(out, {**snapshot, EVALS: bundle})
    assert [path.name for path in paths[-2:]] == ["tickets.json", "evals.json"]
    assert EVALS not in json.loads((out / "status.json").read_text(encoding="utf-8"))
    written = json.loads((out / "evals.json").read_text(encoding="utf-8"))
    assert written == {**bundle, "generated_at_epoch": snapshot["generated_at_epoch"]}


def test_the_written_evals_page_shows_the_bundle_weeks(tmp_path: Path) -> None:
    evals = tmp_path / "evals"
    evals.mkdir()
    write_week(evals, BAD, report(MIDDLE))
    write_week(evals, GOOD, report(NEWEST))
    out = tmp_path / "out"
    write_snapshot_pages(out, {**snapshot_for(tmp_path), EVALS: collect_weekly(evals)})
    main = MAIN.search((out / "evals.html").read_text(encoding="utf-8")).group(1)
    assert "<table" in main
    assert headers(main) == ["2026-11-23", "2026-11-16"]


def test_evals_json_is_written_without_a_bundle(tmp_path: Path) -> None:
    snapshot = snapshot_for(tmp_path)
    write_snapshot_pages(tmp_path / "out", snapshot)
    assert json.loads((tmp_path / "out" / "evals.json").read_text(encoding="utf-8")) == {
        "schema": 1, "state": "no dir", "weeks": [], "skipped": 0, "generated_at_epoch": snapshot["generated_at_epoch"],
    }


@pytest.mark.parametrize("where", ["flag", "state", "home"])
def test_cli_snapshot_reads_the_evals_dir(tmp_path: Path, where: str) -> None:
    db, log = seeded(tmp_path)
    home = tmp_path / "home"
    dirs = {
        "flag": tmp_path / "flag",
        "state": tmp_path / "state" / "evals",
        "home": home / ".local" / "state" / "factory" / "evals",
        "next to db": tmp_path / "evals",
    }
    for directory in dirs.values():
        directory.mkdir(parents=True)
        write_week(directory, GOOD)
    write_week(dirs[where], BAD)
    out = tmp_path / "site"
    result = run_orch(
        "dashboard", "snapshot", "--db", str(db), "--log", str(log), "--out", str(out),
        *(["--evals-dir", str(dirs["flag"])] if where == "flag" else []),
        env={"HOME": str(home)} | (
            {} if where == "home" else {"ORCH_STATE_DIR": str(tmp_path / "state")}
        ),
    )
    assert result.returncode == 0, result.stderr
    assert weeks(json.loads((out / "evals.json").read_text(encoding="utf-8"))) == [
        "2026-11-23", "2026-11-16",
    ]


def test_a_parent_swapped_for_a_symlink_mid_read_cannot_redirect_a_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evals, outside, saved = (tmp_path / name for name in ("evals", "outside", "saved"))
    evals.mkdir()
    outside.mkdir()
    write_week(evals, GOOD, report({"tickets-succeeded": 1}))
    write_week(outside, GOOD, report({"tickets-succeeded": 999}))
    real_open = os.open

    def swapped_open(path, flags, *args, **kwargs):
        if not str(path).endswith(GOOD):
            return real_open(path, flags, *args, **kwargs)
        evals.rename(saved)
        evals.symlink_to(outside, target_is_directory=True)
        try:
            return real_open(path, flags, *args, **kwargs)
        finally:
            evals.unlink()
            saved.rename(evals)

    monkeypatch.setattr(dashboard_evals.os, "open", swapped_open)
    bundle = collect_weekly(evals)
    assert [week["metrics"] for week in bundle["weeks"]] in ([{"tickets-succeeded": 1}], [])


CELL = re.compile(r'<td><span class="ev-v">([^<]*)</span> <span class="chip (\w+)">([^<]*)</span></td>')


def panel(bundle: dict | None) -> str:
    return _evals({} if bundle is None else {EVALS: bundle})


def table(html: str) -> dict[str, list[tuple[str, str, str]]]:
    rows = re.findall(r'<tr><th scope="row">([^<]*)</th>(.*?)</tr>', html)
    return {label: [(value, delta, tone) for value, tone, delta in CELL.findall(cells)]
            for label, cells in rows}


def headers(html: str) -> list[str]:
    return re.findall(r'<th scope="col">([^<]*)</th>', html)


def test_every_metric_key_is_read_by_exactly_one_row() -> None:
    labels = [row.label for row in EVAL_ROWS]
    assert len(set(labels)) == len(labels)
    for key in METRIC_KEYS:
        bundle = {"state": "ok", "skipped": 0, "weeks": [
            {"week": "2026-11-23", "metrics": {key: 2}},
            {"week": "2026-11-16", "metrics": {key: 1}},
        ]}
        read = [label for label, cells in table(panel(bundle)).items() if cells[0][0] != "n/a"]
        assert len(read) == 1, (key, read)


@pytest.mark.parametrize(
    ("bundle", "words"),
    [
        (None, "No evals dir on this host."),
        ({"schema": 1, "state": "no dir", "weeks": [], "skipped": 0}, "No evals dir on this host."),
        ({"schema": 1, "state": "no snapshots", "weeks": [], "skipped": 0},
         "No weekly evals yet. The Monday report writes the first."),
    ],
    ids=["no key", "no dir", "no snapshots"],
)
def test_no_weeks_draws_the_state_and_no_table(bundle: dict | None, words: str) -> None:
    html = panel(bundle)
    assert words in html
    assert "<table" not in html
    assert "skipped" not in html


def test_the_collected_states_render(tmp_path: Path) -> None:
    assert "No evals dir on this host." in panel(collect_weekly(tmp_path / "missing"))
    write_week(tmp_path, GOOD, "[]")
    html = panel(collect_weekly(tmp_path))
    assert "No weekly evals yet." in html
    assert "1 report skipped" in html


def test_one_week_has_one_column_and_no_deltas(tmp_path: Path) -> None:
    write_week(tmp_path, GOOD, report({"tickets-succeeded": 3, "poll-errors": 4}))
    html = panel(collect_weekly(tmp_path))
    assert headers(html) == ["2026-11-23"]
    rows = table(html)
    assert [row.label for row in EVAL_ROWS] == list(rows)
    assert rows["Throughput (tickets shipped)"] == [("n/a", "n/a", "muted")]
    assert rows["Poll errors"] == [("4", "n/a", "muted")]
    assert all(cell[1:] == ("n/a", "muted") for cells in rows.values() for cell in cells)
    assert "1 week" in html


NEWEST = {
    "tickets-succeeded": 10, "first-pass-approve-rate": 0.85, "cap-rate": 0.1, "poll-errors": 5,
    "stuck-jobs": 1, "failed-jobs": 0, "cycle-time-p50-hours": 6.5, "cycle-time-p90-hours": 20,
    "max-interventions": 2, "wasted-wakes": "unrecorded", "productive-cost-usd": 41.2, "wasted-cost-usd": 3.1,
}
MIDDLE = {
    "tickets-succeeded": 7, "first-pass-approve-rate": 0.8, "cap-rate": 0.1, "poll-errors": 3,
    "stuck-jobs": 1, "failed-jobs": 1, "cycle-time-p50-hours": 7.7, "cycle-time-p90-hours": None,
    "wasted-wakes": 4, "productive-cost-usd": 38.1, "wasted-cost-usd": 5,
}
OLDEST = {"tickets-succeeded": 5, "first-pass-approve-rate": 0.9, "poll-errors": 3}


def test_several_weeks_show_values_and_toned_deltas_newest_first(tmp_path: Path) -> None:
    write_week(tmp_path, "weekly-2026-11-09.json", report(OLDEST))
    write_week(tmp_path, "weekly-2026-11-16.json", report(MIDDLE))
    write_week(tmp_path, GOOD, report(NEWEST))
    html = panel(collect_weekly(tmp_path))
    assert headers(html) == ["2026-11-23", "2026-11-16", "2026-11-09"]
    rows = table(html)
    assert rows == {
        "Throughput (tickets shipped)": [("3", "+1", "ok"), ("2", "n/a", "muted"), ("n/a", "n/a", "muted")],
        "First-pass approve rate (lifetime)": [
            ("85%", "+5 pts", "ok"), ("80%", "-10 pts", "bad"), ("90%", "n/a", "muted"),
        ],
        "Cap rate (lifetime)": [("10%", "0 pts", "muted"), ("10%", "n/a", "muted"), ("n/a", "n/a", "muted")],
        "Poll errors": [("5", "+2", "bad"), ("3", "0", "muted"), ("3", "n/a", "muted")],
        "Stuck jobs": [("1", "0", "muted"), ("1", "n/a", "muted"), ("n/a", "n/a", "muted")],
        "Failed jobs": [("0", "-1", "ok"), ("1", "n/a", "muted"), ("n/a", "n/a", "muted")],
        "Cycle time p50": [("6.5 h", "-1.2 h", "ok"), ("7.7 h", "n/a", "muted"), ("n/a", "n/a", "muted")],
        "Cycle time p90": [("20.0 h", "n/a", "muted"), ("n/a", "n/a", "muted"), ("n/a", "n/a", "muted")],
        "Max interventions": [("2", "n/a", "muted"), ("n/a", "n/a", "muted"), ("n/a", "n/a", "muted")],
        "Wasted wakes": [("n/a", "n/a", "muted"), ("4", "n/a", "muted"), ("n/a", "n/a", "muted")],
        "Productive spend": [
            ("$41.20", "+$3.10", "ok"), ("$38.10", "n/a", "muted"), ("n/a", "n/a", "muted"),
        ],
        "Wasted spend": [("$3.10", "-$1.90", "ok"), ("$5.00", "n/a", "muted"), ("n/a", "n/a", "muted")],
    }
    assert "3 weeks" in html
    assert "skipped" not in html


def test_a_rise_in_a_down_is_better_cost_reads_bad() -> None:
    bundle = {"state": "ok", "skipped": 0, "weeks": [
        {"week": "2026-11-23", "metrics": {"wasted-cost-usd": 7.5, "cycle-time-p90-hours": 30}},
        {"week": "2026-11-16", "metrics": {"wasted-cost-usd": 5, "cycle-time-p90-hours": 24.25}},
    ]}
    rows = table(panel(bundle))
    assert rows["Wasted spend"][0] == ("$7.50", "+$2.50", "bad")
    assert rows["Cycle time p90"][0] == ("30.0 h", "+5.8 h", "bad")


@pytest.mark.parametrize(("bad_files", "words"), [(1, "1 report skipped"), (2, "2 reports skipped")])
def test_the_skipped_count_is_shown_under_the_table(tmp_path: Path, bad_files: int, words: str) -> None:
    write_week(tmp_path, GOOD)
    for n in range(bad_files):
        write_week(tmp_path, f"weekly-2026-11-0{n + 1}.json", "{")
    html = panel(collect_weekly(tmp_path))
    assert words in html
    assert html.index("</table>") < html.index(words)


def test_hostile_text_is_escaped() -> None:
    hostile = '<img src=x onerror="y">'
    bundle = {"state": "ok", "skipped": 0, "weeks": [
        {"week": hostile, "metrics": {"poll-errors": hostile, "stuck-jobs": True}},
        {"week": "2026-11-16", "metrics": {"poll-errors": 1, "stuck-jobs": 1}},
    ]}
    html = panel(bundle)
    assert "<img" not in html
    assert headers(html) == [html_escape(hostile), "2026-11-16"]
    rows = table(html)
    assert rows["Poll errors"][0] == ("n/a", "n/a", "muted")
    assert rows["Stuck jobs"][0] == ("n/a", "n/a", "muted")


NAV = re.compile(r'<nav class="nav" id="nav" aria-label="Pages">(.*?)</nav>')
MAIN = re.compile(r'<main id="hud" tabindex="-1">(.*)</main>', re.S)


def evals_page(tmp_path: Path, evals_dir: Path | None) -> str:
    (tmp_path / "db").mkdir()
    snapshot = snapshot_for(tmp_path / "db")
    if evals_dir is not None:
        snapshot[EVALS] = collect_weekly(evals_dir)
    html = render_pages(snapshot)["evals.html"]
    nav = NAV.search(html).group(1)
    assert re.findall(r'href="([^"]+)" aria-current="page"', nav) == ["evals.html"]
    assert '<a href="evals.html" aria-current="page">Evals</a>' in nav
    assert '<body data-page="evals" ' in html
    main = MAIN.search(html).group(1)
    assert main.startswith('<h1 class="vh">Evals</h1>')
    return main


@pytest.mark.parametrize(
    ("where", "words"),
    [
        (None, "No evals dir on this host."),
        ("missing", "No evals dir on this host."),
        ("empty", "No weekly evals yet."),
    ],
    ids=["no key", "missing dir", "empty dir"],
)
def test_the_page_with_no_weeks_shows_the_state(tmp_path: Path, where: str | None, words: str) -> None:
    evals = None if where is None else tmp_path / "evals"
    if where == "empty":
        evals.mkdir()
    main = evals_page(tmp_path, evals)
    assert words in main
    assert "<table" not in main


def test_the_page_with_one_week_has_one_column_and_no_deltas(tmp_path: Path) -> None:
    (tmp_path / "evals").mkdir()
    write_week(tmp_path / "evals", GOOD, report(NEWEST))
    main = evals_page(tmp_path, tmp_path / "evals")
    assert headers(main) == ["2026-11-23"]
    assert {delta for cells in table(main).values() for _, delta, _ in cells} == {"n/a"}


def test_the_page_with_several_weeks_shows_every_row_newest_first(tmp_path: Path) -> None:
    (tmp_path / "evals").mkdir()
    for name, metrics in (("weekly-2026-11-09.json", OLDEST), ("weekly-2026-11-16.json", MIDDLE),
                          (GOOD, NEWEST)):
        write_week(tmp_path / "evals", name, report(metrics))
    main = evals_page(tmp_path, tmp_path / "evals")
    assert headers(main) == ["2026-11-23", "2026-11-16", "2026-11-09"]
    assert list(table(main)) == [row.label for row in EVAL_ROWS]


def flat(text: str) -> str:
    return " ".join(text.split())


def test_the_guides_name_the_report_a_delta_compares() -> None:
    guide = flat((ROOT / "README.md").read_text(encoding="utf-8"))
    pages = (ROOT / "lib/dashboard_pages/README.md").read_text(encoding="utf-8")
    bullet = re.search(r"- \*\*Evals\*\*.*?(?= - \*\*)", guide).group(0)
    row = flat(re.search(r"^\| `evals\.html`.*$", pages, re.M).group(0))
    for text in (bullet, row):
        assert "previous available report" in text
        assert "week before" not in text


def test_the_guides_say_the_evals_table_redraws_from_evals_json() -> None:
    block = re.search(r"var PAGES = \{(.*?)\n  \};", read_asset("js/common_tail.js"), re.S).group(1)
    scripted = set(re.findall(r"^\s*(\w+):", block, re.M))
    assert {name for name, _ in _PAGE_NAMES.values()} <= scripted
    guide = (ROOT / "README.md").read_text(encoding="utf-8")
    paragraph = flat(re.search(r"Each page is rendered in full on the box.*?(?=\n\n)", guide, re.S).group(0))
    assert "Evals table" in paragraph and "evals.json" in paragraph
    assert "evals.json" in dashboard.__doc__.split("Evals", 1)[1]
    intro = (ROOT / "lib/dashboard_pages/README.md").read_text(encoding="utf-8").split("## Page to module")[0]
    assert "Evals table" in flat(intro) and "evals.json" in intro
    for text in (paragraph, dashboard.__doc__, intro):
        assert "reload refreshes" not in text and "no script twin" not in text


spec = importlib.util.spec_from_file_location(
    "factory_dashboard_http_evals", ROOT / "deploy" / "factory-dashboard-http.py"
)
assert spec is not None and spec.loader is not None
server_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server_module)

BUNDLE_TEXT = '{"schema":1,"skipped":0,"state":"no dir","weeks":[]}\n'


@pytest.fixture
def site(tmp_path: Path) -> Path:
    root = tmp_path / "site"
    root.mkdir()
    (root / "evals.html").write_text("<!doctype html><title>evals</title>", encoding="utf-8")
    (root / "evals.json").write_text(BUNDLE_TEXT, encoding="utf-8")
    (tmp_path / "outside.json").write_text('{"secret": "outside"}', encoding="utf-8")
    return root


@pytest.fixture
def get(site: Path):
    server = server_module.make_server("127.0.0.1", 0, site)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(path: str, method: str = "GET") -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            connection.putrequest(method, path, skip_accept_encoding=True)
            connection.endheaders()
            response = connection.getresponse()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
        finally:
            connection.close()

    yield request
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.mark.parametrize(("name", "content_type"), [
    ("evals.html", "text/html; charset=utf-8"), ("evals.json", "application/json"),
])
def test_the_server_serves_the_evals_page_and_bundle(site: Path, get, name: str, content_type: str) -> None:
    status, headers, body = get(f"/{name}")
    assert (status, headers["content-type"]) == (200, content_type)
    assert body == (site / name).read_bytes()
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    head_status, head_headers, head_body = get(f"/{name}", method="HEAD")
    assert (head_status, head_body, head_headers["content-length"]) == (200, b"", str(len(body)))


def test_the_server_refuses_the_weekly_reports_and_any_other_evals_path(site: Path, get) -> None:
    (site / "evals").mkdir()
    for directory in (site, site / "evals"):
        write_week(directory, GOOD)
        (directory / "latest.json").symlink_to(GOOD)
    write_week(site, ".evals.json.push-tmp")
    for path in ("/weekly-2026-11-23.json", "/evals/weekly-2026-11-23.json", "/latest.json", "/evals/",
                 "/evals", "/static/evals.json", "/evals.json/x", "/.evals.json.push-tmp", "/evals.json%2f",
                 "/%65vals.json/..", "/evals.JSON"):
        assert get(path)[0] == 404, path


@pytest.mark.parametrize("link", ["symlink", "hard link"])
def test_the_server_refuses_a_linked_evals_json(site: Path, get, link: str) -> None:
    (site / "evals.json").unlink()
    outside = site.parent / "outside.json"
    if link == "symlink":
        (site / "evals.json").symlink_to(outside)
    else:
        os.link(outside, site / "evals.json")
    status, _, body = get("/evals.json")
    assert status == 404
    assert b"outside" not in body


@pytest.mark.parametrize("session", [None, "signed-in"])
def test_evals_json_is_401_json_when_signed_out_like_the_other_snapshot_json(site: Path, session) -> None:
    (site / "status.json").write_text("{}\n", encoding="utf-8")
    (site / "tickets.json").write_text("{}\n", encoding="utf-8")
    handler = object.__new__(server_module.SnapshotHandler)
    config = server_module.ActionsConfig("synthetic", "human", frozenset(), require_auth_reads=True)
    handler.server = SimpleNamespace(
        actions=SimpleNamespace(config=config, current_with_renewal=lambda *_: (session, False)),
        root_fd=server_module.open_root(site),
    )
    handler.client_address = ("127.0.0.1", 0)
    handler._action_client = lambda: "127.0.0.1"
    handler._cookie = lambda _: None
    out: list[tuple[int, str, bytes]] = []
    handler._send = lambda code, data, content_type, **_: out.append((code, content_type, data))
    handler._json = lambda code, data: out.append((code, "application/json", json.dumps(data).encode()))
    try:
        for path in ("/status.json", "/tickets.json", "/evals.json", "/evals.html"):
            handler.path = path
            handler._serve(body=True)
    finally:
        os.close(handler.server.root_fd)
    if session is None:
        assert [(code, content_type) for code, content_type, _ in out] == [
            (401, "application/json")] * 3 + [(401, "text/html; charset=utf-8")]
        assert json.loads(out[2][2]) == {"error": "no-session"}
    else:
        assert [code for code, _, _ in out] == [200] * 4
        assert out[2] == (200, "application/json", BUNDLE_TEXT.encode())


EVALS_IDS = [*BAR, "hud", "evals"]
ROUNDING = {
    "tickets-succeeded": 1_234_567, "first-pass-approve-rate": 0.125, "cap-rate": 0.005, "poll-errors": 2,
    "cycle-time-p50-hours": 0.25, "cycle-time-p90-hours": 1234.05, "productive-cost-usd": 12_345.675,
    "wasted-cost-usd": 0.125, "max-interventions": 0,
}
ROUNDING_BEFORE = {
    "tickets-succeeded": 1_000, "first-pass-approve-rate": 0.13, "cap-rate": 0.0049, "poll-errors": 2.5,
    "cycle-time-p50-hours": 0.3, "cycle-time-p90-hours": 1234.1, "productive-cost-usd": 12_345.68,
    "wasted-cost-usd": 0.1300001, "max-interventions": "unrecorded",
}
HUGE = {
    "tickets-succeeded": 10**22, "poll-errors": 10**21, "stuck-jobs": 2**53 + 1, "failed-jobs": 1.5e300,
    "first-pass-approve-rate": 1e300, "cap-rate": 1.7e308, "cycle-time-p50-hours": 1.7e308,
    "cycle-time-p90-hours": 9.87654321e25, "productive-cost-usd": 1e307, "wasted-cost-usd": 1.2345678901234567e23,
}
HUGE_BEFORE = {
    "tickets-succeeded": 1, "poll-errors": 1, "stuck-jobs": 1, "failed-jobs": -1.5e300,
    "cycle-time-p50-hours": -1.7e308, "wasted-cost-usd": 1,
}
TIES = {
    "productive-cost-usd": 70368744177664.125, "poll-errors": 10**21, "cycle-time-p50-hours": 1125899906842624.25,
    "wasted-cost-usd": 0.125,
}
TIES_BEFORE = {"productive-cost-usd": 0, "poll-errors": 0, "cycle-time-p50-hours": 0, "wasted-cost-usd": 0}
def week(name: object, metrics: object, since: object = None, until: object = None) -> dict:
    return {"week": name, "since": since, "until": until, "metrics": metrics}


def without(item: dict, key: str) -> dict:
    return {k: v for k, v in item.items() if k != key}


PARITY = {
    "no dir": {"schema": 1, "state": "no dir", "weeks": [], "skipped": 0},
    "no snapshots, skipped": {"schema": 1, "state": "no snapshots", "weeks": [], "skipped": 2},
    "one week": {"schema": 1, "state": "ok", "skipped": 1, "weeks": [week("2026-11-23", NEWEST, SINCE, UNTIL)]},
    "several weeks": {"schema": 1, "state": "ok", "skipped": 0, "weeks": [
        week("2026-11-23", NEWEST, SINCE, UNTIL), week("2026-11-16", MIDDLE), week("2026-11-09", OLDEST)]},
    "rounding and thousands": {"schema": 1, "state": "ok", "skipped": 0, "weeks": [
        week("2026-11-23", ROUNDING), week("2026-11-16", ROUNDING_BEFORE)]},
    "past float precision and range": {"schema": 1, "state": "ok", "skipped": 0, "weeks": [
        week("2026-11-23", HUGE), week("2026-11-16", HUGE_BEFORE)]},
    "ties at the last digit": {"schema": 1, "state": "ok", "skipped": 0, "weeks": [
        week("2026-11-23", TIES), week("2026-11-16", TIES_BEFORE)]},
}


def served(bundle: dict, epoch: float) -> dict:
    return {"status": 200, "body": {**json.loads(evals_json(bundle)), "generated_at_epoch": epoch}}


def section_inner(bundle: dict) -> str:
    html = panel(bundle)
    head = '<section class="panel" id="evals" aria-label="Weekly evals">'
    assert html.startswith(head) and html.endswith("</section>")
    return html[len(head):-len("</section>")]


@needs_node
@pytest.mark.parametrize("bundle", PARITY.values(), ids=PARITY)
def test_the_script_draws_the_same_table_as_the_server(tmp_path: Path, bundle: dict) -> None:
    snapshot = snapshot_for(tmp_path / "db")
    out = _run_page(tmp_path / "page", snapshot, "evals", EVALS_IDS,
                    evals=served(bundle, snapshot["generated_at_epoch"]))
    assert out["evals"] == 1
    assert out["html"]["evals"] == section_inner(bundle)


def test_values_and_deltas_round_half_up_with_thousands_separators() -> None:
    rows = table(panel(PARITY["rounding and thousands"]))
    assert {label: cells[0] for label, cells in rows.items() if cells[0][0] != "n/a"} == {
        "Throughput (tickets shipped)": ("1,233,567", "n/a", "muted"),
        "First-pass approve rate (lifetime)": ("13%", "-1 pts", "bad"),
        "Cap rate (lifetime)": ("1%", "0 pts", "muted"),
        "Poll errors": ("2", "0", "muted"),
        "Cycle time p50": ("0.3 h", "0.0 h", "muted"),
        "Cycle time p90": ("1,234.1 h", "0.0 h", "muted"),
        "Max interventions": ("0", "n/a", "muted"),
        "Productive spend": ("$12,345.68", "-$0.01", "bad"),
        "Wasted spend": ("$0.13", "-$0.01", "ok"),
    }


POLLING = (
    layout.HARNESS.replace("function act(actions) {", "async function act(actions) {")
    .replace("const intervals = [];", "const intervals = [];\nconst realSetTimeout = setTimeout;\nconst polls = [];\n"
             "global.setTimeout = (fn, ms) => fn && fn.name === 'poll' ? polls.push(fn) : realSetTimeout(fn, ms);")
    .replace("let evalsFetches = 0;", "let evalsFetches = 0;\nlet evalsServed = null;\nconst heldEvals = [];")
    .replace(
        "    const answer = evalsAnswer || {status: 404, body: null};\n"
        "    return Promise.resolve(",
        "    const answer = evalsServed || evalsAnswer || {status: 404, body: null};\n"
        "    const reply = {ok: answer.status === 200, status: answer.status, json: () => Promise.resolve(answer.body)};\n"
        "    if (answer.hold) return new Promise((resolve, reject) => heldEvals.push(\n"
        "      {release: () => resolve(reply), drop: () => reject(new Error('dropped'))}));\n"
        "    return Promise.resolve(",
    )
    .replace(
        "    } else if (kind === 'record') {",
        "    } else if (kind === 'poll') {\n"
        "      Object.assign(status, arg);\n"
        "      polls.splice(0).forEach(fn => fn());\n"
        "      await new Promise(resolve => realSetTimeout(resolve, 20));\n"
        "    } else if (kind === 'serve') {\n"
        "      evalsServed = arg;\n"
        "    } else if (kind === 'drop' || kind === 'release') {\n"
        "      heldEvals.splice(0).forEach(held => held[kind]());\n"
        "      await new Promise(resolve => realSetTimeout(resolve, 20));\n"
        "    } else if (kind === 'record') {",
    )
    .replace("setTimeout(() => {\n  act(JSON.parse(actionsText));",
             "realSetTimeout(async () => {\n  await act(JSON.parse(actionsText));")
)

NOV_16 = {"schema": 1, "state": "ok", "skipped": 0, "weeks": [week("2026-11-16", MIDDLE)]}
BOTH_WEEKS = {"schema": 1, "state": "ok", "skipped": 0, "weeks": [
    week("2026-11-23", NEWEST, SINCE, UNTIL), week("2026-11-16", MIDDLE)]}


def polled(tmp_path: Path, actions: list, first: dict) -> tuple[dict, float]:
    snapshot = snapshot_for(tmp_path / "db")
    epoch = snapshot["generated_at_epoch"]
    assert all(fragment in POLLING for fragment in ("heldEvals.push", "evalsServed = arg", "await act("))
    out = layout.run_harness(
        tmp_path / "page", POLLING, render_static_assets(snapshot)["app.js"], snapshot, "evals", EVALS_IDS,
        actions(epoch) if callable(actions) else actions, evals=first(epoch) if callable(first) else first,
    )
    return out, epoch


@needs_node
def test_the_script_fetches_evals_json_once_per_new_snapshot(tmp_path: Path) -> None:
    out, _ = polled(tmp_path, lambda epoch: [
        ["advance", 1], ["poll", {}], ["serve", served(NOV_16, epoch + 30)],
        ["poll", {"generated_at_epoch": epoch + 30}], ["advance", 1], ["poll", {}],
        ["serve", served(PARITY["one week"], epoch + 60)], ["poll", {"generated_at_epoch": epoch + 60}],
        ["advance", 1], ["poll", {}],
    ], lambda epoch: served(NOV_16, epoch))
    assert out["fetches"] == 6
    assert out["evals"] == 3
    assert out["html"]["evals"] == section_inner(PARITY["one week"])


@needs_node
def test_a_bundle_older_than_the_snapshot_is_fetched_again_until_the_publish_lands(tmp_path: Path) -> None:
    out, _ = polled(tmp_path, lambda epoch: [
        ["poll", {"generated_at_epoch": epoch + 30}],
        ["serve", served(BOTH_WEEKS, epoch + 30)], ["poll", {}], ["poll", {}],
    ], lambda epoch: served(NOV_16, epoch))
    assert out["html"]["evals"] == section_inner(BOTH_WEEKS)
    assert out["evals"] == 3


@needs_node
def test_a_bundle_newer_than_the_snapshot_is_drawn_and_not_fetched_again(tmp_path: Path) -> None:
    out, _ = polled(tmp_path, [["poll", {}], ["poll", {}]], lambda epoch: served(BOTH_WEEKS, epoch + 30))
    assert out["html"]["evals"] == section_inner(BOTH_WEEKS)
    assert out["evals"] == 1


@needs_node
def test_a_published_bundle_is_drawn_when_a_status_only_push_lands_before_the_next_poll(tmp_path: Path) -> None:
    out, _ = polled(tmp_path, lambda epoch: [
        ["serve", served(BOTH_WEEKS, epoch + 30)], ["poll", {"generated_at_epoch": epoch + 31}],
        ["poll", {}], ["poll", {}], ["poll", {}],
    ], lambda epoch: served(NOV_16, epoch))
    assert out["html"]["evals"] == section_inner(BOTH_WEEKS)


@needs_node
@pytest.mark.parametrize("end", ["drop", "release"], ids=["failed", "answered with the older bundle"])
def test_a_held_fetch_for_an_older_snapshot_leaves_the_newer_one_to_load(tmp_path: Path, end: str) -> None:
    out, _ = polled(tmp_path, lambda epoch: [
        ["poll", {"generated_at_epoch": epoch + 30}], [end, None],
        ["serve", served(BOTH_WEEKS, epoch + 30)], ["poll", {}], ["poll", {}],
    ], lambda epoch: {**served(NOV_16, epoch), "hold": True})
    assert out["html"]["evals"] == section_inner(BOTH_WEEKS)
    assert out["evals"] == 2


PRECISE = 1800000001.1238
GENERATED = re.compile(r'<body data-page="evals" data-generated="([^"]*)"')


def published(tmp_path: Path, epoch: float) -> tuple[Path, dict]:
    root = tmp_path / repr(epoch)
    (root / "evals").mkdir(parents=True)
    write_week(root / "evals", BAD, report(MIDDLE))
    write_week(root / "evals", GOOD, report(NEWEST))
    bundle = collect_weekly(root / "evals")
    snapshot = {**snapshot_for(root / "db"), "generated_at_epoch": epoch, EVALS: bundle}
    write_snapshot_pages(root / "site", snapshot)
    return root / "site", bundle


def polled_published(tmp_path: Path, page: Path, site: Path) -> dict:
    attribute = GENERATED.search((page / "evals.html").read_text(encoding="utf-8")).group(1)
    stock = "body.attrs['data-generated'] = '1800000000.000';"
    assert stock in POLLING
    harness = POLLING.replace(stock, f"body.attrs['data-generated'] = {json.dumps(attribute)};", 1)
    status, bundle = (json.loads((site / name).read_text(encoding="utf-8")) for name in ("status.json", "evals.json"))
    return layout.run_harness(
        tmp_path / "page", harness, (site / "app.js").read_text(encoding="utf-8"), status, "evals", EVALS_IDS,
        [["poll", {}]] * 4, evals={"status": 200, "body": bundle},
    )


@needs_node
def test_a_bundle_published_at_a_sub_millisecond_epoch_is_fetched_once_and_drawn(tmp_path: Path) -> None:
    site, bundle = published(tmp_path, PRECISE)
    out = polled_published(tmp_path, site, site)
    assert out["evals"] == 1
    assert out["html"]["evals"] == section_inner(bundle)


@needs_node
def test_a_bundle_as_new_as_the_snapshot_is_fetched_once_when_the_page_is_newer(tmp_path: Path) -> None:
    site, _ = published(tmp_path, PRECISE)
    page, _ = published(tmp_path, PRECISE + 30)
    out = polled_published(tmp_path, page, site)
    assert out["evals"] == 1


def test_numbers_past_float_precision_and_range_render_as_a_float_would() -> None:
    rows = table(panel(PARITY["past float precision and range"]))
    assert {label: cells[0] for label, cells in rows.items()} == {
        "Throughput (tickets shipped)": ("10,000,000,000,000,000,000,000", "n/a", "muted"),
        "First-pass approve rate (lifetime)": (f"{1e302:,.0f}%", "n/a", "muted"),
        "Cap rate (lifetime)": ("n/a", "n/a", "muted"),
        "Poll errors": ("1,000,000,000,000,000,000,000", "+1,000,000,000,000,000,000,000", "bad"),
        "Stuck jobs": ("9,007,199,254,740,992", "+9,007,199,254,740,991", "bad"),
        "Failed jobs": (f"{1.5e300:,.0f}", f"+{3e300:,.0f}", "bad"),
        "Cycle time p50": ("n/a", "n/a", "muted"),
        "Cycle time p90": ("98,765,432,099,999,996,838,936,576.0 h", "n/a", "muted"),
        "Max interventions": ("n/a", "n/a", "muted"),
        "Wasted wakes": ("n/a", "n/a", "muted"),
        "Productive spend": ("n/a", "n/a", "muted"),
        "Wasted spend": ("$123,456,789,012,345,669,025,792.00", "+$123,456,789,012,345,669,025,792.00", "bad"),
    }


DROP = object()
MALFORMED = {
    "week without a name": {"weeks": [without(week("2026-11-23", {}), "week")]},
    "week name not text": {"weeks": [week(7, {})]},
    "week not an object": {"weeks": [None]},
    "week a list": {"weeks": [["2026-11-23"]]},
    "metrics a list": {"weeks": [week("2026-11-23", [])]},
    "metrics null": {"weeks": [week("2026-11-23", None)]},
    "skipped negative": {"skipped": -1},
    "skipped a fraction": {"skipped": 1.5},
    "skipped text": {"skipped": "2"},
    "unknown state": {"state": "fine"},
    "unknown schema": {"schema": 2},
    "no epoch": {"generated_at_epoch": None},
    "a list": None,
    "week name empty": {"weeks": [week("", {})]},
    "week name not a date": {"weeks": [week("2026-02-30", {})]},
    "week name hostile": {"weeks": [week('<img src=x onerror="y">&\'', {})]},
    "weeks oldest first": {"weeks": [week("2026-11-16", {}), week("2026-11-23", {})]},
    "a week twice": {"weeks": [week("2026-11-23", {}), week("2026-11-23", {})]},
    "more weeks than kept": {"weeks": [
        week((date(2026, 11, 23) - timedelta(weeks=n)).isoformat(), {}) for n in range(KEEP + 1)]},
    "metric text": {"weeks": [week("2026-11-23", {"poll-errors": "<b>"})]},
    "metric true": {"weeks": [week("2026-11-23", {"stuck-jobs": True})]},
    "metric unknown": {"weeks": [week("2026-11-23", {"poll-error": 1})]},
    "since not a time": {"weeks": [week("2026-11-23", {}, since="monday")]},
    "until not text": {"weeks": [week("2026-11-23", {}, until=5)]},
    "week without since": {"weeks": [without(BOTH_WEEKS["weeks"][0], "since"), BOTH_WEEKS["weeks"][1]]},
    "week without until": {"weeks": [without(BOTH_WEEKS["weeks"][0], "until"), BOTH_WEEKS["weeks"][1]]},
    "week with an unknown key": {"weeks": [{**BOTH_WEEKS["weeks"][0], "note": "x"}, BOTH_WEEKS["weeks"][1]]},
    "bundle with an unknown key": {"note": "x"},
    "bundle without skipped": {"skipped": DROP},
    "no dir with weeks": {"state": "no dir"},
    "no snapshots with weeks": {"state": "no snapshots"},
    "ok without weeks": {"weeks": []},
    "no dir with skipped": {"state": "no dir", "weeks": [], "skipped": 1},
}


@needs_node
@pytest.mark.parametrize("change", MALFORMED.values(), ids=MALFORMED)
def test_a_malformed_bundle_keeps_the_drawn_table(tmp_path: Path, change: dict | None) -> None:
    def bad(epoch: float) -> dict:
        body = {k: v for k, v in {**served(BOTH_WEEKS, epoch + 30)["body"], **(change or {})}.items() if v is not DROP}
        return {"status": 200, "body": [body] if change is None else body}

    out, _ = polled(tmp_path, lambda epoch: [
        ["serve", bad(epoch)], ["poll", {"generated_at_epoch": epoch + 30}], ["poll", {}], ["poll", {}],
    ], lambda epoch: served(NOV_16, epoch))
    assert out["html"]["evals"] == section_inner(NOV_16)
    assert out["evals"] == 2


@needs_node
@pytest.mark.parametrize("answer", [
    {"status": 401, "body": PARITY["one week"]},
    {"status": 404, "body": PARITY["one week"]},
    {"status": 200, "body": None},
    {"status": 200, "body": {"state": "ok", "skipped": 0, "weeks": "x"}},
    {"status": 200, "body": {"state": "ok", "skipped": 0, "weeks": [{"week": "2026-11-23"}]}},
], ids=["signed out", "missing", "null", "weeks not a list", "week without metrics"])
def test_a_failed_evals_fetch_leaves_the_server_render(tmp_path: Path, answer: dict) -> None:
    snapshot = snapshot_for(tmp_path / "db")
    out = _run_page(tmp_path / "page", snapshot, "evals", EVALS_IDS, evals=answer, actions=[["advance", 1]])
    assert out["evals"] == 1
    assert out["html"]["evals"] == ""
