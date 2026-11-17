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
    "CSS": "0838fd43367b57b0d2579bd033d398c466a4f626bb7016e4f7d3e387251299fa",
    "SCRIPT": "e601165e5257a536ac4b63219369de48f86b1d773d053f67a761a53e850fa638",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "7dccf3e11aa85c2635750cbf397fae71c7195cf01ebfde3cc7951b3a821054e4",
    "seeded/index.html": "76194078cb4bbde70b275eb078f611d16d8ff44a8c6f9c42afa82b83a9d0b207",
    "seeded/job.html": "4b39fed6a356e717ea33c16dc782f731b3c7b971f7b5f95214a45dd4cbaf715e",
    "seeded/activity.html": "36bde616204e1a23b4a7ebc6c9a337aafff8315b8acff096a69a72603a01972d",
    "seeded/efficiency.html": "390cf7ebc863646f98f2756cf735c0aa8ba92b4fe9b72cb831d4a25dadc4557c",
    "seeded/health.html": "2b93494ec94bf479cac223920fd4dbcbf99a87646ae3f5902fdd6699f6bd2dc5",
    "seeded/ticket.html": "10146fcce18081337b4b1e37f7683a88eb85a7dd0035cfc95c0672bcaabb8074",
    "hostile/index.html": "ac8b8b19ced32f40217410e93e1f3da92d4c43d8a10bffe781cf0c955f6f1f3d",
    "hostile/job.html": "f4dbf6de72d7bce3187646e6356e09f0308cb725a3ecf20722df93c577076fbb",
    "hostile/activity.html": "2713535df3cf9610736af04b0fdbb13848893930af5187ee560bfb955602bd76",
    "hostile/efficiency.html": "4c1ad738d5eabea39ffaf1224a2e06597e7d5be77379f42db5b0d14f263c678e",
    "hostile/health.html": "448e649ae396f060533c30076f7069d7c3edcba68dc6a4e780e8f64b32e47a0a",
    "hostile/ticket.html": "b60f7885a58005ad27c1fbf672feeb45c9df323d194833b65c5850e8bbbbe5b0",
    "empty/index.html": "d932a34d34491f41f655e7b770eb83df24ecc2c44ebf0c8c56f94ae351df0d6b",
    "empty/job.html": "afa12162fa9c0860a961d0cf825fbac9725a779f846617ab778290b85a589c25",
    "empty/activity.html": "fbd809b620ef5a51f6d0f21efef79b2aa3c73535de9346a7475900a324bcf3a4",
    "empty/efficiency.html": "f2d48901255d896f11c6060aeb8c4ff17cd1ec8a777fd161b3c65e74e5975060",
    "empty/health.html": "475dab91cad1266d0243daee4f95baba77e6e930fcb9e07577c9fd84db5c9893",
    "empty/ticket.html": "2ca41a10210d81b9a7bf4dfc9ae245857a344b63997e9953753aad5a2c4e4b16",
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
