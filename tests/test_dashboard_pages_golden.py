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
    "CSS": "9c1ba3e15bef03535c3e44f46b7472de89efbbd48ca4a294424e67461d5cbc15",
    "SCRIPT": "51b82c4af6322b02007f067f72fce195aa254ac9e6d7f59822aae5959c9440dd",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "266d4bcde21d80ca846a822c5d2cd8f40c0d75cf9e9a80d18de380553c261055",
    "seeded/index.html": "2dc241cb76968ed94142eb07ddffc2a9dfedc1be7c14bcdb78ea91b12ed3d592",
    "seeded/job.html": "1849735615354534ccffb52481ab984d6c5f592309bec725c4bbdcd11d52adf3",
    "seeded/activity.html": "3fcbcc89b5dfda32dd194f510b2448a34ce44c08b8621e18357b20fa568aba8e",
    "seeded/efficiency.html": "abd12d1354f379eace1a59917fe0907c982292a0968b5b6d8f6b1bbe835a3826",
    "seeded/health.html": "97621582dca9d63c769ae19852143e6223738153492e71b8bb1e16d4d59f1183",
    "hostile/index.html": "a28871496afa0303847a25b2ab1ac1a8ef7660f45a73fd93003e7df44511613c",
    "hostile/job.html": "fd8e753fd7172d90428ee639af4ec4ca6ae17a61336c049e0d3642ddf16f168f",
    "hostile/activity.html": "ad2cd9512411e1f052b3ef33ad5133f28658fde433918183bcf420b268d7e525",
    "hostile/efficiency.html": "a3d76424c6d9f773f0425cff84f81cc1ecdce70a44219de31680cc03865420a4",
    "hostile/health.html": "f76f3fadc2bf99374a058546a3663910766dc2960d4de11f55da7fc0699b64cd",
    "empty/index.html": "fb856a53f390abe301d141fc823e068bd0c051fc8c69c385f5ff28f27b8e31a1",
    "empty/job.html": "c2478c561ecc614470c3355f29dc8862ceac0aef0e4a45246ae8283ea33681a7",
    "empty/activity.html": "4ae9abba590e6831a4008c84314772bcec57e2336e2c1119aa5b81821e773b88",
    "empty/efficiency.html": "3156f2b30cfc9183df52f8cb7dc3f1f9c03e6b23ea948c7fdb19d8838d6053cf",
    "empty/health.html": "f91ae11bd6472c43f78d35f880d9658a78ebf48a31f74b091e93a90e339de01d",
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
