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
    "CSS": "902ae71614cbc5d3274edbfc1c5391bbd6bac84eb9c282bb16477ce9ed210c15",
    "SCRIPT": "ca11201ab8f9f1d3e963b1ded7ff32695ca99496ecd50dd5393e4d6bdcecca73",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "dca4327f19dc45d2fe1e5c60f0ee80a94dfdb32ac2a67c668c9c2672e49c0977",
    "seeded/index.html": "b53ebb45910996a03a393da21730251695f881fa6ddc09e4e7644d2f0b3a2370",
    "seeded/job.html": "098a90c2425e46b4e39e12e3f1f1586ed9902cdfcac221e1db9126756aea4227",
    "seeded/activity.html": "b4289c81dcfdd49f63fb6e11d3bf062159ec4743ea04c0da5d93b2e007d3cfa0",
    "seeded/efficiency.html": "4a2b5b8e117e1c4de69820a59aa15433f8d944950b64ca57e43f3c1c4ee51592",
    "seeded/health.html": "0155dc6690920c890ec8ee8970e6b15a40402248bcf3cc4e30d2893038c8095a",
    "hostile/index.html": "0d444710a436dd02a0f769ba222a6fd435afb13b347cc377163e15817f4027b8",
    "hostile/job.html": "0ad7c23109ba81dd0ad046cffa909d6da3e35a9c538878c840c0b2cfbf13bcb5",
    "hostile/activity.html": "ca24502f93d96abfa2dd98567c91308f629ea959b8f8663bdebe38b2ef0a6f46",
    "hostile/efficiency.html": "48d0822b8858645db2c17156bed5628119e675a356b832b465881956757d7fd1",
    "hostile/health.html": "3f376e80a06a3aadafaca0bc7d2ab9fa492641aeb216a856cefe17fe82521b4d",
    "empty/index.html": "11530521336c433becb837d0db8c2c5df6699ca7c81a52d46d214299677c2fc0",
    "empty/job.html": "f3c3977cf9039641aa89a6fed46b2a0af865a2f8b4c6e3b4650b18048f3b75df",
    "empty/activity.html": "f641cc1fb2a8f1dcba93284c78d391b85ce2ea008d1d84c9c6660d7ced62a5a8",
    "empty/efficiency.html": "af4dbc0af076c64d46b6d15d9378fff887d79b70fcbdfb9b1a348d1ae3e72d50",
    "empty/health.html": "b2ea31aeac0a945b24a5eef456683733024b3b03844269950cb5bd3007d34e47",
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
