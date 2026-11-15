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
    "CSS": "f3b36208b02bf16969b3c346e59ed508ac90b848bd83f2a4bd98736158f48e1c",
    "SCRIPT": "449d3a7d242e8ae36ba13ac6b161d1e7eb287ff0c169ab9dac14c7f85001a562",
    "PX_SCRIPT": "52a0c0593d6d308d1eb7a1f1c0198b80538287b4eff077f24336f27d9872e746",
    "page_script": "9aa90acf9b3c51a66f223f9da76f8c435d1672d9dcd7aceb6cd95f7b31349617",
    "seeded/index.html": "0e050e6f063dd3e137767e57cb32127531b351ffe7e34fc810dc3bd7c4ca2bed",
    "seeded/job.html": "70fd7b039513dcb8952eb92d2d63ad6a8b86fd8d8692335d774a408d5a0bbde8",
    "seeded/activity.html": "c11e8c024e4057f08ae1788daf15e36500038dc2127a2e65b43ca858205b89bf",
    "seeded/efficiency.html": "e858f2f02074c0392898d3f613e09448085d9c672ae4510aa0ac87057d5f9c1d",
    "seeded/health.html": "ab840f50d71b98c97f86e29c62dfadca1a5f3ebf0d6206aa0c23781d68701421",
    "hostile/index.html": "c724abc2890723c14ca6dd562ec1af906bad869447136ceda64850349ba2509f",
    "hostile/job.html": "0cc5ff85a7c2a8dd7a7ff7122c8e4f5bbd7b6f7f2720dcd990bf0ec4ee17302d",
    "hostile/activity.html": "15b1c03a925b72e53f4c0168bcacba89fb3aa1bf4f18b9e59df083d4e2f80d93",
    "hostile/efficiency.html": "bb900d9b9a0087553c81b57a85bff19dea2ea7f4ac988f884c81daf67ecf1518",
    "hostile/health.html": "176fd330b1d0a74f713de6f19e0e1701edf581dd543ba19f15afd0a296d2992e",
    "empty/index.html": "6b4509833de0aef5ba1cab95c2feee6fe1c6b2da8e98a55889be7717d16be086",
    "empty/job.html": "03c4574a37ce5eb53283ad6cb746fb5b95b3540caf829a6e07d191e4c5b59644",
    "empty/activity.html": "1d599874d62ae54b08c622d3267232889aa287645fd87acbb306e226497d39ab",
    "empty/efficiency.html": "2f4c6ee85b505e2ef59caeb094c78d3c7884a6a5449121f1545981b9812cbf37",
    "empty/health.html": "7867859814489b856fc5ba1998dbc76ce05b9ca066fd719506e249c60c237b22",
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
