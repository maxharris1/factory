from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from unittest import mock

import lib.dashboard_page as page
import lib.health
from lib.dashboard import EVALS, collect_snapshot
from lib.dashboard_evals import collect_weekly
from lib.dashboard_pages import lanes, tickets
from tests.test_dashboard import NOW, make_db, seeded

PAGE_FILES = {
    "index.html": "test_lanes_golden",
    "job.html": "test_tickets_golden",
    "activity.html": "test_activity_golden",
    "efficiency.html": "test_efficiency_golden",
    "evals.html": "test_evals_golden",
    "health.html": "test_system_golden",
    "settings.html": "test_settings_golden",
    "ticket.html": "test_ticket_golden",
}

MARKERS = {"main": "\0main\0"}

WEEKS = {
    "2026-11-09": {"tickets-succeeded": 5, "first-pass-approve-rate": 0.9, "poll-errors": 3},
    "2026-11-16": {"tickets-succeeded": 7, "first-pass-approve-rate": 0.8, "poll-errors": 3,
                   "cycle-time-p50-hours": 7.7, "wasted-cost-usd": 5},
    "2026-11-23": {"tickets-succeeded": 10, "first-pass-approve-rate": 0.85, "poll-errors": 5,
                   "cycle-time-p50-hours": 6.5, "wasted-cost-usd": 3.1},
}
HOSTILE_EVALS = {"schema": 1, "state": "ok", "skipped": 2, "weeks": [
    {"week": "</script><script>x()</script>", "since": None, "until": None,
     "metrics": {"poll-errors": "<img src=x onerror=y>", "stuck-jobs": "unrecorded", "failed-jobs": None}},
    {"week": "2026-11-16", "since": None, "until": None, "metrics": {"poll-errors": 1, "stuck-jobs": 2}},
]}

_PAGE = re.compile(
    r'(.*?<link rel="stylesheet" href="/app\.css"></head>.*?<main id="hud" tabindex="-1">)(.*?)'
    r'(</main>.*?<script src="/app\.js" defer></script>\n</body></html>\n)',
    re.S,
)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def page_name(file: str) -> str:
    return next(name for name, stem in PAGE_FILES.items() if stem == Path(file).stem)


def pinned_version():
    return mock.patch.object(lib.health, "factory_version", lambda root=None: "0123456789ab")


def snapshots(root: Path) -> dict[str, dict]:
    with pinned_version():
        return _snapshots(root)


def _snapshots(root: Path) -> dict[str, dict]:
    (root / "seeded").mkdir(parents=True)
    db, log = seeded(root / "seeded")
    os.utime(log, (NOW - 30, NOW - 30))
    seeded_snapshot = collect_snapshot(db, log, now=NOW)
    (root / "evals").mkdir()
    for day, metrics in WEEKS.items():
        (root / "evals" / f"weekly-{day}.json").write_text(json.dumps({"metrics": metrics}), encoding="utf-8")
    seeded_snapshot[EVALS] = collect_weekly(root / "evals")
    hostile = copy.deepcopy(seeded_snapshot)
    hostile[EVALS] = HOSTILE_EVALS
    hostile["attention"] = {"count": 2, "items": [
        {"kind": "down", "repo": "", "ticket_id": "", "component": "<b>x</b>",
         "link": "javascript:alert(1)", "detail": "down \"now\""},
        {"kind": "stuck", "repo": "</script><script>x()</script>", "ticket_id": "'1'",
         "detail": "<img src=x onerror=y>"}]}
    empty = root / "empty"
    empty.mkdir()
    make_db(empty / "orch.db")
    (empty / "orch.log").write_text("")
    os.utime(empty / "orch.log", (NOW - 30, NOW - 30))
    return {
        "seeded": seeded_snapshot,
        "hostile": hostile,
        "empty": collect_snapshot(empty / "orch.db", empty / "orch.log", now=NOW),
    }


def render(snapshot: dict) -> dict[str, str]:
    with pinned_version():
        return page.render_pages(snapshot)


def served(root: Path) -> dict[str, dict[str, str]]:
    return {case: render(snapshot) for case, snapshot in snapshots(root).items()}


def split(html: str) -> dict[str, str]:
    match = _PAGE.fullmatch(html)
    assert match, "the page links static css/js and has a main"
    head, main, tail = match.groups()
    chrome = head + MARKERS["main"] + tail
    parts = {"main": main, "chrome": chrome}
    assert chrome.replace(MARKERS["main"], main) == html, "the split puts the page back together byte for byte"
    return parts


def substitutions(chart: dict | None) -> dict[str, str]:
    return {
        "__LANES__": json.dumps(lanes.lanes_table(), separators=(",", ":")),
        "__TELEMETRY__": page.TELEMETRY_SCRIPT,
        "__PIXELSTRIP__": page.px_script(),
        "__SHELL__": json.dumps(page.shell_table(), separators=(",", ":")),
        "__POLL_MS__": str(page.POLL_MS),
        "__FLOWMAP__": page._flowmap_json(chart),
        "__BOARD__": json.dumps(tickets.board_constants(), separators=(",", ":")).replace("<", "\\u003c"),
        "__FONTS__": page.font_faces(),
    }


def page_digests(name: str, assets: dict[str, str], root: Path) -> dict[str, str]:
    found = {key: sha(text) for key, text in assets.items()}
    for case, pages in served(root).items():
        parts = split(pages[name])
        found[f"{case}/main"] = sha(parts["main"])
        found[f"{case}/chrome"] = sha(parts["chrome"])
    return found


def record(digests: Callable[[Path], dict[str, str]]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        for key, value in digests(Path(tmp)).items():
            sys.stdout.write(f'    "{key}": "{value}",\n')
