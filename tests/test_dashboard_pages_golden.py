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
from tests.test_dashboard import NOW, make_db, seeded

FACADE = Path(__file__).resolve().parent.parent / "lib" / "dashboard_page.py"

GOLDEN = {
    "CSS": "11730ceabde6daf9bcfd3e1136633e84127c6c3a59b285618cd052050f00f709",
    "SCRIPT": "5df2facae151ee77cf8213b801aa6e99112036bf28023d7157e190858949b26b",
    "PX_SCRIPT": "c468396e0939fde787160777747955fb58f890ece54d70552df65458ccbe4209",
    "page_script": "b12d34b8e30ae7f46a8359cceb0984ad317b05faf1a35951406bca5f542ca24d",
    "seeded/index.html": "ce7764be12bbda0a5fe78385515bef8577b2cef091aa2843fe94b261b2ad527c",
    "seeded/job.html": "9537819122fd1ecd33b485b66787cf6d98df030bf0e588d92492a65cbced5869",
    "seeded/activity.html": "f04514a56e71f727aef3dece622db0e1df3ecf6d9c188f41bcdc97baec6bb3e7",
    "seeded/efficiency.html": "c1b6a77e578d42a054831da3a9debac99245a520aea5b2e33d22fa817d121706",
    "seeded/health.html": "91d7652a1d40726ce96bd89adbfcc61ce3c4acd0f0b0b14d81a861b53d731e70",
    "hostile/index.html": "600e91e835a2babc339b342e15c119a80a03688915e6ff8e46715617527e9a2c",
    "hostile/job.html": "8dc6d9832f618b6cfe38a751ab9a5e4371292e953ac63eee16bf1b8033a637c4",
    "hostile/activity.html": "283736d65a6e2c42d4b32e1426e4e2724a4818a155597171bfd448b7c6ba0ce2",
    "hostile/efficiency.html": "98e0d930c2c22177a2d12aef5003244b8f3cccf0c5a14554e2331271fa95f84e",
    "hostile/health.html": "1f4fbe7db4ea0641bc1c7275e81ca195dfbdea8d72c30c4fa683c0d976fe4c06",
    "empty/index.html": "50e865825cc33ffecf1a128894a2b4ffadf8fd3dd3e12689f7e2887e43ff7b12",
    "empty/job.html": "851ae3295c1d1f655cfbc6ba0e457817e28acac1e8b329528752f77c5ca4385f",
    "empty/activity.html": "1a23b1049ea392f3db06dba61ef146a296efc0788bd44e41d254c4e07ecc9f1a",
    "empty/efficiency.html": "8aa32650515db73ea8105d676000327b41af574ad2edf689130cef99a7ec7edc",
    "empty/health.html": "d00404320455322d10d40cfbd0383cda5457c47bcedc1fbaa4ca33f38154bf37",
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
    "_linear_webhook", "_JOB_STATE", "_state_chip", "_slot", "lane_devices", "_lanes", "_queue",
    "_ATTENTION", "_HEALTH_LINK", "_health_link", "_attention", "_HANDOFF", "_handoffs",
    "_VERDICT", "_verdicts", "_td", "_tickets", "_log", "_POLL_STATE", "_BUDGET_STATE",
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
        "page_script": _sha(page.page_script()),
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
