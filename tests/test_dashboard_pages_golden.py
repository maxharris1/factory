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
"CSS": "66255dd57f49efa58b36048c0ce2ce7c7f4c37c81402cf8ef02fe06680f9ac91",
    "SCRIPT": "25ea584b7a7dbb49b85db179082bb074c66017855c480c668d24e4522198bf3e",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "f6ddab3cf53be7406e0abcb364429bdb2f771e2f431fea83c5828224b5017fc1",
    "seeded/index.html": "4aa73397233e5615d9f56df2963b68af344809260ca245d00ab288900b33630c",
    "seeded/job.html": "0f10e15f1d6777f64e741a215dd30ecc9a7cb984c085fc7737210307ecee15c4",
    "seeded/activity.html": "37da92dbc7a14b049444bbcdc23c6804b3611f0a1c9abf8d9bd6fd675f417e03",
    "seeded/efficiency.html": "a559247db9b6bd5e73c9450afb1fcc66a60513cb1b6f76a651a5cb1de31442f1",
    "seeded/health.html": "8db9bef8c4e82f1b96e56cfc0083fb23033be0e099e7a8ce6ef6f0288d71905c",
    "hostile/index.html": "09bbd6b7503e2d227705d73a197f264471de6d188f943aa688549dea4c63d12c",
    "hostile/job.html": "bc39d27bf448815551d3b74c557e68eb76ce288f2c295d1e0a43fb3a0e3c4ef5",
    "hostile/activity.html": "ce56e79851190c03fc42c9da2c49ca73f518936b3c6d37f1287943f80707b6ef",
    "hostile/efficiency.html": "3d9545d8ea6a963beb4d04f8973aec0e10390912a809e202e2397aeab67607fd",
    "hostile/health.html": "c8076f0d34d29a531fb0b58dac66896ba1c9d349eb29f0b033501b3d9a408b6b",
    "empty/index.html": "14f35f5b55c0c967f69ac5460e6cf681b0bfd0d306f56fa9750746437f9e4c7a",
    "empty/job.html": "cb9b54260f160add3bd9e0ad043fa8f71cc42b8ab5dda1cb10020d7aca275224",
    "empty/activity.html": "dfc920b7bb2b5acb68202973e516bd22aec2be412f554af3d2c1abe33c0a6302",
    "empty/efficiency.html": "4344bdb594ec20628cd304345a5bd2f023a0eb772a7b15afb4bb75d7ddec5b01",
    "empty/health.html": "5ee6fccebbc7130ca2fc4c1a2f4b9c187913a748da5ca0ec03f22e7aa93f0f24",
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
