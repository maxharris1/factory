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
from lib.dashboard import collect_snapshot
from lib.dashboard_pages import lanes, tickets
from tests.test_dashboard import NOW, make_db, seeded

PAGE_FILES = {
    "index.html": "test_lanes_golden",
    "job.html": "test_tickets_golden",
    "activity.html": "test_activity_golden",
    "efficiency.html": "test_efficiency_golden",
    "health.html": "test_system_golden",
    "ticket.html": "test_ticket_golden",
}

MARKERS = {"style": "\0style\0", "main": "\0main\0", "script": "\0script\0"}

_PAGE = re.compile(
    r'(.*?<style>)(.*?)(</style></head>.*?<main id="hud" tabindex="-1">)(.*?)'
    r"(</main>.*?<script>)(.*)(</script>\n</body></html>\n)",
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
    hostile = copy.deepcopy(seeded_snapshot)
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
    assert match, "the page has a style, a main and a script"
    head, style, middle, main, tail, script, end = match.groups()
    chrome = head + MARKERS["style"] + middle + MARKERS["main"] + tail + MARKERS["script"] + end
    parts = {"style": style, "main": main, "script": script, "chrome": chrome}
    rebuilt = chrome
    for key, marker in MARKERS.items():
        rebuilt = rebuilt.replace(marker, parts[key])
    assert rebuilt == html, "the split puts the page back together byte for byte"
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
