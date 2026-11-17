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
    "SCRIPT": "3107f0f1589d32e2afb5872448945fe6ab86198b9b5dfe0f761b66c90fb3b184",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "70898c50e9aeb329858fa43adee052cdd10dff1791be72085319483063cb9a60",
    "seeded/index.html": "84fbaaa24fc223aaa37479480135e0f0096439851275aa728371e151d14c0576",
    "seeded/job.html": "4e38528d27fe648c91c80cd3db0ea131f742cdb0584b629a7811da577d887028",
    "seeded/activity.html": "29256556e660cf5705a6cc9b4408a32548290193088146f924c27b76b9c1efc6",
    "seeded/efficiency.html": "a522364454f5850a934f7663b3c254a776e66bf29db653edf647223decc571ef",
    "seeded/health.html": "d0d8f19cc1f5533962e5767710df739a4f2d3fe24d679bcacaf6543965fc5578",
    "hostile/index.html": "7d9726ad2b1677bbfa1bd4f5bf5b698f7cba444082c6904e521e64d89b27bbfd",
    "hostile/job.html": "b08f3bae90e832993044aba732025139437cbb84c0a86adcf46ba24c047d917c",
    "hostile/activity.html": "b5d356fcbb6d9eeb49e21025d7fd3281a69902327a635428aa5ac96c60f6c425",
    "hostile/efficiency.html": "8b89234c41450d043f77dc47ea37ced4b70674420780886c04cf210ae5b0d0d6",
    "hostile/health.html": "1941f6ad76cf4f8d27f6a92638d640738a8a2fa06f6c97302927f69a9daab755",
    "empty/index.html": "b41817e7a1eb2922fd0aa9ed86b24b763f746a5e5196896d9ce4ae8e6fd5b6ec",
    "empty/job.html": "45eaf85e23650a41281bd615fcda743b35f78a93baf4d4a26d16703b5214ad54",
    "empty/activity.html": "344d8ded32570d99536b0fb2dd6c2e5f94bbacd54b71c40880b586564afe95c4",
    "empty/efficiency.html": "5619fe2bb2599d8ed5eaec22be5acbfc17d97c0d753f2717a04577171bf416e6",
    "empty/health.html": "ea572e9bc3a0abd14e43840d5c2397c8eb864acca36666f3376b15e8b41bee63",
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
