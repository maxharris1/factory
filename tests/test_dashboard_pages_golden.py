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
    "CSS": "258ee6480d1c0d478b7e925f737a6f9fbabe9b46286dff2a76cc1de2ba496dc7",
    "SCRIPT": "4fb0bf679251b174b64be91e0aa080e7df5c35371d27c762f15b577b61ac801b",
    "PX_SCRIPT": "d3c012a9dcc3b97338a731e172ea14cd5d25dfc59c7fab2fdd867bb974b9dc1b",
    "page_script": "63b41e941ab0c259d5412e7ef945c531c99dace9a841bb3ad2a3be748bcc59d5",
    "seeded/index.html": "d5bf8eaf64b6d89bb2980ce282103c655e0ec6c9368301fb70d88a350e625464",
    "seeded/job.html": "69d1f3f0efecdcfa46259b36d4d03216de87b03ec040ed7cb312efb1552729eb",
    "seeded/activity.html": "2e4d398c85991d27c6819f0772878e39c6ccd4743306d29d72dce7e32c61fc9e",
    "seeded/efficiency.html": "551631f690b572063452a4cc5f029a29c9c49f1b4fe451a879a7c43cc9efa13c",
    "seeded/health.html": "f889c2462334db180f2c5f676e0d99af66bb006742fe11fcaa38c9c28b7dca67",
    "hostile/index.html": "8d7e03dbc5df280401c55a1fd367d583a23032831c0d810172363e1a6f37374d",
    "hostile/job.html": "2b7ee717a75199b2520cee88a38011a6794476064fa7af13b7430bb8a86ee68d",
    "hostile/activity.html": "d0a1b9894b390222a25bca493f52072e4dedd534c055724cdaff0f4f90c57026",
    "hostile/efficiency.html": "64c9bc4f6651c813b833b186efdc99f23756da1d884087a2fa6c8e612c68a5d6",
    "hostile/health.html": "702e5640cad84527b1b79bb2eab815724c860137e9e266856bd573faa55e9529",
    "empty/index.html": "b25ad18c83d3f1126532aa782b3af0c66107f3d393c96dc54d7a6b7ef638505b",
    "empty/job.html": "b716ccf7d6bd277761ce4b50cdb78a9bc7bf51a9771b85e22a29367e48dc4130",
    "empty/activity.html": "299bd3d545eaf22b7b5e8614161334f58ee86ac8034f774aa680a6b39457e15a",
    "empty/efficiency.html": "7472a14cd70a37fb2745bd606fc6f6b22f38fc19c0c8011a0df3bdc6530abbac",
    "empty/health.html": "03bbfb0a009bcdcf8aa1c672b7e459b412b7d4eb939ada8218bd0c5e6e76c21f",
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
    "_savings", "_active_jobs", "_jobs", "_legend", "_TRAIL_LEGEND", "_meta", "_job_article",
    "_jobview", "_flow_template", "_PANELS", "_page", "render_pages",
    "path_geometry", "render_path_svg", "render_svg", "TELEMETRY_CSS", "TELEMETRY_SCRIPT",
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
