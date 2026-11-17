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
    "CSS": "d382d4fc4314abcbc54d8d7421e8e0ad1ac4319b604861a795d671320b9dc703",
    "SCRIPT": "51b82c4af6322b02007f067f72fce195aa254ac9e6d7f59822aae5959c9440dd",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "266d4bcde21d80ca846a822c5d2cd8f40c0d75cf9e9a80d18de380553c261055",
    "seeded/index.html": "baa3a1faec54fab46445d11ff42702ff22155b708524c12f4f8b521898be7ec5",
    "seeded/job.html": "b8ee299969799a4fdab6072f0172830b63f1570502f4186191ededad05be83a0",
    "seeded/activity.html": "da90725026b546a09e60e5aaae41bdf28aaedb8204e0bd0d0d7573e833d244b3",
    "seeded/efficiency.html": "9d2a1c07bab524685aca72e26992cf5c5cc3b551b4f77a771a57c59b578cb5a6",
    "seeded/health.html": "efa3a16905b5cc12dac1cdc0b2281545fd10ede77187bd0eabe90d8e810652e4",
    "hostile/index.html": "eda2f85480c218c57d038bd9ea29c3094b0ed8049645c0511da75150271b9d0b",
    "hostile/job.html": "a5f6c39d1e474eb70833ba11ad59e6614be42b4b6c70a9f6b89ce3651cd26f19",
    "hostile/activity.html": "81c410b5fd624274acc3da37e6696302dcbad049d65701ecc1d1c04727015d7f",
    "hostile/efficiency.html": "cd9312c93430649493602f48e0e1809660647ebc19b4d7a9ffa91ebe2ace5ef6",
    "hostile/health.html": "73b5b962ec1fbc2d28713dde0b257207217f9d7e77b34be47c13f7fd7f27c9a6",
    "empty/index.html": "ba82b85e7d8d2e2d92f806391b1d5b0f6ec912985d5c528e6131a89331cf4c57",
    "empty/job.html": "03e4e8bdfe99bd6b52719ee38c05ced731a8574b65ffff6b0af5df5e2b6e3318",
    "empty/activity.html": "39ad41129b9c06a2b90b97935ee35c939f9500f0763e2960e4beba898b6ba456",
    "empty/efficiency.html": "7697c65518d3c050cbaa824e63802ec75fcba5a45428b6677c0e21262376c201",
    "empty/health.html": "a8d4486955e8d9051a5fc663e08ed56c726974281011e6ad27d2ada47622f2fe",
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
