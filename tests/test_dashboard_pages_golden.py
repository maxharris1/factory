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
    "CSS": "898658fac1a12f02717167656b16affc9c15c22f9f89a670ce6108fa613c29ae",
    "SCRIPT": "dca59c8a2172b2ad85ba010826817050b1c273112eb9964b4b0bf04ca2ba2aff",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "e06eb6e44b69362c13501f181251b99cdfd6ec20402ff43cb84583eb24b13777",
    "seeded/index.html": "1863c0111d034fbd76e6092ce2fd7e4a031dbfc9d21024f7c22a6a4338079a58",
    "seeded/job.html": "8ef65a4ba8cabe4ed6a7efbbd918543e283a08b334adbbbf442203186478bc51",
    "seeded/activity.html": "141b5dee82e85c685a5a0ddd6f2bcbd58aa8acceb0d45c0f4b8973ab6b56c069",
    "seeded/efficiency.html": "d361ec341b0646b31fe9c01ffee8aca682c972e49749a41bd2e100a9c5679469",
    "seeded/health.html": "920029e605096f8219088945a92aac829fc599ed148b38f02bba92aa6da27753",
    "hostile/index.html": "ec3e36c9ab39090a61f92ce3de04be6f79b23ca20808c8cd3df34324eede932f",
    "hostile/job.html": "ea407925d27dfc3e8bb4618305654c44bff8433dee22487691967ce35a1e79b3",
    "hostile/activity.html": "9183f6c821fdac9c9f698a0751ff472823075d95a11e413e63a14484f9e48c32",
    "hostile/efficiency.html": "196b2d436f9330593c78cfa7becb28a2e1268da72bf80438bbe9e3d53113c007",
    "hostile/health.html": "e267b33a9b8c162224dfb93f14c9e4655d189170f3a3c4f843f1478f324374c1",
    "empty/index.html": "8c67d43630df6e789419f924ebfc99c49e45f7d3349b4b378b45ccb384cb8015",
    "empty/job.html": "cfaa23259dad7fbdfe123aec5cffb21b628e12dd0f3bee64487acc0f16aea816",
    "empty/activity.html": "148648350542a021f285c9d999db3cf4d9e3cc22ce6b06ad5119f8742ccfd547",
    "empty/efficiency.html": "7805953ce49910e496481b2e98fa691cacd6fae21dc4cfa7a22f4ce3f9a223d1",
    "empty/health.html": "9ec7153f590d5e9b22055b2e8ecb1e666280346dc52b9c692c75761016ba6d11",
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
