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
    "CSS": "6af903a1796dc615f03f2bc5a413d9ff5907b8141c8c80c044ce0a79d6d16e33",
    "SCRIPT": "3be2b5f4ccc563970e7613ffbb0987333cbe371df92d073b4ef0614ac2f05dbc",
    "PX_SCRIPT": "d3c012a9dcc3b97338a731e172ea14cd5d25dfc59c7fab2fdd867bb974b9dc1b",
    "page_script": "c0a10f5178daf3a51279514d59768e9cb9416ac0732c533fda26383dc30e7259",
    "seeded/index.html": "eb36272072798d9cd3bd4914da60ad3a4a92a512661e987b92978d5e584a3d68",
    "seeded/job.html": "b65946234121cbbf5379681592f2ba5dd20bcefc218df863e973e25ba1306fad",
    "seeded/activity.html": "0d82fbf327d3abffeadc20e5c770f7b6ac11a57b8f4cb8b9bf4c28033eba7ff1",
    "seeded/efficiency.html": "6a571dc9b3f15c51e5e93d2bee44040e366f25ced24f5dcffe02e8d3514ab450",
    "seeded/health.html": "df28cb0da5ff991ee3ec55ba30ea4ce7b50209016bcbea917a13413e5f89cc0f",
    "hostile/index.html": "56aee68fb14d77c19591ac4ee78a0d7bb28033f498047ac358f0df40afa61715",
    "hostile/job.html": "4782626715d27dadb9c171ec22f6fae02103be7390d08d48e5ee9ea6fb7cb34b",
    "hostile/activity.html": "0b3aa6ca604221d7f636318e750f946d996edc564c4f199af31a80b29356a8bd",
    "hostile/efficiency.html": "6f63ddbfd389075ec812fec462e9700d17ce3fe05dec001d8a51126da4f5a41b",
    "hostile/health.html": "070b1e2d9dd6b722939a00bd190b1c8e3e80e795efff65e7a037fd09404d677c",
    "empty/index.html": "ffe827968fba84023f7a07cf6a0d8f3f96b302379a802a476514c88fa9248758",
    "empty/job.html": "fc071c3ab9eb94ae0567c10184f7a651899f78eb7e63e2b5014538f17e6f16f2",
    "empty/activity.html": "ac1e2a9fd8cca45460b20397a00713716368f15e63c5da0ef12b2d8a90abef0d",
    "empty/efficiency.html": "7125578b79733857dd0f43b85d411fb1817f9b2f32fee6fa00ad16fe091a97f5",
    "empty/health.html": "c201f8130daa57e0b8efeaabb0b8fd937bf8208d8436c1ccdfadea9b3acef7c7",
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
