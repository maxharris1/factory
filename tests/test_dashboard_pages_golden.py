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
    "CSS": "cf2073bc2f46a7ea628a7f98c3eebf7eb906a6e423a8129c661ee55592714eff",
    "SCRIPT": "2ed6d3276e88256a3f39360e3b7976bb877eca92540cef1afdcbcbb3ca885fc4",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "6a1508ac5cbda1b0d2821e36e23bccd05830b210afa7151a3cddc01581029653",
    "seeded/index.html": "6d567b7d2ed70f9833cdfc94c2ecb958e9d38a845065a4af8c5b082fba861d4c",
    "seeded/job.html": "57f8249afa4fd880e7495dbf586d4bdb935ee5a6185bec486ba01281a0a01d01",
    "seeded/activity.html": "27839d54fdf57aeb0e77eb612a555155e399bcded9287231e3c709ebd127744b",
    "seeded/efficiency.html": "2c9cf0730c6d5ec6b03e6252c289635bab9f05e0df456bd703ca13d99404b5dc",
    "seeded/health.html": "1fe9ac3e87aaa2b6211da890b69b771cbab8329e2e84b6ce9752624f4d1386aa",
    "hostile/index.html": "be8e490b9e8093d5e975265bd47020dff96c494ca16d6a068945ed54944d47c0",
    "hostile/job.html": "b35f8527c3c28274bccb9d337e66c91eda86723c2ae80c4de26fc2695ecff9ac",
    "hostile/activity.html": "f96083748a1ab51e7529d8c6aae96c69a32c9b57ac5447c6937493991a71cb23",
    "hostile/efficiency.html": "2f7bd391987a265a17e777d4576a7e00486a1701cf8c74774090b8f183a8ccea",
    "hostile/health.html": "2b4eaf0cee2959d54e0caa285d2b5b4fe4bac4756b4ef28fc472cf6243c96428",
    "empty/index.html": "8ed7d26a7910683a102ea0da5e6ccba9c4dc8192fadb76918efeadc61a2b97f1",
    "empty/job.html": "5275afaa772658fa41ecd5377300e2230ef910088cee97f999d8336f33ee9002",
    "empty/activity.html": "635e60a0a49c2fc623049d476d3abd54c145bdb3ff07586b68ccb656856ece73",
    "empty/efficiency.html": "ce8219f9ad3a1e37bcf82343aad5ed7edf3c557040cb986f6491e570c7db3844",
    "empty/health.html": "b414bd014e672553b5bc187f5b9d2a55f5f7a05d8070be84459af7da8a3b11d3",
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
