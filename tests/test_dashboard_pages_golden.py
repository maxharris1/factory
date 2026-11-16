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
    "SCRIPT": "ea4bf3e0e1e36c0973cacd76af9310bc7180bd5268a9029519283dfddd17172f",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "cd6fd15faec0e9ac2a130eaff740c307a43a96fa536efc76f0e296d9b3b8de5b",
    "seeded/index.html": "fcb7b43b5239e54f78e04e44ff11e8089131bcaadb352cd0aba0805eebc922d5",
    "seeded/job.html": "3a51a9f03d999d5939ed7ddb350a31afb5122642118c17cfc7045a6cfea8f5b3",
    "seeded/activity.html": "75011ad5674f55287d76be343fb1a891815084eb9a789b03c6747c75b2f3bfa6",
    "seeded/efficiency.html": "d953f60d91ce5c18496d6ba78bbb26e57189e151b32e04b9b309eae1f1de6cdd",
    "seeded/health.html": "f14bdf342f5e3793890dc428b7a0efb4f80c56ad90a51e6e7b18d325e54c515e",
    "hostile/index.html": "da63d3f259a9bfe2539b0aeb3922aa0384633d2704b4907d3d8802d0be5a5642",
    "hostile/job.html": "cf0d1d3e1b4fc70eea37cb502ad53746f8b55487dfc505ed43d9c1316f7dad45",
    "hostile/activity.html": "290a372f4d05dd930fb8d85b201eb02cde7068956cbb89259d6ed2655102d795",
    "hostile/efficiency.html": "3ea1ca000f2b19b5152170189d07c22491633d7ec7c9c3c926714ad82acd74f4",
    "hostile/health.html": "488215c312c330567d868f32d97b9caadf93ebecdd2b3e9a34d5b30c5967ff94",
    "empty/index.html": "0e5775ffa373b8b68a69dbce5fc7fa52c414974b14a5fed4859ca39459777647",
    "empty/job.html": "cf8347fd6fa5fcbfa78e532069eabb2bd2942572c41609e904590467e6bd90da",
    "empty/activity.html": "4d66e6c36814da3565ffb2e7d91f4f10e6bdf7aa1b7e9d34228b430850f4e159",
    "empty/efficiency.html": "5356ce043c34e0e0c2b080abc831e20eb7522f42d1382c5a7225e0be2a5b358e",
    "empty/health.html": "75cdaa127537011874f73311a0fd0e72169e463ad06f5e18c24e05e7cedb234f",
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
