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
    "SCRIPT": "4525a131417051d1185aa1af5469e0e2f76177cd36a09c5a6a05495372a468db",
    "PX_SCRIPT": "9638f3995b27e67c16e5830b74f8b506fbd48c79258e7d04d40d95e9bddf1d98",
    "page_script": "1285b533fd0810166a47577b78841aaab7a73efcf80bdb83d8d4f488538e179f",
    "seeded/index.html": "997886ac665ed8b95ec45b81ffc31a5265d0238f731732a6b15dc9cdfd77d97a",
    "seeded/job.html": "a0113813d1f8fffb26badd35e8add796ee90d541d26da4c8739362a500168734",
    "seeded/activity.html": "612a272a30b2836315a975ea30bbf807e6565a2e899a157f6324fad8cb8d5d10",
    "seeded/efficiency.html": "67d8f15c8c5bee8d90de81279dba8e8228e0004d56e5c0cd07adcd1c2213a6e3",
    "seeded/health.html": "0925f6961ac92d229f6513feccd20dddc803f74244446c35e82e36e065900f99",
    "hostile/index.html": "a0e715654e4044377da1cf5f08ce9860fef4c9c6e313836b984a9d3255c1e108",
    "hostile/job.html": "39f506ff0d1a7195822fca186b49bba4878f143061fd212b777144f4f821c2c7",
    "hostile/activity.html": "aaae12037a65c1ab0ab75ebd0f4750ec1beb89fe26a82a370fc77715341b44bc",
    "hostile/efficiency.html": "043f9af5b34194e078080e4c8ba7976ddd58cb765fa5a82ce200504e7245026d",
    "hostile/health.html": "ef21eff01ddfa3fbbce6d4467b264bfeac1927f8203ef3a696cafda62679c43c",
    "empty/index.html": "d6c97ea61ee4ddb42d4cc00030530737dcb80f745b22f0d5ee3c322f7c01d833",
    "empty/job.html": "20a1800c58dfe1699f7d4a016e3cc87238d2979486f3d8f4a58fccde36d2c68b",
    "empty/activity.html": "826ca92fe431f30f26ad2e3e34f0db61bd15ebf2e98626f831123f6d32849ba4",
    "empty/efficiency.html": "d56c1bb5d3eb0143f3c73906235d02c7294d90a6a85340a10187c305bf4a05a7",
    "empty/health.html": "37cbe636adeb8f4eaac144b6d275fab89a25105e80ee4ec3e913214cd3ca15c0",
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
