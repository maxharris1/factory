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
    "CSS": "d4df591e86666a87dd1943df050559e109c2613b37d9bdf5e6094dbe3fbcd346",
    "SCRIPT": "96cf7b035777678860b808f245e294999b390163d18da82c1679daf42451966f",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "158df4ec15217ba20c3c79c81375367f100bcc2753b03853239dc67edb804e7e",
    "seeded/index.html": "983f4d8afec0a8a408e2f17fa86b6e285b75fa769fac7cdb06cbd2a0010d93b3",
    "seeded/job.html": "3f98adedd9e82d52b391f7ea451a662e9d3203cb6378eb68628a34fca5984e0f",
    "seeded/activity.html": "6de06b03423f761d0ca860bded6bca31fea6b2678fbc677ef4dd75b9728b9b69",
    "seeded/efficiency.html": "d9664789dccdc46deb6ea1dd39d01f9dc5eee27d761dc373bb7101ac5509425d",
    "seeded/health.html": "e99c025d78c75e687ac6dca9e217f9e10bfe44fdddd8f98b2a55a229be883a53",
    "hostile/index.html": "614d15ac099839376d95b7bfea17b3c10943adb56a7826c10c73c5c5b318f3a3",
    "hostile/job.html": "c2375b0d7c0330086c2d44c6cab283ec0de9cfb25aaf8ac4790db09397ca2013",
    "hostile/activity.html": "91065d20c6a52d7fe2b1045ebff678510147e41e3a3f0c4ffab28595b81aab7d",
    "hostile/efficiency.html": "d66a55b30856f7727956ad75720df868a516a992cb5627289736eabd18aa8f43",
    "hostile/health.html": "a6c013a402f9b5653ea3a68cb98e98dc3dacfa60b4d0f576190c1324f9ea19e3",
    "empty/index.html": "75551ad1e38284fd1ffba1021e770789d2eff1c3a55741e0ae971a9cd2694ecd",
    "empty/job.html": "a2caeb2ac1f719ba8848714476bb1a20af430bc0e81d26e299933ff709f60f71",
    "empty/activity.html": "49963098c491039ac4e2fc2a4faf1a89a420640ba223f043ff6aa7117986135e",
    "empty/efficiency.html": "58261b9695a274b969dc0ce85c1590057ab80435569841313397a24569c44520",
    "empty/health.html": "16f283b56470a5c7c73d3222f7a91450b3c0868f29500cb9262f58194342a8d2",
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
