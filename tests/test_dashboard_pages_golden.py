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
    "CSS": "e8cfad151a12ec4caf6afa287c5729b6dde43155c3c772c8a02b8a9ca2b37a2f",
    "SCRIPT": "a1efc50e6a29a26ae4b4ede149a3dc68d7c4cb52b859f7a21f382caa552bf827",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "d9a78d9d186a9ef865b6aac7f3f7d6fead6a2a4628d7454aec89d30d3c425bff",
    "seeded/index.html": "16391a179934673529e8e590d3b906347f30de610f0a60e4aedb7dbb3a1484dd",
    "seeded/job.html": "d53ce972429caf188aa4edc17f27beccf0eb40fdf69b767c5a25e3d4229737de",
    "seeded/activity.html": "3e97189d2e0985301a5d0ff8e610f287c17f165a214f81a2fe07bb48d9e5f6d8",
    "seeded/efficiency.html": "d6211592077c221555869f36ec9293cca2a09a36786a7d6b0c408ecf8b1fa1ae",
    "seeded/health.html": "03caabed4ba5f75990641331d34dff526c7cd6b422c4b78f6143937608373644",
    "hostile/index.html": "c513bc59518e73a28ac0ecc428c9552936475c29e036af3918fe6fff962e7317",
    "hostile/job.html": "015a2c1e0b5fb6d437de9757f0e60bf229221faa2f957b77243973c96f20e8af",
    "hostile/activity.html": "c262b5a2adf9e310c7d4c268b4c40633e8b37e163ed00e7074561021c83ff3bb",
    "hostile/efficiency.html": "29d9d1a905beb578958ecac846881b4569dd4fde8e8def1e0d990dfa1c8705d0",
    "hostile/health.html": "5efd9196e9e74a414ae6c92f4c9280b2e07867b64a10f12551aa14ab384bfb65",
    "empty/index.html": "0139f1211f4d9c0e386c5b177c1e50720b3c3df7e8928d238dcbe01d08124ef7",
    "empty/job.html": "772dc51cd339d6329d96ecae7acc59a9fe4f04b5900411f4f8a225937a16b078",
    "empty/activity.html": "d2b171cca94609ddb9de36412966fd359c0a487a6d1a247ab1942dc1689af1de",
    "empty/efficiency.html": "615039eedde6237399430ea105eb0c5d4c0e9a1f44865cdd2f4ed2e0a0734473",
    "empty/health.html": "eedf35213a770e8101658aa2ba360dc1eef97380fc8d28e13b2ca5815a52b07e",
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
