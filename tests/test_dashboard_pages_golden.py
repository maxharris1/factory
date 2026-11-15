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
    "SCRIPT": "035699b4bcdfc3c3257417af39d52299bde6c305cda822ecc1c4b715df2d40ae",
    "PX_SCRIPT": "9638f3995b27e67c16e5830b74f8b506fbd48c79258e7d04d40d95e9bddf1d98",
    "page_script": "5a42131c95f7efaf4f8df336f6991832afab0790fd222cb00fd61ddb1c68157e",
    "seeded/index.html": "6c82d4e262a7047dc904154e77899f9d2e08ca1e1d550b7adf6cf69d14137bbc",
    "seeded/job.html": "d2d726a7f0b028bf04f2cb03f547b247d5088963ae2078a57584285d885e8ca4",
    "seeded/activity.html": "a6665937c1434282a1bfdf768347fa62131679e8a1a2d0b471d5b5728b3b566a",
    "seeded/efficiency.html": "c3208e66445d1461a380a2c9f76a250fb9ade8fb24cbe2b36ff8f82ffc62ed16",
    "seeded/health.html": "709763d7fafff70b8502144f9947e2d2f58001029fca43b75f5dbeea0baff472",
    "hostile/index.html": "d0b44c725b09ca5a79aa270058e83a98b80607ac1e4b756ce2da9924dfaa6416",
    "hostile/job.html": "09a5e05fdbf145a9220af6dbce5e4fd4a274e165405d0ba56b6e18319f71b2c0",
    "hostile/activity.html": "c1af024f5c07a6d7c9486370aa7fff06a9bde239e846416fb6172ef3359c9ba3",
    "hostile/efficiency.html": "e4d12b85af9c9e6bc78c276b4150a742b3f6617f2643e05976f1bcbc9ef52b1a",
    "hostile/health.html": "35544c3618603ccd53b900382a8ca33df0266fb9b6985000bd45bcd48957efa8",
    "empty/index.html": "39118b9a7d0d087a65ff76458e186443b1422bd85fc83eead7af78ce4110e438",
    "empty/job.html": "20481219acc851c220c3a4b44b2dd09dba50e98f7269be041fa5b296f7b0d9b2",
    "empty/activity.html": "3bf80b46fe4ca233284e0716bdc3498cecc83dfd7d4cbb3aded0aa77fbd951af",
    "empty/efficiency.html": "5758871ef1eb5e9374093c89b8eca4d692981b5d1253737cae63a14babfa456f",
    "empty/health.html": "9c9dd975f209313582ab4be9273e1de34d110a6203a3e836ce494e3e937973c6",
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
