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
    "SCRIPT": "1fe229db8bea6d347fb6e3260fe06d893233d164bb6c69088ed125ad7ffc3a66",
    "PX_SCRIPT": "9638f3995b27e67c16e5830b74f8b506fbd48c79258e7d04d40d95e9bddf1d98",
    "page_script": "cd686bbed3f745d654b5e86d09ab248c36af4b891c853ded2382a35143e69358",
    "seeded/index.html": "2c4b4b340820bb4032ae61eff5924a8491451c97614dd0af70dcddff683205b3",
    "seeded/job.html": "d9fa7d13b8f2cb99c933d959b134cde1a6033b58649df2001a69af4abf1868e0",
    "seeded/activity.html": "552304db1820f313a6b128f9bdc447d10c19479ec582e11def1e38d66358d701",
    "seeded/efficiency.html": "a41e23e5eefeadae008412d7cbd3bad388eb124b7d017eee5415f03beb7ee88d",
    "seeded/health.html": "d048a338c611d8782bc59706ecbb3bad1834d1499812327b010fd5a3fe521ef4",
    "hostile/index.html": "fd85a9d48b8259164acb93e51b01e87055b6e785cbf851af200aa2e93287489e",
    "hostile/job.html": "c7fb871e975887ec49f5df99acf9da21c27f4932e21304f0703fd7391377d4c7",
    "hostile/activity.html": "8d0127b3a5cb214d9a43341b97a51c54ec6db34f2bd587b55271d49ba35831de",
    "hostile/efficiency.html": "05a42fec08dc178021063f6c1169f409a37dcc08d297627df7990316436e1e11",
    "hostile/health.html": "ecbc97da9e498ef77559de8f0c1a15e67fc36afd4b3028408ea97410e1d20b67",
    "empty/index.html": "1d04dc4b86885f71c82a04df854dc1d08b915746a11cbc613f7fd4df60ed385a",
    "empty/job.html": "831137afbe467ad4620ccc38eaffed49ba5e01dacd7a796637edbf96ee16943c",
    "empty/activity.html": "9e64bb60a51c965dd8314f4015d7888ed874edd7a99bb717076a55d2304e6a5c",
    "empty/efficiency.html": "2e57af91654182577d46ee3b608842c1cf71aaed827baf17fb5338661eb9e9f2",
    "empty/health.html": "f01d7d08b1cbc52a4c5e0dbb64acb7ac0b0142ab29addec0f164016a94543009",
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
