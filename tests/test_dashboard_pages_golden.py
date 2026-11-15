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
    "CSS": "5875e8d15ee66f92872b73f4851519dd48d9ec15f03876caca183c440c6d8384",
    "SCRIPT": "613269382f9e636b31737b2a300371fe6c12491a653129cca0ddb3b1a45097b0",
    "PX_SCRIPT": "c468396e0939fde787160777747955fb58f890ece54d70552df65458ccbe4209",
    "page_script": "affc4aefd90221178fb401833d94573b26793e7b4c7ece8eb71a12476affc3ac",
    "seeded/index.html": "b62698729e285075ac38b5702c81c5b00d15567a76f4247fca86e2dfdd72b41c",
    "seeded/job.html": "14a6b4fccfa7247b9bed6f227621b5a58d25f796fe82aa6535f535c086d0d4b0",
    "seeded/activity.html": "165b02f8e9d6498780afe80cb53710c94b68b85182a4b6f2939d2c2ea356d213",
    "seeded/efficiency.html": "8805a4323259f3c063190bcba560b0800920e3e1aadb44268a1d406f4153aa97",
    "seeded/health.html": "025a019e72823a1faeb107912846fcd3962129c6fed42e735763d7fd6d58c150",
    "hostile/index.html": "0fef5dc1244c4bbf65898021784bdc71a90a704de349e45f21cd0b0c7e801ad9",
    "hostile/job.html": "eb85f321bf68a65306abaf3df5a81c1625c17ba6690134148db400443f862e1f",
    "hostile/activity.html": "ffc1232c96c88a5889c14bbacdab93a45ada74aa2e73224f02c2868e15220808",
    "hostile/efficiency.html": "e26d749eebb7d8ed4e3c60d95bdfe0724e4635f899a16d29ea05ba5cf6b5aed7",
    "hostile/health.html": "ec42332e4dda118ed20c24cc01e7b32f7a8b2bcbc93940be47a5e91b5605f0c9",
    "empty/index.html": "19ff5e7bdbaa9881bd0b191e862024c21614c2a2880b5e3b7914b33c421760d9",
    "empty/job.html": "89d22db9e36d1b57e260cd1f301cb51544995cb2d20a382cf04ac51244d3c813",
    "empty/activity.html": "fd85218a403d292af8245bc5f20917e7a3713ffb7e0ccdb7c5bef0b2488566a2",
    "empty/efficiency.html": "54eca8a5ff989ba203ad160ccb34e4c6f73e937733950ba09ed80696e4b7d7a1",
    "empty/health.html": "a8b3de06cf854299f782a37279d75faeb3955a853fabb31f71b6023857554ee1",
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
    "_savings", "_active_jobs", "_board", "_legend", "_TRAIL_LEGEND", "_meta", "_job_article",
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
