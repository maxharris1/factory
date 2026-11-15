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
    "CSS": "19a0b1f9f72d20351073fe719ac4971cdef5c2bff041dad108a8a8133b636237",
    "SCRIPT": "d52ab72896b7b64a6108b5c3a0dbc37e050a7697b9787558cdde6b844342c14a",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "6aa34b812836b1b3b604d6988e932b0e8481463e25d866ffe3fe2705d3edb6f8",
    "seeded/index.html": "006c641c59fd4190bfc69b4a87281eac00554b8b65896a7444f991718a68ad91",
    "seeded/job.html": "6cf21a43b0cdd8b902798dae88b77c47a154aea07c3f4de22324d54c86ef4a8b",
    "seeded/activity.html": "9f5632d96cd3b2db2f640de9c8451457750dc2b241943b9f2f738f74be555b57",
    "seeded/efficiency.html": "9de5ca16d84018cacc14171cd7420fcdf66f7a9e5bd7845772fa587e6f2db9ba",
    "seeded/health.html": "afe32d8f8070c26f4b4c0817baa2da4670d32651c6a7d97dd2d53b76f478aae5",
    "hostile/index.html": "b7e9716fc1710ae165b11db744dd3da6a0003458b904307a212c7c2046312e14",
    "hostile/job.html": "aeed3a30e0cb1d2027fc4012bd7564d26f110fc40971b9dee849732fc4bbf88e",
    "hostile/activity.html": "a852c0b27ef0b93bde5345f49fb820521c83bb0ed28609866816ff1eb75225fb",
    "hostile/efficiency.html": "f4d3ab5a8861afeb6e2fb3dd8a6f4b78de920e1a72e586f46e2cf10da21fae21",
    "hostile/health.html": "632b1209cfa55917d0642c72bb4bfebc92a668fb912259e87071194ea9301d09",
    "empty/index.html": "9d361bba608619ba97b504f6fc582b8d37f22c085eef27d6cf0887172750f0d8",
    "empty/job.html": "0bce1598162e16c9cb71e0aae60119a5d972a053340344bd2d817100d129a2bd",
    "empty/activity.html": "ae19457595184cbf74eed27ae75383794f208e944dd2f2ea8873a2f7a26460d3",
    "empty/efficiency.html": "fd91589debbe7bc2d350cb9d654e71bd3e9fee5c7495a2aef1c25cae54f09053",
    "empty/health.html": "f83dd4ea7f26ec6169efb411e8c50f96c510523758976c399cf43646189d068b",
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
    "job_block", "rollup_panel", "CODE_COMPONENTS", "label",
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
