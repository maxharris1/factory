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
    "CSS": "27a57c223f9e25adf4f0f09152ba90061c6b4a17bcc1460ffb275b1869689882",
    "SCRIPT": "241565fdf937aa408ea4d89f35f716794d13a2d3e438a1c3c609081c42c48bbb",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "365ceb72a3a04442ef4b5c0a4f7518166e13327824c443d3688176732123ed89",
    "seeded/index.html": "a51eb3b410e09c9f81d58e4f11d596ea416bc5531b1e6e4339c8df9a654b9d70",
    "seeded/job.html": "1bec7f4c77de572f7f33c358337a50226fe0cc9d85adab2928e7088e794dd71e",
    "seeded/activity.html": "ddff855db039315d8a3b3cd9986c38711fa7e258f0b08e86f62047cb205cbe55",
    "seeded/efficiency.html": "ec087414f4931967da3bc761b9190ad27e7b29842f2cc7bc38fdda59cea08255",
    "seeded/health.html": "130ac1ef926f6f1cb3f99637c6547b8c219f78cba56cb456c9891ca8a8864010",
    "hostile/index.html": "799b17cfb0541ba06926fdd6288d015ce4f6362f5a7d9bea11b7f0fbc29592ff",
    "hostile/job.html": "fd2fc4c382778d77ecf476c7423418557ecfd67952f9b35605ee64a3cf2b8ef7",
    "hostile/activity.html": "7daf806c22fb0a19688f4ec5a2869ff9754d67786e05be481147136501d59d9a",
    "hostile/efficiency.html": "7cb749b0f603e14e50c91bbf93da36893cffddaf3370c4b997061f2d5dea869a",
    "hostile/health.html": "22a4bc3b1c490dce1ef54aa021fb63071f2d62c7fdd47ea38b45ee60085c9921",
    "empty/index.html": "96ae41550e015c8e2012a192713969043aba3c2b7c5aaf269587adb6bb637bfc",
    "empty/job.html": "8f46840689a6c1bf0d047215166b520379d72ebb7ae4f548fd9aaf551b76329a",
    "empty/activity.html": "57ad4e39459f80a54049fd9884a391aee95e58ffd6658fb1be2a7d858812d1a3",
    "empty/efficiency.html": "9b91c5931001df415a9a60b548a96b2dba8ebce8b3ad99b137f1e21a0fbbee1a",
    "empty/health.html": "f8bff09fb1d45833b6aabf23ab2453d68e70ac47875f391a772b35d0ec8f1c41",
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
