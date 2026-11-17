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
    "page_script": "b60661bf226489b604d0eee27661dbdb708cb182457b3310e39ffcec5a79a92c",
    "seeded/index.html": "7aa006b19651c0b83e3232577a1ece9738eb60878451ad600f684defbd2474e7",
    "seeded/job.html": "4119b600fbef4890eac9bfe3183e4a332afb5eee484326fb573fbbf408dd40c0",
    "seeded/activity.html": "201c331ef60000c0f024dcb274ad3685481e56b156238ebb54e31e3054f35420",
    "seeded/efficiency.html": "bef1201f01182c147e638bef293dea1fc1ca3dda8288e65ab4903e226c024535",
    "seeded/health.html": "76520e4dd97baea765f0fdc7502f00d7e3fd209a1caa7f2d45f2b8aa1cb0b0aa",
    "hostile/index.html": "9e37400d55b3e7b824c60ad88e4e12750b1c319c87ae3ad9848f1e28fc6fca1c",
    "hostile/job.html": "a7df80a80988d17bb22db6fde357246a1ed8f69dfb82b1a07bd3440b1f64483a",
    "hostile/activity.html": "65676fa5a3aeab206136bd32e17bae5aa6e7facbb36810fb1f59afeb1b923773",
    "hostile/efficiency.html": "1df3f2397754091aef1c18319a34c3156e3608eea216f9e2e47ecb641de273c8",
    "hostile/health.html": "563ae1e8cc30225ecc398cd9ded4b3280803c2a2cae5a3fa77977f75f9b95e6a",
    "empty/index.html": "1b38190bd4ff003aaddfcde12f67ac5f6114b3db49150dce036397868c641a0b",
    "empty/job.html": "9ec0dd196f9964899100081d88b87e044f51162cfe8da5eef2ff03ab3006894e",
    "empty/activity.html": "480e18eac66beed75839fb435cf396e9b658de70b65ea29f350360be01ca154e",
    "empty/efficiency.html": "a6fee3f54f04aa8b42f766c40f0e62a67b7bb12a75a0af67d6b721ce6d898c62",
    "empty/health.html": "40b76e745317d160238315510961b3886e4317fc20fc3439af83d1b6149b82a2",
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
