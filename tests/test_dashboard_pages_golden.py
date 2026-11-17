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
    "CSS": "b76779fe147c8a3e5ab062714068b38bb15417c7f37628fbfb8fc291ee8615f5",
    "SCRIPT": "5aceca257d01624da0d10133825dfc0000258022fbc3e82f7498ef1237f86530",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "ad1e9d8e130c699347032795a66c651c3b530304e7118ec2a1260e052380ca75",
    "seeded/index.html": "a24eda233f7c8897ff327caf1c01a0d7f98a31c6fda869c438be049d0c43f6cc",
    "seeded/job.html": "86cfa7bece91e4818e23cb6b388684ec8c223c7454fab544ddca62ab6e096387",
    "seeded/activity.html": "fc5976cef2580f30aa17e28b1d78a5bef95bb0d5861055fbbf4f22ae7112ab55",
    "seeded/efficiency.html": "6777f516a6fa61bca5817aac4636f2c384fa2b08216dfda0eaf575b752e4808a",
    "seeded/health.html": "f4caf12439efdbe4081e6f700c9bc8fab848ccdf50331004e87302d4d5ea6961",
    "hostile/index.html": "3fba4f78ef9bdf6596c37ce86a8211846d0b4222acdf57be0f3f072b305515c2",
    "hostile/job.html": "6d1fa082acb1aece881755cf1690a26e7408e8d1776455a98721c025373a4acc",
    "hostile/activity.html": "c215377e69c476c7cfbe9213228b49eacd3a15647586e24bd7aa8053bcca20c8",
    "hostile/efficiency.html": "b6ca97855f0b47456887c230c8e629730c5afad0d09ac810cb687d6454e93054",
    "hostile/health.html": "38e9e0c48453c591d1640336fc410f44b3d2d154a8ac4bfa1ebb97e8d08c6d24",
    "empty/index.html": "e204cae70a8c640cfe49701d8798034ff5ba6c3df07c3f74ad5e5cba186c24b1",
    "empty/job.html": "f558540d0cd7286ecc7c65b338b248b4f9e9fd140343123f30a60c5500a29d43",
    "empty/activity.html": "23940ccfaacc587a13bd8dcf58aa8a1d7fc3154e9179549a4e04786df1c1384a",
    "empty/efficiency.html": "906b5b59629d0dd30821957f4351094b766c3d4e77069a84aaf149e8cd87ea7c",
    "empty/health.html": "07fc02431eaafcd6a0bcd2b8bacc66fd4a6027cd5ccbed2a8a63606d16c85494",
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
