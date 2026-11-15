"""#271: lib/dashboard_page.py split into lib/dashboard_pages/ moved no byte.

The digests were recorded from the single-file module on main (595745d).
A change that alters the served pages on purpose records them again with
`python -m tests.test_dashboard_pages_golden`.
"""

from __future__ import annotations

import copy
import hashlib
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

import lib.dashboard_page as page
import lib.health
from lib.dashboard import collect_snapshot
from tests.test_dashboard import NOW, make_db, seeded

FACADE = Path(__file__).resolve().parent.parent / "lib" / "dashboard_page.py"

GOLDEN = {
    "CSS": "3ddca7e9708dbcb2a19836de98ebd7b53ddf9c3de5c2a0f1919097cea489d932",
    "SCRIPT": "06546a113bf7b7bbbecf6a663c6c8329b44f19b03cd0cb5b80628f30920a0b8e",
    "PX_SCRIPT": "d3c012a9dcc3b97338a731e172ea14cd5d25dfc59c7fab2fdd867bb974b9dc1b",
    "page_script": "ce6728b936d495ab53e22d44cf8d4e61b3736cab3ba6769081a56c957e4535ef",
    "seeded/index.html": "e9ed3bd42dc01e1ae5ac4b11628963a9ed05bade2e1c05fa51ec544ff41c4576",
    "seeded/job.html": "a044162215341fc44c03689eb928cca643d03cf14b52e26c594a5db1aa4930fb",
    "seeded/activity.html": "eaa15137e9cfaf728c8b15a2f2ba84d436a1b00cbadbf0ff599785c2e0004648",
    "seeded/efficiency.html": "d3d7d2b6138ad9557dc805bf44c26068e7ab84728854183f5b2359f2b006cab3",
    "seeded/health.html": "b39f30ff9ca8d8fde974344bc4c3df9c98adf23074ce18bfda729a2333c3b95c",
    "hostile/index.html": "6110a388243cd9dba6c4f052d219db49c88cf7e730e5d49f154139793c6bd8be",
    "hostile/job.html": "e11de99849c452d56411f771d972a06f25bd3851d6cff67ec4d1db5702316593",
    "hostile/activity.html": "00bbbbcf00d5915d860f8bbe426395d36ea23fb2b063a395e1de9e4b034a2d33",
    "hostile/efficiency.html": "cfc1371ce1fa2bfad44439247c7c239a98158340e114be0730afe6e71451ce32",
    "hostile/health.html": "30241f335762c0a28bcd2d62846d16724d78839d7c223c74eada9149ba8f13de",
    "empty/index.html": "4d13b14ed13ac9498b00c2f896367dbf3a7f22f5481d51581bb62abd73af6bed",
    "empty/job.html": "7ed6d7787f013ef771ce7fe7496f8e1f793cbd35afb8d26535efc998a22e3d4d",
    "empty/activity.html": "599d0b11d8b737993303802cac0644ac973a939b44716a9c09f1045e1963b07e",
    "empty/efficiency.html": "a0098052c98a679dad2c15e0557320c8f1e0450b3e5b8a6467bb53561aee3c1e",
    "empty/health.html": "206fa8ec9452c83c0041402c857373ec5238ecf0c28e96d531f28d0d934a00b1",
}

NAMES = (
    "POLL_MS", "FONT_DIR", "FONTS", "CSS", "SCRIPT", "PX_SCRIPT", "font_faces", "px_script",
    "page_script", "PAGES", "_PAGE_NAMES", "LIST_MAX", "_BOTS", "_e", "PX_EFFECTS", "PX_ONESHOTS",
    "PX_BOT_TINTS", "PX_FALLBACK", "_PX_BAD", "PX_BROKEN", "_PX_LINK", "PX_STATE", "PX_ACTIONS",
    "_PX_HEX", "px_table", "px_state", "_px_progress", "px_frame", "_fixed3", "px_strip",
    "px_job_strip", "duration", "_num", "_short", "_ticket", "_job_label", "_job_kind", "anchor",
    "_b", "_span", "_bot", "_chip", "_panel", "_feed", "_when", "_topbar", "header_load", "_band",
    "_attention_strip", "STATUS_LINKS", "STATUS_ABSENT_LINK", "shell_table", "version_drift",
    "_segment", "_count", "status_segments", "_statusline", "_kpis", "_FEEDER_STATE", "_feeder",
    "_linear_webhook", "_JOB_STATE", "_state_chip", "_slot", "lane_devices", "_lanes", "_queue",
    "_ATTENTION", "_HEALTH_LINK", "_health_link", "_attention", "_HANDOFF", "_handoffs",
    "_VERDICT", "_verdicts", "_td", "_tickets", "_log", "_POLL_STATE", "_BUDGET_STATE",
    "_health", "_HEALTH_STATE", "_ANCHOR", "_anchor_attr", "_components", "_no_waiter",
    "_savings", "_active_jobs", "_jobs", "_legend", "_TRAIL_LEGEND", "_meta", "_job_article",
    "_jobview", "_flow_template", "_PANELS", "_page", "render_pages",
    "path_geometry", "render_path_svg", "render_svg", "TELEMETRY_CSS", "TELEMETRY_SCRIPT",
    "job_block", "rollup_panel", "CODE_COMPONENTS", "label",
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _snapshots(root: Path) -> dict[str, dict]:
    (root / "seeded").mkdir()
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


def digests(root: Path) -> dict[str, str]:
    with mock.patch.object(lib.health, "factory_version", lambda root=None: "0123456789ab"):
        return _digests(root)


def _digests(root: Path) -> dict[str, str]:
    found = {
        "CSS": _sha(page.CSS),
        "SCRIPT": _sha(page.SCRIPT),
        "PX_SCRIPT": _sha(page.PX_SCRIPT),
        "page_script": _sha(page.page_script()),
    }
    for case, snapshot in _snapshots(root).items():
        for name, text in page.render_pages(snapshot).items():
            found[f"{case}/{name}"] = _sha(text)
    return found


def test_every_page_and_asset_is_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


def test_every_page_is_covered(tmp_path: Path) -> None:
    assert {key.split("/")[1] for key in GOLDEN if "/" in key} == set(page.PAGES)


@pytest.mark.parametrize("name", NAMES)
def test_the_facade_still_exports(name: str) -> None:
    assert hasattr(page, name)


def test_the_facade_is_under_150_lines() -> None:
    assert len(FACADE.read_text().splitlines()) < 150


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        for key, value in digests(Path(tmp)).items():
            sys.stdout.write(f'    "{key}": "{value}",\n')
