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
    "SCRIPT": "78999449d2063fa480c816a3ec6f5b492306d4027a0fd5d20f69b9b1c3f46d9e",
    "PX_SCRIPT": "c468396e0939fde787160777747955fb58f890ece54d70552df65458ccbe4209",
    "page_script": "e135f974493c369cfd894c884f45f3035e974dff097316ad179ce37e0fe289e2",
    "seeded/index.html": "7515320fea4efa305af08b283218ca31f36261a57a8bc1cfa0f71dc32356e30f",
    "seeded/job.html": "c8e5f353b7c09930dddfca8fdb4a3554ab5fd17dd872fef39151bbed0f810f79",
    "seeded/activity.html": "66c18a940032f06ff6aaef9da35ca8d0e98eec63466047852cd72588fb0365ac",
    "seeded/efficiency.html": "7c6d6570af3c35dd2f387908cdc0f8c387358cb8c5c105a53499fd747349d44e",
    "seeded/health.html": "85a021ccaf9f30d80dfc61984afd3ee9abb3a4675303610ca1988d520111509c",
    "hostile/index.html": "285609289a63b5039dda7025a5d3d89b791eeacb81ab1bc69fe126d24a2af395",
    "hostile/job.html": "6eb6dc7bdd140c07d8e8537853d74bb6abb622ad8daae0da23b904913a8e6d9c",
    "hostile/activity.html": "70d4793a0a5cb8033305b2390f337f82f1c2d14a23fa12c09e6690c0da47f8d6",
    "hostile/efficiency.html": "ead780f569375264fbf68d43c42ea062bfcd45104999529a4918a737a4599ba6",
    "hostile/health.html": "7c241191e5d67c8a90e29196e8a9c5aee66923b72b3d5cc479c98bc8a51186cb",
    "empty/index.html": "dab4a74021a7a5f4df31ae4cbaec45c6d0f6ff4eee9261ad5fb56d1331c16c3f",
    "empty/job.html": "90eff738e6fbf05171422e405a52f26f8eccad75e2c678bbde2ff67df2f3627d",
    "empty/activity.html": "7c0201049f6f598d6d102d2f1dcc9c1d82fea76c90567033e00db532dc6d429b",
    "empty/efficiency.html": "eaad7d8a25e502b51d1214896be028253614e56cce2e81c0b89328dd932895b2",
    "empty/health.html": "de0980f65551c51a8f3165ebe4ed44d55fb25a9b94eb7330ca86d640de88bfb8",
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
