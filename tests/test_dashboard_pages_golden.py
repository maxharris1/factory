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
    "SCRIPT": "044845e46233047cc264f7a4442fbb9850f0f413863b92fececea57e724fea02",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "79aeede72ce1b4748705c0c7eb3309fd0aecf18990b3ea86bbf22cd29e8fd657",
    "seeded/index.html": "f0787ce47778d5cecc51717722fde7ce7b1b5af511d3fd2f025231c9a00fceb2",
    "seeded/job.html": "11f8ac8efcb189c6b0be6b8a1f27c89b810f90d34fa2fdb7b1eeeb640949cef6",
    "seeded/activity.html": "b0f2868bff358967cc9813548f5723260363e59adf5c429b0d6e3fdb18efb775",
    "seeded/efficiency.html": "d12222fdc78e037bbb0e950bb1cabb8e7d2eed23cb30ffbeb41aa71b40e7612c",
    "seeded/health.html": "7586fa752b4c0d1763cfe7edf3ed36cc2438737414f89f22946d6eff5b885338",
    "hostile/index.html": "39d8f345293b63ef7778ea473d3ef50141078e818e3398027d0212514b81cbe2",
    "hostile/job.html": "b0fc402f640f0d967a530868bd6a82d932355c745b8d278950d1570804e4ce07",
    "hostile/activity.html": "5fc1ffe0d214cd0698cd6837ac488a97845429c5f92227b738b84f409a199b75",
    "hostile/efficiency.html": "c79df73e8cc9d89f2ee5a8c58edb14ee09c8042231367a4f4ee05c145de15b1f",
    "hostile/health.html": "10d38c20e9eeb9c72b7d12e35fd268979b80f37e3a26c3e436d4d20b87332ffb",
    "empty/index.html": "5469dbe72098dea4816b3ddfe952e4cf789acca279ffbb5c130db5308a503640",
    "empty/job.html": "6c4c72ea42417e0c32adcbbe5f37000fceeb59a791a8bc24f8a253e3cb687ad1",
    "empty/activity.html": "05f1857cd35766b61ea253a8c411b632d26866f1d1fe50c3b7575dfa6cdac546",
    "empty/efficiency.html": "e602805ff135876fde2567cfc28490ef167b73945265f1e008c183199a2fd24a",
    "empty/health.html": "03c23f55bd47b73e6554a742dcf8e1b7dfba56390c8435a408aec1c5dbdf9d1a",
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
