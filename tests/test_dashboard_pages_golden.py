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
    "CSS": "8429f8c8059df7218cb5f76c54cae29e0857454329540bf88f55d7fe1d9a373c",
    "SCRIPT": "3903935db39e38270373e43ead515a933e314dc21d82f86ec7554ad47ade7783",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "e7367a8cc53b8e7b951225688ba851f47018466e7a54d98bced82dfc6d19a966",
    "seeded/index.html": "3f49ec64790b1a323f6db5f933721cb1506e2fc3d2c0ae6d4cafd908d057e382",
    "seeded/job.html": "9b025ac25fdb973f29fcdeaaf0fa399e2720bafaedb7188450e0820bee88e2a7",
    "seeded/activity.html": "a4679500039dd6c80609c260d0becd3549c84d0b7e045d8fa6a5589b9486ef03",
    "seeded/efficiency.html": "56bdbdf550386b0e9a5ebab44e209794c3d24563bee9e433f6313a2282a94cc7",
    "seeded/health.html": "cf6f1bf9c000c28906c37873694c16f946d705993e00dc7178f31c96744240df",
    "hostile/index.html": "fe8d8ba346fe271f1e5815fc7e8b15b7ee1117a4941b0a2d3bfd9bb37e138ca6",
    "hostile/job.html": "316039d12a30113810234a9aa8f102a9293e3806868aca95cf99cc0750a26a60",
    "hostile/activity.html": "cb3410620bd929ad6e3bceda60e1a6af84115c66cc66fa81918f80013a60a8e8",
    "hostile/efficiency.html": "198a19cc3c2361abd7adeaa4bcd6908ddcb753374ec46102f8205f7a37a981bb",
    "hostile/health.html": "56b1e2395edc4c0f1d59c546d7741284adaae963ad1794091893962e65780631",
    "empty/index.html": "0f8df422a75b58e0e4c5ef0a3e8bf25454ae1458d4ce6665366ab07567362a87",
    "empty/job.html": "e44143c9f76c0ff7b31f87e212190c198eed6eb41fe616051a152ce0c7d8ca49",
    "empty/activity.html": "13a6b73077088ff276c18ccd147b88fee93a45c70d394058cc0a8e4ebc9ce9a5",
    "empty/efficiency.html": "c196909f1e5faaca09bb96ffee03f630be81c1f838922f226133ffa17116553f",
    "empty/health.html": "8bdbd1ba407572523c3f316849f701bcef3da1d68659d1775a325c6651b46183",
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
