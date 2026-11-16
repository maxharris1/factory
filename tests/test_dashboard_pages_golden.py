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
    "CSS": "08c3328de5abc8d81f7ce8e17ba0c9df9d8d99f9b495918126f4a2310d2ed53b",
    "SCRIPT": "30bb253817b1fb36802711ae927fbf15a5096ac5f940902a9d075c23af47374d",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "7ff1b3e9285f43d44ed55b779c7d0d8db9a7beda80e1d451e5849122e51f196a",
    "seeded/index.html": "8f401f7d31e4271735da70482c17ae089315d3140a14e43914dfb7466eb234d3",
    "seeded/job.html": "ef4b7d0d2f1ef338b43dcaa6b76197dc22389c59fb6c0f6e6e50155371626f6c",
    "seeded/activity.html": "e2d0828c20ce9678e28127c270dfe68bd8aea82277572df30619b687fbf369b2",
    "seeded/efficiency.html": "100842dfa1fde97e858f4f1f840f5b1a5f924b385ed4936696dafa14055ab4c7",
    "seeded/health.html": "a08ece469f4802a75ca07a68ed5675c5aa724229977240ff16e57961c5ddad50",
    "hostile/index.html": "9fa736944761109fd5ee4dab7b40f25c8e9e5031d624e10809759c078bcab72b",
    "hostile/job.html": "0758b9cb470ccb439fcbd8a6d52c8636a6e947157c6e567dca8888498b3c87d5",
    "hostile/activity.html": "b524b7f99a9864568be144634a2c33d3ae1915244129fae8def96eb6db6fe5c2",
    "hostile/efficiency.html": "5e3d3aa7ab5a9c6af8afeb711a02dd14671b4823510e1151013780ecc1cffdf5",
    "hostile/health.html": "c4e9f28a9cbb6b73cc9b16e9181bcd40eb17d784be16951dd39851eae0f39cef",
    "empty/index.html": "36a765b1533ff5d78a02e1cce94a8762011fdd42773b957f077f95f4daea530a",
    "empty/job.html": "e369ac0bd79cb7133b87cf23e5e8a83f253387d92aaff889ed28d695bb25127d",
    "empty/activity.html": "ed2ad08c3a2e96f8e4d830b8bc22de82171c4950f8c9b28b4d023f98421a05b7",
    "empty/efficiency.html": "52a0bdb2a97263fabb96e23551987e305510866aeb0a96d77fa6ad76a36498fa",
    "empty/health.html": "f51b655325a993db1f3ed47e0e99438cdd8ce8b641b6c21c14c1ff1fe6220441",
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
