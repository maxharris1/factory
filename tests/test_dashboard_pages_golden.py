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
    "CSS": "dda6a096bca48a932a41b71933fded2b9948088237d5fab75cf8c22e0c57a978",
    "SCRIPT": "25ebc257b9f161075db6a8032a27c96314278d0fe96f5c726070952f78010086",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "c36e75a2b683f70102a0b29d054b585705b2a215f8e19511a23d079f1d9f34ae",
    "seeded/index.html": "56a427bd3eaa66abc05015b689396134b5715cf87fc9aaaf26d01f6abf44cccb",
    "seeded/job.html": "093d3a432df4f17816b601ab9e1c6666bef48afc2b9807c08ee7e4b23a41e79c",
    "seeded/activity.html": "1f4c653c70807025f95a1d43cb7d7b1ddfc39a454af2e8cf20aebaac0868be92",
    "seeded/efficiency.html": "be400ce7e7c2ba170b7519a09e6e211e4b9147d471209de17b8d1998bcc943e3",
    "seeded/health.html": "b685041b477f1c38c663a19b3de29ef5e3708637a3c9e0b0790eedcbb1912cbf",
    "hostile/index.html": "ec69169cc0415fc975a4a8161d02f57280af63a142879cd9aec0d79fa96e0289",
    "hostile/job.html": "7576d1048a8fc0f68ce5a0f68223205724118799cf94937f76c41e590ea7824c",
    "hostile/activity.html": "122b45dd89d945b3127b11344ebc17ba530c0d2448a1c1ff9f3c429dab25b3fb",
    "hostile/efficiency.html": "be35a2ca791b8f4d50dedb9682a2872ca654f267bda878e85bf8a07e76dc2d97",
    "hostile/health.html": "3ff732ac430c0bbfb101bfff1d4efa1b504d7ab70ac64ab4170ae2631afd44de",
    "empty/index.html": "af180ab7685ed66a3f1e0f51184c959a6f3fcb84f412416bb3f2b45f5d3aa1da",
    "empty/job.html": "3f9fa5acab2cda65cfee89b406e14203241de97f19e50fc1993388e0e793264b",
    "empty/activity.html": "4f1b5b97d398cb6d06c28d16fbef59b8f405e74267fd9b42af7e3ecfaba29e56",
    "empty/efficiency.html": "128bd17903550cf71de10ae3773018590212d48139b88ee88ebc738e4d9eca8a",
    "empty/health.html": "16f9b91ce08e2cc45432343a6088d4832768f56dc3a702b18a59f533eba7e33a",
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
