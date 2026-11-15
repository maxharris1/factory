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
    "CSS": "4aaeeeb1c6c55f69fa5cbef9b078045f32a049bfa3b8ebb7bb12d123433e2264",
    "SCRIPT": "761dbabbed9a0ef0f378eace72e759a30aedcb8b25701b6d702c5eacf910fb52",
    "PX_SCRIPT": "b9353d15709b8b09e1de473712f8b909633ebe0cdfda2abcd987acf9b62a92b4",
    "page_script": "2a07d8d8fd1b90151fd7d54bee8d8abfb9bafbced6a8a91c3127f4ba7d40d864",
    "seeded/index.html": "c06038c0ad5f8c5ba6ef5401cacb72a4f549fb86cb3ea494458b14b961e5096a",
    "seeded/job.html": "eca2944adca7fdbf5ceb94173e6c20a58e593d9560a7db0652e563936e677990",
    "seeded/activity.html": "e31014d0ec870ef99f1e05ed9f064f69e6e1dce5ba054e01fec4150abd9d9f91",
    "seeded/efficiency.html": "14cdebce50f68d9f4b83e4ceb65b71cdc99756b4602a3e6299abcfa79c4e2cee",
    "seeded/health.html": "c516cb5238d06b1b59ccec850fb277737efde52626e2fffb31556ee1ea411a69",
    "hostile/index.html": "7e29e963f604658fd9de4843d5bf8857850da6bdbe44c4d21e9bcb8187d48c04",
    "hostile/job.html": "d5c42d295470d6f51635fa20148a97b5f6d8611938566c15a6c7257cee001a8f",
    "hostile/activity.html": "c14ecc289963676a794e0388130c7459eb1256c69d45ebfec034ffa37ae5f0bf",
    "hostile/efficiency.html": "97b81ecf13a79083b688294c1100233c9decba0fa38e152542c20be97e214b2e",
    "hostile/health.html": "417393067e0df10db532b7e7e073772135aed798ecf5fc0220cbaeba77faf936",
    "empty/index.html": "a29f79cd7d1fb937dc3dc2b3dd26e4139dc4e0cc7344c5ad953d3dfe6ed90b57",
    "empty/job.html": "604246e0ea0fb717efd38a88ee745312e4f7bb8cd55baedb60a0d8871fff9eb5",
    "empty/activity.html": "360942d371db9b1e2249d711c741c01e4473cdcb21fd42f1ee79b4a5f1dd5163",
    "empty/efficiency.html": "4f68f0cd03f4fa3039595d8ead2e95ecfc48965315f1f3131738d43f87cb493e",
    "empty/health.html": "ba6a56235bf6e5434b48843e9555d6ff0b3ceb44d27434b77ce460cdc29d9975",
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
