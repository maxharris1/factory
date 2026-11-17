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
    "CSS": "1990a4ca9843cf9776cb96ecded61530a5bcc15239eb48f86a1aac3c63b3b48c",
    "SCRIPT": "84ab3310ab90274e7a4d986d8395883f0b6191cba3aa758f719ea63ced1d0e22",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "158e0c134bb53f9d2ceda9b52e9c89a86cd4c0d9ea98f717cc5fe6c6cb9520e7",
    "seeded/index.html": "b13ff7d1c58bf8e0c65eee7ac7956d5de6390b55fe02e9bd49104db77594f0c2",
    "seeded/job.html": "25f69ff2d6b2ff9d91ee96bcbd36bc31ad00eb42717e4077dcbc16ef26add64c",
    "seeded/activity.html": "e6c6a4680ea4cb38aeadad65b042621d27d20f83977be5119af81fb1ebcc64f7",
    "seeded/efficiency.html": "301903171c7926c65749ab820f4f1c5dbb1e5d164c135dc51e0dfc54e89e5d8e",
    "seeded/health.html": "43eccc84454ddc3aa15dd4ce50e3836ac8bd41122d8d821f272eae6555e9e6c3",
    "hostile/index.html": "5bbd9f787aaa7b028a0094bc4b7e12d03f394f18e6d60c47bef91c7fa3c6c18a",
    "hostile/job.html": "a2ccba74d0a20166ab2a17c1d5e3b1012a9ec59517587432f2881065b944271b",
    "hostile/activity.html": "8fc378d07797fa9cb88d09a5c57e2cc97b5656391b0a702ed594860b3e8f5e1e",
    "hostile/efficiency.html": "0b3e289f14774698ee78ea75e286530eb8c736c9552b1addfb1f3f25bc41d0f5",
    "hostile/health.html": "3f4f3480b8ae8e6d71b30cbd8546e62e49f789307767f5c0a98f2e0ae854b8af",
    "empty/index.html": "1fdb18023211d7459593a5c1fd631700e3d1377f628d1442ebe1ad821436b212",
    "empty/job.html": "0f56954811b6f74348ea26c4c198a1f34d7190982ada5fc7a0cffee1f81bc239",
    "empty/activity.html": "f9ec8ccc12b892737edc2f4241cf8f552dafa711da66877a54c8cdd5daea4065",
    "empty/efficiency.html": "b203876c3da2779f847ecd17763c1fe00f52880eda364337ea491ced4e7a098a",
    "empty/health.html": "09f878e4d3bc56eaa57ac81d29807624e68532d420891bc188b0b2e171b9d215",
}

NAMES = (
    "POLL_MS", "FONT_DIR", "FONTS", "CSS", "SCRIPT", "PX_SCRIPT", "font_faces", "px_script",
    "page_script", "PAGES", "_PAGE_NAMES", "LIST_MAX", "_BOTS", "_e", "PX_EFFECTS", "PX_ONESHOTS",
    "PX_BOT_TINTS", "PX_FALLBACK", "_PX_BAD", "PX_BROKEN", "_PX_LINK", "PX_STATE", "PX_ACTIONS",
    "_PX_HEX", "px_table", "px_state", "_px_progress", "px_frame", "_fixed3", "px_strip",
    "px_job_strip", "duration", "_num", "_short", "_ticket", "_job_label", "_job_kind", "anchor",
    "_b", "_span", "_bot", "_chip", "_panel", "_feed", "_when", "_topbar", "header_load", "_band",
    "_attention_strip", "STATUS_LINKS", "STATUS_ABSENT_LINK", "shell_table", "version_drift",
    "_segment", "_count", "status_segments", "_statusline", "_FEEDER_STATE", "_feeder",
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
