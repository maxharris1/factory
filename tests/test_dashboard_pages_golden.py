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
from lib.dashboard_flow import load_chart
from tests.test_dashboard import NOW, make_db, seeded

FACADE = Path(__file__).resolve().parent.parent / "lib" / "dashboard_page.py"

GOLDEN = {
    "CSS": "d4df591e86666a87dd1943df050559e109c2613b37d9bdf5e6094dbe3fbcd346",
    "SCRIPT": "5128e4b1226c4f8419f8ebb0f3af2695660195a6a0ec363922689a0c796990ba",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "9d5c009e90ceaec2a1c3c58634a138d3665fd0a1e248096b9fbc2fda2b7ffca9",
    "seeded/index.html": "9b7a9a5a0be0105a555af491b52888d46600bbac4881f26c38c8363ac4c0627f",
    "seeded/job.html": "4e1354142748946faf35fe3d68250146e2884144f98ef17f29dbeaa99f2464dc",
    "seeded/activity.html": "aff200decc8572e048823ab91aa7cfa98b34bb681c3f7d2476490cc7ad8acc6b",
    "seeded/efficiency.html": "c670a7f6c924d54908441dea382c012fa2b0a7ba987eb1c22001c6aa19ddb733",
    "seeded/health.html": "743d4f979dfcd7806ab9cf2a6302c39adaa9f81c83d360778a93a0aa72199386",
    "hostile/index.html": "be5be932e3fff039926dc83cf1b217ca0f97ef33c42fa687bd4c3acb9ad8b914",
    "hostile/job.html": "273cb5239a1694359b62ed7de0f208ac64f983e9409283f573dcbbbe0473c947",
    "hostile/activity.html": "b9c7f85afa7c199e9d182f3f6e4f74b384100ed1e73f27bfbe4ec3d53a7b7f14",
    "hostile/efficiency.html": "85d9270ebb386f0248fe7ccb9356911c1fceca4f9a55978586191a367812e1f5",
    "hostile/health.html": "99ec042363b843c00d32b6591b2b8afbe9ae56149bcbb434251b4041851ec07f",
    "empty/index.html": "f6548aa46339e3c74ddca85a79576fced54991c3a3fd4e2f90c11b5a2f0d2dff",
    "empty/job.html": "78464460f4828be4b6d1b682ff0c1790d02529182ce16020d045ba5e0ae20682",
    "empty/activity.html": "dd254253de57fc6c5d805c9e9d48b7ef4dfd42132f6fceb9ca4fd41ae3513d88",
    "empty/efficiency.html": "b120c2d7dbfb702248d57f7280eaee4e1d8022ddeee3ec06f26acf56ae4bcd54",
    "empty/health.html": "b608bb89edf415ab4b35c8a9d1d5770b7a9b6919e937c2f80fe25313cdb6fa97",
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
    "_linear_webhook", "_JOB_STATE", "_state_chip", "_slot", "lane_devices", "_lanes", "_queue_lane", "_just_happened", "_intake",
    "_ATTENTION", "_HEALTH_LINK", "_health_link", "_attention", "_relay", "relay_threads",
    "_VERDICT", "_td", "_tickets", "_log", "_POLL_STATE", "_BUDGET_STATE",
    "_health", "_HEALTH_STATE", "_ANCHOR", "_anchor_attr", "_components", "_no_waiter",
    "_savings", "_active_jobs", "_board", "_detail_model", "_job_article",
    "_jobview", "_PANELS", "_page", "render_pages", "TELEMETRY_CSS", "TELEMETRY_SCRIPT",
    "rollup_panel", "CODE_COMPONENTS", "label",
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
        "page_script": _sha(page.page_script(load_chart())),
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
