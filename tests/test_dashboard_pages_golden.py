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
    "CSS": "55c106602c0b2cf9f01e257eb391e02e9e18baf007fa91f5bcf9a6882984fbc7",
    "SCRIPT": "25ebc257b9f161075db6a8032a27c96314278d0fe96f5c726070952f78010086",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "c36e75a2b683f70102a0b29d054b585705b2a215f8e19511a23d079f1d9f34ae",
    "seeded/index.html": "bef71c9cdf31a695ab689835286b54a7a0596e3a79430cd13d0967ad0e94b256",
    "seeded/job.html": "7ddc2e7a2079752c9c85d6787510b586e9d961b73a653c9ef9d95b892cbe11c7",
    "seeded/activity.html": "e697af5a68a9132bad3c72a3e20fa0693a5edff4b87f001846927a172485f3e4",
    "seeded/efficiency.html": "9b45721c1f72813031986dc8456ba277ba2a741bceb0a0f0e954671559317f12",
    "seeded/health.html": "bf7391d05c4e2ab1eaa2f37f200cbe6a1ae20a19f121b2a07ad8e5d4b254d291",
    "hostile/index.html": "12944bfccc85fc6167d953c573bf121576fbff9f478c56b0d032097ee93dfd8c",
    "hostile/job.html": "21cec5733de26d9c76a90eaeb10cfb87b48d853ec7a408e09ababa718e9807e7",
    "hostile/activity.html": "106e032e3b68b447951b40f488d59449c6d8646c5db26d9b95abbbac4720ad5e",
    "hostile/efficiency.html": "18cd2df36e3c0964abb2ca18c09331888f5309b64c53fda2d3ddcc2ec6d945b1",
    "hostile/health.html": "164a8f764701696133aa1af881dfeacc55ad1ed15a156e0a175d1ee715828646",
    "empty/index.html": "9058c05cc3e147f4fa14f9a2d9a61a5830b60e6685dbc945dc8720eb51c5ff17",
    "empty/job.html": "72e763371b3df0782cf4bee00d03b04fd8221d7c3ca946ff50d1dc563d93ffb3",
    "empty/activity.html": "bc9ae126dc945149719880d488edc6f31f5b78a12ba95805f1122fa8d867a99d",
    "empty/efficiency.html": "aa59e59b59260e0fc198ad0a8168229a815587a799cb3b7f9bf889f0a3ecd406",
    "empty/health.html": "854d19354933e9384423aa6c3a4114c8bd486ba9568463e2c18b4050a96bdc60",
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
