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
    "CSS": "902ae71614cbc5d3274edbfc1c5391bbd6bac84eb9c282bb16477ce9ed210c15",
    "SCRIPT": "ca11201ab8f9f1d3e963b1ded7ff32695ca99496ecd50dd5393e4d6bdcecca73",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "9bf81a1ad237d8b29553972cc9b6e6db321517af71be01e176edc7b5e497b69f",
    "seeded/index.html": "b03845a179adae43a6969ca2c649c06bd90a78db10aad064330ebb06faa46d28",
    "seeded/job.html": "0bd39420ca2648b17525729c0d596ff4d338d6be5a5814e718d3a1a0e46e6a98",
    "seeded/activity.html": "b261bdc2215bb16db2107a12f0c1a563a7c9dbe976f73b592200b8e60d43076e",
    "seeded/efficiency.html": "b3f0b48b4e7da11c2a068a8ab61d41e277cd50b477bc4f08a6c0f54bec02c4be",
    "seeded/health.html": "6d88b8615cefbe2a3cb52e12402ff29c64692b5fe7e3b4f98509c876c3c3b007",
    "hostile/index.html": "32bb21f813028bbcae9a924a287bf76a9784c348b1a4d871ee19c1d932dcdd94",
    "hostile/job.html": "20baab16463eea8e923f000aab79be0302309d5158346b161d4b13acfd6ec63b",
    "hostile/activity.html": "986d9f178a13f5ed56466d3fc2f5e555da7bc8afe2cb23b6ffbe6f790902b623",
    "hostile/efficiency.html": "f6e55b120b8f67aa759f7490268eab1af2cb29e9da0ee3935a9bd0b7462d3281",
    "hostile/health.html": "485aaa433b31702c0b6d3245649c756b30f569608bdd81bd3a76128d5304eaaa",
    "empty/index.html": "7c56236d0638d8055d77567d702cebc2793c35e6fcd3f82b936c16285855d60e",
    "empty/job.html": "c89b9bd2a33a120a8947e2be878886c0d921129f53e2f791ba4156a444e7d76a",
    "empty/activity.html": "f2850fc8c41d9469a12519e2457e6a9fa4fac2f5cab9770ee807f2b1bc00ada6",
    "empty/efficiency.html": "d78bb8f6b7242e05da203b3f5dece80c783873faac51843ce6f8b6cd11126b53",
    "empty/health.html": "be4086fe9f066099a2f807216c349ee30f91875d36ae88b96156b808aec8970e",
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
