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
    "CSS": "8b1344568be1ac40da0eb27c7f1b7c141856bd681393d0b0dedeef3f7c13dc72",
    "SCRIPT": "72a18ae8692cd123c4d959d6aae6c39365b5b9f1e651305455a9cf1daeb4763f",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "94aea922278874a0ec6da5dbe31f26c443408dded6addded2f93117f7bf3a6c9",
    "seeded/index.html": "55cd5094ac604a26a4d69d8333cabd275cf8da9c6faaefa1e7cca35b56b3d0fc",
    "seeded/job.html": "62c021ddf5e12a4bb835891e7e763b7de333a15398d0de4014d830ca76050a6a",
    "seeded/activity.html": "59fe5768634ce9deae03492493e91c8a00462c424619699a86a3ed7ae1393d65",
    "seeded/efficiency.html": "152af101197ec4c46ca2c846bfa6523425e055463162e5e637613d7e33e45e99",
    "seeded/health.html": "c113f840895300ddaeabc8c45b7e47fc72f69be3e8346216207997050df060ad",
    "hostile/index.html": "d158697e3da444549f246aa163687e406b8edbffb1c8325dd90435e296b3f341",
    "hostile/job.html": "3894c3a5c6488f27ed45c423da4978db2a76ade5de7857c1c91d81d43df3aaca",
    "hostile/activity.html": "a9a8535da690660ddbcee255fa91c2b4822089dd0dfa5c9cd6444871cfbab026",
    "hostile/efficiency.html": "09c07091ea45c2548e637a7a96f184588cb959ff1085953a72f79d9ba0eb656c",
    "hostile/health.html": "6f888b829d443d9b2a95e4de63d20b1013390426e67aad7427044897203a2977",
    "empty/index.html": "ad81e3871c365e9853c78e0d3e1034763b55e1be2a7860c9b11fcd0b7d288ca3",
    "empty/job.html": "5859172be4417cc6e5a588fa28a6e64f746b32c8ab3e42bcbcbcfa46930bcbd0",
    "empty/activity.html": "b404c493dfe9737092771e68bfeb12e841ce254dc8545b7e5cbacac53e256e48",
    "empty/efficiency.html": "1783b32d6bfb958e218ccda3ea18571bbfeb2d9c3fef55684cbf60ff38acdca9",
    "empty/health.html": "84461f887147bb5e927b00d6809329ab0ced463fc8a6607015f8bc44f92e487a",
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
