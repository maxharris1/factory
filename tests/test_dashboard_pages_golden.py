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
    "CSS": "499f5dfa77422896324e03dbfea34161bc7a6693919e31c56029ca4854a59d9a",
    "SCRIPT": "feccb8140cbbe0b71812005728c92cda62593fd6131f2f738fadf0ec5db914f4",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "91bbdaee2f9ba0a4486c1cd1ec99d242a1235066db5c2146b5891260949039d1",
    "seeded/index.html": "240c7be31fc94af12b572025678259d73bfdeb16ef03fd50fb893fa50324871a",
    "seeded/job.html": "344c94126c38ec37959eeee06f49ebaad9d45eee60e29f712fe59b112e891e3b",
    "seeded/activity.html": "80e995e4c2be7faa0a47a23a19957c21f5694c6a6bbab9df91bb6cbcbdfb7884",
    "seeded/efficiency.html": "d24e2781e106acadd1ef5b65284d6c07bb4a1017716dfec00c0e0f7a4c71cc27",
    "seeded/health.html": "59525d906a61735b01f82329f00037c8fc9bf1bbd1167e870f99e3fd31adeb43",
    "hostile/index.html": "447b2ebe86f6ceace25e0de896e01c6115a0a379ca7ddd785a5bdd3135558ad8",
    "hostile/job.html": "73670e2fd32ffd5cd2e94ed5bad99cc978259de8f80456e25274b05b7564b725",
    "hostile/activity.html": "6c509132fbdb478afdca43e29b5ffc563f7aaec060bcbb21fb2f73fc6a65921a",
    "hostile/efficiency.html": "214a8975083f1aac585faf6adb5ca8603e09274c687f2010b6cc36e40466b909",
    "hostile/health.html": "09cfe1b2d8a76ba7b36a6393fcb3dbe57c956a6b14dd16a86ed584405fdc9d7b",
    "empty/index.html": "16ece9298b08ee8b8281bd8f91bee1bbb1608204b2dd3a48b204186b3fc2533d",
    "empty/job.html": "ea7d9c80e3c6d95f10a9c622276494ffe631170c41e89fc3346ce06e96311e10",
    "empty/activity.html": "d99831e49101a4ff7b184cb8f12ed6621a0eb94f5bfd8e001670295108b2ec91",
    "empty/efficiency.html": "5264dcb31809dcf643d65fced1adfdeddb60e0ab7188e1bea82b51e774912909",
    "empty/health.html": "1db323c565c782bdc8261e394f029f8b80d323f1a8902b99ff1268efd5ba9507",
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
