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
    "SCRIPT": "6be144baf4812a16688c9794ab6f6c984658ad387d19d8b570da633a69c541ae",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "4be9983886a6a8bd7505f1385f02fae31634c424d29b73b727334eb006b765fb",
    "seeded/index.html": "c9daed18f3a97a50465c0ecf1c259779b1056db7b9e8ae38015e59d02f00686a",
    "seeded/job.html": "126084432cd0564830af2af740dd71dd0878fed10df45fcb624baed8ca6cf466",
    "seeded/activity.html": "bbcecbd747e7599f9a56cd18b3f5aa20900ae5e38fe4e03cf435fbce3a8d001e",
    "seeded/efficiency.html": "ff5539e7d951fcfcab231c3cdb1cbcfa0f319a030d68dad579679915a8125f17",
    "seeded/health.html": "5c1195f30aabbb76a7232e52ca5af321c89bc0df6749560be1c002d43cabf88b",
    "hostile/index.html": "916947aca4e52e889b90f00593e438aa1ceee95f7af50b5dd9079cdd9c4d84c3",
    "hostile/job.html": "d05916e16e2e14f9190a84997352328e4c7e688763e70f933deaa6b0acfbd4b0",
    "hostile/activity.html": "7299b5752eec9ed054727e26771c37941785c7d7d1f87b09bc6b090f6ab04cb7",
    "hostile/efficiency.html": "0ddc93b276512bda4043c319bdb53951708a2d697fc1e9c4834706adaaa93dd8",
    "hostile/health.html": "1961783f6ed24475e7b15280c0f1c6edadbd9a354b41090257ec9ee056f958e9",
    "empty/index.html": "1ce7de95697889ed18a25cbfbbeb7c19dc4c377d6903f7ef05eba93bda231d8b",
    "empty/job.html": "ead1ba8b10397c191772c6c9e1bdbe68fbecfe0d55099f83c67918a808ddf9b6",
    "empty/activity.html": "103dd0a5c74ea868002ddd9171d6db140ec395262f0a7a3f3d65596d387eaf51",
    "empty/efficiency.html": "9532b54a5019d8371b1cd3d5d06fc6f2d75a404ac8184479f2c90d99feda6b55",
    "empty/health.html": "208b25067a1c3c1188b00ce4b2f84f88717e1600326c35a042fe91ebea255c18",
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
