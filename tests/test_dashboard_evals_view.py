from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from datetime import date, timedelta
from html import escape as html_escape
from pathlib import Path

import pytest

from lib import dashboard_evals
from lib.dashboard import EVALS, write_snapshot_pages
from lib.dashboard_evals import KEEP, METRIC_KEYS, collect_weekly, evals_json
from lib.dashboard_page import render_pages
from lib.dashboard_pages.evals import EVAL_ROWS, _evals
from tests.test_dashboard import run_orch, seeded, snapshot_for

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
        '"first-pass-approve-rate": null, "wasted-wakes": 4, "secret-metric": 1}}',
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
    paths = write_snapshot_pages(out, {**snapshot_for(tmp_path), EVALS: bundle})
    assert [path.name for path in paths[-2:]] == ["tickets.json", "evals.json"]
    assert EVALS not in json.loads((out / "status.json").read_text(encoding="utf-8"))
    assert json.loads((out / "evals.json").read_text(encoding="utf-8")) == bundle


def test_evals_json_is_written_without_a_bundle(tmp_path: Path) -> None:
    write_snapshot_pages(tmp_path / "out", snapshot_for(tmp_path))
    assert json.loads((tmp_path / "out" / "evals.json").read_text(encoding="utf-8")) == {
        "schema": 1, "state": "no dir", "weeks": [], "skipped": 0,
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
    "max-interventions": 2, "wasted-wakes": "x", "productive-cost-usd": 41.2, "wasted-cost-usd": 3.1,
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
