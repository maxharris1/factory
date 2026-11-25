"""The weekly eval reports collected into evals.json (#1608)."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

import pytest

from lib import dashboard_evals
from lib.dashboard import EVALS, write_snapshot_pages
from lib.dashboard_evals import KEEP, collect_weekly, evals_json
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


def _escape(directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_week(directory, BAD)
    real = os.path.realpath
    monkeypatch.setattr(
        dashboard_evals.os.path,
        "realpath",
        lambda path: "/elsewhere/" + BAD if str(path).endswith(BAD) else real(path),
    )


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
    "escaped parent": _escape,
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


@pytest.mark.parametrize("where", ["flag", "state", "db"])
def test_cli_snapshot_reads_the_evals_dir(tmp_path: Path, where: str) -> None:
    db, log = seeded(tmp_path)
    dirs = {"flag": tmp_path / "flag", "state": tmp_path / "state" / "evals", "db": tmp_path / "evals"}
    for directory in dirs.values():
        directory.mkdir(parents=True)
        write_week(directory, GOOD)
    write_week(dirs[where], BAD)
    out = tmp_path / "site"
    result = run_orch(
        "dashboard", "snapshot", "--db", str(db), "--log", str(log), "--out", str(out),
        *(["--evals-dir", str(dirs["flag"])] if where == "flag" else []),
        env={} if where == "db" else {"ORCH_STATE_DIR": str(tmp_path / "state")},
    )
    assert result.returncode == 0, result.stderr
    assert weeks(json.loads((out / "evals.json").read_text(encoding="utf-8"))) == [
        "2026-11-23", "2026-11-16",
    ]
