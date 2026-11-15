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
    "CSS": "a888cd5290fd766c0e0b7af1053b523b5262e35382aae0f93adaccdce95a6026",
    "SCRIPT": "e13e06fb80a3cf6b6be79207b4bb5196f07ad8ee1d89aef04ec545e29677b906",
    "PX_SCRIPT": "b9353d15709b8b09e1de473712f8b909633ebe0cdfda2abcd987acf9b62a92b4",
    "page_script": "9fbee264c696fc0d2edf593d2050ad67afc399b056876589d160957508eae938",
    "seeded/index.html": "508d7c28cce92df4b880f0e215e827173993ade8f2fb8ba961b81d37b47c25ea",
    "seeded/job.html": "593230120d8b40a833adfd2e9bf768cd5bc8d4e967c7097c786953f1059e3348",
    "seeded/activity.html": "3a0fadebd5028763330f5287297ff79ed0f04e6343715680a03dacbae91c685a",
    "seeded/efficiency.html": "f4ca98541e52bfd77e34c56d6797b6b23f476c301f9850349d57c400a238dd02",
    "seeded/health.html": "130f5b8c3c0a4aa0a8af7e709cabdb9e7f67352c1ef98cc4b38f557d86f5d955",
    "hostile/index.html": "1f614a0978d393dff73a723f92a01eccebdecdcd101cbf69dcea5c6173770233",
    "hostile/job.html": "6e8d3954063b3bdb12b0eddacef18f0c1479f4f30b545dcf67fc7e99c86b7ec0",
    "hostile/activity.html": "62ea6d44784f3d90e0f90b260fb68e856fdff5ed4652e69bde7fc8fe3c31773c",
    "hostile/efficiency.html": "62305d8b4d6f6c20acd321231cf1dc61dd2531142aed36b68588d328dcfdb27c",
    "hostile/health.html": "3471004bf1c25f0ab076caf49f65ae0f3c4a78dd7d84f149dfb8ab8d7536b75e",
    "empty/index.html": "98780260de4f4e267fcdafb44dcfc077e3db40696c7ab6102c883e9271d800a3",
    "empty/job.html": "3f5e01af00cb868581aeaa4407821446cba04d6e9a693b18b2db092145d6b46b",
    "empty/activity.html": "fd68ef09f9afc740aa6ac46e40bb6dda659c8143d0e1a042ff28062907f1c3ec",
    "empty/efficiency.html": "6fca6d92eebc2369b163c6429ac1649c9097740985b90fd37d5ab32c7b9b9432",
    "empty/health.html": "0600e61784579df353bbf0d67a9b9370b57199fe2f879cae94d42551117fecb4",
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
