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
    "CSS": "ca51da642d2f6522d95e78e816a65631d1b2dddfa6543bad179553bc697767f2",
    "SCRIPT": "69f541de34ba10011164a5557c758ccd1e24618a7e16db0c2cb84c7425f90d59",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "85c6b898e3e5afe58bf9558cbf6e49cae7bbf298e26f7400118718e278fe659f",
    "seeded/index.html": "b0f6988999c995d6a1bed020ea11f759df4b594f23b23c3580fbd4765dac1c47",
    "seeded/job.html": "7d2804ace374364145d12c48c158c150d7de5e70ccc463be54b6cd646c3a428e",
    "seeded/activity.html": "c11c4e0e60fd18f34f19d5d722bf3b31b9b83d42d71bf841b3e8f3f332abe2ec",
    "seeded/efficiency.html": "faa7d00ff8a38db6ab99a318561f4a6421d33a2f64baef58505bf71829219fff",
    "seeded/health.html": "5d797999c59ae2197af8584d39768df3535cf5a7088397becb69a70a0c770a94",
    "hostile/index.html": "8d1c5f506505e44cb1a74525a2b46f3e842969b4b4ac9e395f3d845444f0b23e",
    "hostile/job.html": "ff028be326f8d841504a9e38d143c01f810a074653b4b894ab3981b82a22f961",
    "hostile/activity.html": "73d711d95816669609664fbc29f20b3315a76407e7ab3e92bcd45c7d80374c51",
    "hostile/efficiency.html": "de2901e8fecf6f838a05c05beb750c54d472f3e6a1b639a9080d691b0a2eae52",
    "hostile/health.html": "d5ccdeaa1bff44f2a78f5dd21f69a1939d02cd3dc3887f1fc8f46cc5f0c2eabc",
    "empty/index.html": "eb066a1d6914847c9a85c0f7e3fcf884f88786658a6edc1e9afc7354f54538bc",
    "empty/job.html": "55ce230babd161ebe1f4c15d3b5c8fdc7c57403adc02a1756034b0fb5fb45291",
    "empty/activity.html": "46afb8ea6054f27b43d5f383c5125b20c1cdfc703884e6210b7171a5c6ba6733",
    "empty/efficiency.html": "0b07203c42dbc92bf22e99fa07b7d4704c301926be188d701aa4a6222bd528b7",
    "empty/health.html": "8d4ddbc6121f79257aeffeee13c80b8d9685fdfeea17d1e65743d7e550b9f609",
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
