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
    "CSS": "5bde42c1ddcd80437b6d56c4fe6b1568b502d9473aced0e5c3cba1eb888b9910",
    "SCRIPT": "26a02bb7888c0e934faabbb210981e1262a6dab8e3a411d61d2dcdae5999fcf3",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "e529caf78706e35845850d76e1f00c866785c3bde97db27a929bfcc84874a03b",
    "seeded/index.html": "ad06c27e2aac8585a97c23752b0a81dad53091e44991025c640b017410acc272",
    "seeded/job.html": "397d74f0deab034f457d053f3657483e69af2936ac7a92f811332ec5029a2654",
    "seeded/activity.html": "817d3dbefe2635b806f852ec46efe0c585c9c4209e3d03eb086189e36c94603e",
    "seeded/efficiency.html": "818e2eb6a42c87523d4e7776f36de097c7b0b59f1a7799e66777a5bd445a8cfc",
    "seeded/health.html": "5f5e5b85313f2d7eac74edbe0609ccce916890582045decb37ba7cc2d5f0a105",
    "hostile/index.html": "fb4b26b5741a05980f8080c3b34dfa98c14a1f3bde06929435112a1f58bf48a6",
    "hostile/job.html": "13f4366c4460b3e71597356de1eec55e277c21047189f53609eee0387886d703",
    "hostile/activity.html": "4425377dcfe108e35788c9acb3f64af29dfc1aecc00972b67cfe1109560e94a6",
    "hostile/efficiency.html": "c4978df37a4a3c63e1556f6e6ee5a1f9281cd19d8e493679429e131d5eaee0c5",
    "hostile/health.html": "486c9bba97a3851813aa8a6396cc7834a62392a58d7bb07093c8aa943339747e",
    "empty/index.html": "59ac41c5b7f9456577f4ea0027b0f5bfb74173dfb215de9aada7bfb06550e554",
    "empty/job.html": "7e4dca90dbf94413b3c3b68a3b703c61d015087cc2947996e5d69ce3fc76d844",
    "empty/activity.html": "303070cfbc15dda07eb7d4da4106b98931b247b74cc7ee7fca1961de41ef1331",
    "empty/efficiency.html": "5b59d251de3af328c4407a437dc4909caed7ff3a892ed634cb64c9dd76434b94",
    "empty/health.html": "e7873f03edd4f9859b34982e1012c826c24d8ed1ec6523394f43fddfa9040a0b",
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
