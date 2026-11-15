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
    "CSS": "d873908b2d6d1e1d7d6d91bded11054c2a44940fbd1beffaa63e2199d27ceb40",
    "SCRIPT": "5eb71a564f839aef58de2e93337e41b35d645c14fcac0f46eff0421e36246597",
    "PX_SCRIPT": "abed49c9c90132b883215c9431b1eece5d86e054edd09468199e938c40e8d18c",
    "page_script": "71faebef01ad6a8a513f1aeaa882428fc703df9c97c84a4548870fefa10653eb",
    "seeded/index.html": "76d1fd34f4c666a7560117e4acfd82e9e2f6205977dfc5f8b7b66941de0badf5",
    "seeded/job.html": "f5fd21fe57f4928630f9930179feacf0ec8f58dd44c8b52c9a145e6a0bed8270",
    "seeded/activity.html": "96652953d165d8116bec6d6efe5c4f303a780b09f6444aae5070723349de3fda",
    "seeded/efficiency.html": "5729e9737ef2ff342169342d1db9494fa7b72444765530e0de5da6d73a81e65b",
    "seeded/health.html": "efe241441b188ae6ca8e8914dd20eb426f2c38ba7cb36b5a9fdfb72e154e5971",
    "hostile/index.html": "27740bd1806df35bb91bf2066f93bcc9a81a8b7764b654e6f1fd5459b77d8b82",
    "hostile/job.html": "3efde58ec1bc06cf1b76c06562e8587e7d87eaa0b864d71852dd5d910dfd7242",
    "hostile/activity.html": "a9449110f36a0fa02bff07966dbcee1cbb6bc31dde4b28fa1e9140c25bf06faf",
    "hostile/efficiency.html": "55d932c60ce69a1d510561d7e624c8100c1ea9a0ea363d9cfb46d90c329c1016",
    "hostile/health.html": "97eb6c996219ce31360bc3dc23407ac17d024cf4d949369f13a568e534bdc85c",
    "empty/index.html": "3af622666c1ef40672fbfec0d3d770d74192dda1db03974bec36dd6500b7a376",
    "empty/job.html": "be882d574d9e6e4172d62efe63435ace7cd05c58b421c10f77e6288f2b887deb",
    "empty/activity.html": "c0644002e72258006ef9d354aebfbd6331cc28833c84a331edbed6d8656586bf",
    "empty/efficiency.html": "f364b74bbbeb445a9b7942b7911535d19a437ad61eed5e952c8a10ea0dfa536a",
    "empty/health.html": "9e51a4df0b808a2e21fe8752df61abd3ea62a6548e3c5a81011d429b4724b235",
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
