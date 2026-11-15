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
    "CSS": "e0a11b5c1fc3e332c1bcf46e441db33291aa1e52b18032ff49ce6e581d08127f",
    "SCRIPT": "78bceae11a2eee71efd6f2c44ff054910434863e48d4d8dfb877a9b0fc930bee",
    "PX_SCRIPT": "c468396e0939fde787160777747955fb58f890ece54d70552df65458ccbe4209",
    "page_script": "22a1ca0fdce6196538ab3afe4ed60b9f7d5fb60065dd789031a00ef11cfe88cc",
    "seeded/index.html": "fb15f0112cdc8c21481e04b728d282678fb78f842ba6ed3b5765142b4c3f2b49",
    "seeded/job.html": "5c75866629d5bd708ccd6d416c394c9559f21574e9331b268e23ad8b3075a6d6",
    "seeded/activity.html": "170dee5afecd56ae6d8e79e38dc0e33e0e97732c6d46d2fffb6e3307af616f0b",
    "seeded/efficiency.html": "0eaab2e213fdb67d6ef7203cfd6a344cb9e75eda9759cbd65c8cd6be04f25f7e",
    "seeded/health.html": "303e8572e82834f9784d47d3cfff388c5f25c87fa88b95a0dc742cf7a935c072",
    "hostile/index.html": "1fcdc6e8f71c515a72c4dda2653c93b26afd41d51ed60b3b0231f6a5f6cdf501",
    "hostile/job.html": "a4a193f6de4af14e39b42aa8b0551b563041bbd3840c2aac9a98d0f8ced8873a",
    "hostile/activity.html": "c2d86d0d71d229b3e2396be01f79db6b8cfab3b637c2a7162f3b39457c4dcab6",
    "hostile/efficiency.html": "d0b02c356d29bbcde251ee0ce94cf019e0c1d6701d2ec066612f33b8da02314e",
    "hostile/health.html": "8a15beed2175797d1347b4f69f9d2d2bf2d8dada0f05a9b827e3da198eafd37a",
    "empty/index.html": "d1d225837615e8192fc086cb9d7265c7129bc81f8edda9120cdc2180e1b6747e",
    "empty/job.html": "d1341f12166496b01264e51084fa9d51fb2197c54d57e99e34ce961d48bdc999",
    "empty/activity.html": "1a13e290ab65262424907ca26ec9a0cd8b946af1963088bcdb5d1ce173d2ed48",
    "empty/efficiency.html": "2fb600a1559a8c7a0db3065c44a94af5baece7e685353dfd6cdd0d3ae50f0188",
    "empty/health.html": "f68ef4cb5cee91fc1a93a500bf2f729a74e55c4057480ea776376f677448dfbf",
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
    "_savings", "_active_jobs", "_board", "_legend", "_TRAIL_LEGEND", "_meta", "_job_article",
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
