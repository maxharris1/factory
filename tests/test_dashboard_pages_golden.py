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
    "CSS": "499f5dfa77422896324e03dbfea34161bc7a6693919e31c56029ca4854a59d9a",
    "SCRIPT": "c34fa5a7979878ca8f377facf59135f6ad36a88593461f6ef7ace801cfccf147",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "adf19be981b0f8a3e97cd99f49ff268aefb05678e2011d4516f38602622635b6",
    "seeded/index.html": "6190084681cc204cca0a97d17d9e7b7feed2e717d3845687899365ce33c5e1bc",
    "seeded/job.html": "72f0df19fbbc94e4be15df3b919ed4e562c56e53c7a3a130aaccd9e674786672",
    "seeded/activity.html": "dd9da3bfcd99f2751eb6a3d0163335dc9c815cbb2841103d1c472b434a28a137",
    "seeded/efficiency.html": "49a30d5271dcda01dfb93c7fc1a4da97a66e40d31e0467928dfeb84ca761ad86",
    "seeded/health.html": "a71212868e8d8b205950e381398dc99dee90b77e6c4c6904bbf4394b98131299",
    "hostile/index.html": "8eea876668657d303d823a9a32fc034a62b5958ed698c4a17310ee1be401eb6e",
    "hostile/job.html": "9f3769a1ca22d42c1ddeb5dbd7a5c6ef3a6ce962415e578cad2d35c706247d7b",
    "hostile/activity.html": "f65f011eb829c8eaa19ed854dc875d88f0996fba788e2087abb39edffba27916",
    "hostile/efficiency.html": "233fa9dab8e5c0a4d16e44e025b965cba9b7c526e250f82e9d12acf08a554607",
    "hostile/health.html": "12cee8a1e10349ecaf2350b863e7d9b783396cf374be8adab8aa06df7465c20a",
    "empty/index.html": "a3d348eb3e23d7d2288a60a741f9c84877d65a97714a89985d39c297207b7ae3",
    "empty/job.html": "45aa66323cc00d11ef82df1fc351d8b40175e72ef5976daff18b2cb09a15a87f",
    "empty/activity.html": "099394c2333d077d41ede8614fb2914723ed2cf927c2d53ea6834cf44301feaf",
    "empty/efficiency.html": "7722bf5f92c5dfc9f7f4cd70dc233bf4a5a8c4a9b1bed4846c78545cb69f72c5",
    "empty/health.html": "4b7467d1dfc11619364a9a9d6986be2c55d6a99772ac60238bcd3f21e307919f",
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
