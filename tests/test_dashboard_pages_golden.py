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
    "page_script": "e25767f78c1df86be9d2e369983a98f488e696430c2378dc2791ba13a0deee1a",
    "seeded/index.html": "3ed078f25b4da4cbfcc4fa1645b6f84b43d01b0bb9ee2486ca4e1960ae710c57",
    "seeded/job.html": "a5a633721d8ddd8347d0a4b60e04b478a8c1f9feeca1f82dd4aeb0ce73c977f0",
    "seeded/activity.html": "352fb8ffd93399c4b137b69666dde3fd6cda5a4001ee2b93766e93ddb854c0f8",
    "seeded/efficiency.html": "47637d0488798d96b608a0cfa34e71d91b58e53f902ebb088cf01e96beb2a380",
    "seeded/health.html": "9ae45c559316d0e0ab93181c87cbcb9664a85e6678b25fb079c884c3bed01268",
    "hostile/index.html": "b3deae258e66fc848c1038ca4fd02423002c6e44396e2116902290707c9d5a9e",
    "hostile/job.html": "2b92f17c9a185240210504d200c3bb3fcca927229911f22220544f5034ab4d60",
    "hostile/activity.html": "525fde8db249cf0a9f5748184edd1d7e5728e5516f9347c4d5ba85eac5dd5f11",
    "hostile/efficiency.html": "beb12660a218857e7ddd25fdc8d8eb4fd6fcb71084e6c4e665e1e7ab09c73f3c",
    "hostile/health.html": "6588b3229bd59e5a275ba43ed9ebfc3a8be8c4ef1714899138c88de596605b6d",
    "empty/index.html": "06bb73ebaf726a3890f87fffd39a938bbe10c34fccc8125b3867cd881abe439d",
    "empty/job.html": "01f408283d40989c52034541f92911f399be517ce1467beef4525b98a3ac2302",
    "empty/activity.html": "77a082410e2dce5ef3e4b4147133448af979e605904e7ef22d0d49ae06a0fbd0",
    "empty/efficiency.html": "977e5ea49d4edd9c011909b458e2e46688dc8b52ed5c4297537aa49162e4e552",
    "empty/health.html": "1544633ca468ed03152e71b811039bf90d27ca3b6d4399f4d533654c1e4ea40f",
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
