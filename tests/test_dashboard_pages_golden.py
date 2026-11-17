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
    "CSS": "f512cced12afe53ba62a08b6b0c2df27af5f5b1fce499dd9d6bc1fd6223964fe",
    "SCRIPT": "b6ca85cad5255ab270ef7e05f601e555ecdc38ff596bed6269831fe95d977dd7",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "e7c9abea65397e090398a89ba36090d0757561f227303ceb7de57a5d70689a70",
    "seeded/index.html": "fffce2e883d26938fae4889f4e0d587533064f0f1ebeeb272e15d14529684c56",
    "seeded/job.html": "6d651df3979613ae1f82628792b9371d93b4cc51f9bbc41934a4450ac26f32e0",
    "seeded/activity.html": "7ecca187781023059936206a8b1e7ba823d28a166b666cc44d22217860a28feb",
    "seeded/efficiency.html": "19990e09ed101ccb568c366eabfe6577ee4a9059685abcff60083294fc30cdd8",
    "seeded/health.html": "9c6d62fec494e4bf119edcd5eb4e8dc5e69ea4bd4f6dd9cbd4c32f582c134681",
    "hostile/index.html": "f809fedccbdbb98b2efbd4d8b19b3d12dc241dc04ae56638d2e6749f09a151e6",
    "hostile/job.html": "2affd1f68e0f2919959974f5d7db2ea62f50888c8e8981ba65bd09cb58d457b7",
    "hostile/activity.html": "f5478f4c03fe1a5400a0c571a9506b25b2c87ae5eaf0cb32187a529f5bdefd67",
    "hostile/efficiency.html": "727b945ee34a22f41d47377898fe7da1927a8fbadf20b785c27422645cfeb8e9",
    "hostile/health.html": "1dac69f7cc7a5b03d1dd97e6129c895dbfe400e1261787e1259545eab6e5ca6c",
    "empty/index.html": "c1050abd66d40ab33372fae5bf5d0136d68de05d5281b63279078d0a3391ecf9",
    "empty/job.html": "a44a4a3c057aa436e603b3b349d8e30b03fedc3bf0ea40a07e0b5b41aab0d609",
    "empty/activity.html": "6ee5cf93a1298ea5ddcf2d7ccca70fbb47ae06af24fffaa467ed80db74fe4ae9",
    "empty/efficiency.html": "a5c37a8d2e06eaf9f613f3fe2065cdc4d1524451fa308f30c975c7f212a30b65",
    "empty/health.html": "a3ed1f70caafddda487a9447f2667a91bd502a6c97c79073334003ee7f8ec41d",
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
