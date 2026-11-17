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
"CSS": "66255dd57f49efa58b36048c0ce2ce7c7f4c37c81402cf8ef02fe06680f9ac91",
    "SCRIPT": "1bb49334c75ecc5c5d6292b78c1113067b44ed2a226bee0372b51ff92863ce0f",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "9f8857a1c19e0adef992176759675271b77927869abf171e1bbecf392267dff2",
    "seeded/index.html": "b4ec9bb801437fd203b0f9f3ce765160fae07cb0882f6fd931925cd8dc5854fb",
    "seeded/job.html": "fd8bfe17f4f26166b414a6b0871b87639aeb8d68899da4f4f6ecc6cc420ffb43",
    "seeded/activity.html": "aa54ea8265ed60612cdd1bea600129538d2460735e21791a608359d7d9e43795",
    "seeded/efficiency.html": "7bc9adb533b75ea779741fb89be2c71585629ace4bea6dd0c2ce43294429e280",
    "seeded/health.html": "50b53d8fe187f7469ca55c4e42d0d4f9fef076b779be675300c4c0a4505baed0",
    "hostile/index.html": "9e5bff9bd51bfbbeba7f470be908f9c9b7741bfa5213603600ecef9295006fb0",
    "hostile/job.html": "4abb39d9adc01cd1e75f659ddcda57a2a3765c4985b7e3934ea93a9a73bf5f85",
    "hostile/activity.html": "2e8e3928bbda40b4e003823d52385977bc28fde9c651468a0b99b9f57b9528d7",
    "hostile/efficiency.html": "fc4ba18dc92cb8594a28de6918f43ca24a14222b1f62877f0e33396436706b73",
    "hostile/health.html": "67bdc29a011f4e1ce22109169b1c6631af6f4edaea5e6c4072748118296f2acc",
    "empty/index.html": "85a78f3d847cd799459f397348d32dbe4bcd0c520cd463a7cb9e69d438934337",
    "empty/job.html": "2bb8db40dd760ac9f33bb5e74fa91b13ba86860f99186122ac4aaa60c5147c0a",
    "empty/activity.html": "f2fad95531410d6a71e457b5afaacc3fcf602a8708d815161f3b748fd23bb133",
    "empty/efficiency.html": "1c7f8e4db98bfbd255007878786a1af66e8add90d0b5aad80cfa74f3439a1c43",
    "empty/health.html": "2edc95d1409e50340375eb88a111d246e59710a9965e68925f7a8e4c722cf99a",
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
