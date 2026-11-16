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
    "CSS": "95158f56d78a43f4657d689d623b84bed185e53d50be194ee9dc704c8bd2abdc",
    "SCRIPT": "24523f9bafeeb97ee0c97b30a7af37431aec46345ff257a6ed3db3fb1b11e8f2",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "a6233f56d1df05b5a2bac5200b0f5aac3114c47d85cdb0575b8c33a13ab3916b",
    "seeded/index.html": "de320e3974e8afc54cdff2ad95a422d57543a00542800b7bc2dba4b3c7ed720f",
    "seeded/job.html": "54ca86145cf428018266c388cbca03fbefd12b553b444df939e59130b7694ebc",
    "seeded/activity.html": "12709b34c1c7ecb4212f6ce5436a15d1b9e5c568cc1a23a14fb76a32e11f233b",
    "seeded/efficiency.html": "a71d313115f64d97683aa2e26bea1ceb8d2781e7a17320ae40ec6fefa7063729",
    "seeded/health.html": "b29fb78bacd0934fb3cecbc417b12ac2acbbb08aa13d8f740aa04abf0c24868d",
    "hostile/index.html": "a37e412353cb671972af452a5fc6b663dba511cf475d1ad6ce3769d7c65b7d18",
    "hostile/job.html": "432b69d07ddaaf1616bc5ad6300cbbfa1854365cad8ea7e299ffe7e3b2615f2a",
    "hostile/activity.html": "2659081e211d343400fa7e7d569aca13daf8ffde130826cf8debaad674aa9afb",
    "hostile/efficiency.html": "4c167e5a6d4e9b7048ac46467a3caa55c69691f659c8384faa44e536e01d4587",
    "hostile/health.html": "68dc131dc84159591b52c59fc593bb0e9290f31ea2ca3b19cab91690d0a65a73",
    "empty/index.html": "16e85db5cc1c12f755262f60c5fe719576ce8632f43d8af4c8e43cf638d0a9c9",
    "empty/job.html": "1c32d3dcdf4dcbf486e333b416c2f1474a0b301c5778d87e3af6b9516b4b44d4",
    "empty/activity.html": "6efb722e01487e52873bd24a8ccb0859d8f512dd0b5dfb0fa0ad6ccc196ebda3",
    "empty/efficiency.html": "b7fb5078944f027531e609ea344435560842b05fa03d0e96e032fdd967bbece5",
    "empty/health.html": "47e835ea7068b44603b9c6bcb968f355f0133347bc8a5e0a33cd9a5ccf5dc719",
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
