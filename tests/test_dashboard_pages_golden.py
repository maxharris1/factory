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
    "SCRIPT": "9e0a8f9a1b3f53ac1dff785b43f8b3f5b876f626210664d2690717c9f56a0fd3",
    "PX_SCRIPT": "d3c012a9dcc3b97338a731e172ea14cd5d25dfc59c7fab2fdd867bb974b9dc1b",
    "page_script": "3547303a34015d7ae1f12a9994d7ead3d092dd6257c379c79e7079c7fe34f682",
    "seeded/index.html": "7678c80839d31ad3016530ef667934f6c90eee0053436865f0d0278cc2a1d3aa",
    "seeded/job.html": "3874c3d54f98c5ae354b560290840fb57f94290600143de889720d14e856f16c",
    "seeded/activity.html": "2294139cb653b3db967c681a7820c771e18e7ea372d286ac9d70d897b9de6bab",
    "seeded/efficiency.html": "e2833a0f954f8e7c92db845ed88a44d3f99738e917b89b2245fefa30cf4e1182",
    "seeded/health.html": "6b66862db742cb388cb04277ab86e881c0ee2269d3d248bdb4fc675f71801ee6",
    "hostile/index.html": "1b0e32ccf417b839aa9d9d3bb75dc9e0aa3800e90b1f1c09428b315e4387400f",
    "hostile/job.html": "6e267f4d64cd2a547214290859c9d7e2d9ca0b9ffbbf281ca5765148d3cdec17",
    "hostile/activity.html": "f18937b967c92144e9abb53938fb4518656ed2509a8dd73390a465e7c7d03091",
    "hostile/efficiency.html": "091514c839390706e0b07051c9bab2fdd46b6d59e0d9ae9cdf102cbdecfa141e",
    "hostile/health.html": "5908c20d48d4fd6f89db5dd853ae0a79f6fc387514fce100bc39f03de90df759",
    "empty/index.html": "792bc1b1509db08d558b6faa9c242855cef2406508c1ff3a9e7ed11981cd801c",
    "empty/job.html": "5229e05375fd17839e6d360095f27b8606804dc88055741cbada0b892a47915d",
    "empty/activity.html": "c670faaeb55c1d6ea9a0fc654000f78ce8693838770fb095c89645965b28650a",
    "empty/efficiency.html": "73106227eaf25f885b5e435f536413265ebb736d9bde7458b732a3314c0d664b",
    "empty/health.html": "b762585cc7152ca431eed750eba3b629b189d8c06a8755a6771565277b3049ae",
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
