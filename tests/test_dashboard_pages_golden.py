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
    "SCRIPT": "86d7a953fa669ea8a27d2331a1abc642e40b549ddcdd265d3b8d0ee821648e3e",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "a035ec8593f0f979a5a51061784e1e1707af13e01091cf7674b0b2fa1cd9992d",
    "seeded/index.html": "0ace109562f19766f91eb838b07571f9693ca1ea13093411b917af37d8de2971",
    "seeded/job.html": "61b757f55856c2a4070e4e504af3fe33caff69513853cd58bdae7179ed025545",
    "seeded/activity.html": "beccca2b0540f4365a010d7ba7fa611895bd804f6b56d881010d55706d5ab87e",
    "seeded/efficiency.html": "4f9cab7a56ff91a68a4cd5d17208b91014f46c4088c02f1ad11f3817aad57218",
    "seeded/health.html": "563d4da0d144512aaf20523bec798e3f07c4cd8d80854cdcf1efe8cb677c699f",
    "hostile/index.html": "666bc53a6aa924e7c103708b3de636bdabe5d7a408233527231e981bd72ecd0f",
    "hostile/job.html": "a5d6222387624419166df2ed6ad481789970ea9c1bba64983bea6d2ac4bccb79",
    "hostile/activity.html": "817be95f7cc4a8e863172d4050f5ace1842c68ab254201d9260af1a6d3ab4602",
    "hostile/efficiency.html": "71b3032b262ae96357463352ec8a435cc593c678000075bb5c733b33e1768d63",
    "hostile/health.html": "62b4a0345c0d12dd337ec6fb1e006d6942efba1faced41bbdaac74c778891357",
    "empty/index.html": "e498551935695a454745db93cd5386d26c52a11a9f8b963c4af7cb8c0d92ff14",
    "empty/job.html": "2145bdc9ba5b81f1f916648105978f5e89a133a8ed8f8c5ed9adf33a48adb7e4",
    "empty/activity.html": "0accc86ee3d7f2fa14bf5cd94b583a45e6554c7f8372b5cffb5255a318615555",
    "empty/efficiency.html": "c7a1163e97a64295d15c6358a8de21621d3a5f2f8e4dcec583ca9f1d1c102146",
    "empty/health.html": "c18ee660cd6ce67db976fe70b04d745294441f46799274f7279f50a38184620d",
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
