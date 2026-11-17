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
    "CSS": "b923e176904b6bdcb8759919f183e9f79723300cc221aa3808de8cf41952f4cd",
    "SCRIPT": "43ef667518896adccbf90738f1a3c6719324c48104d3afb2db29ada2c1c318ba",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "03336ab61ca8ab9ae3f84cdd053362a3c7658a491ab2a610ade67aff4c6b345a",
    "seeded/index.html": "24f69ef3372c9d794acb7439673c2199e74676af175e249f6bc97d88daddd32c",
    "seeded/job.html": "9338756fd835166d3512823c7ae9bcf7df5769ff6690321c288e7721025aad8b",
    "seeded/activity.html": "4d4d404a1e40d99a49ba603e4e54db41f3539335b86c5f2258b6e98e438f2353",
    "seeded/efficiency.html": "ee19d2328cc7dd53b0aa7c2d64f6bea7da7c3d235513204f5eafbf6e3afa2244",
    "seeded/health.html": "cb93210fadad3587bec735436b102b832134b13507ba58d2f24cefd0bb34d142",
    "hostile/index.html": "c12ab07278afb26c1da6eddf200dc61096f6ca40662c30d40bda7bdb50cf6185",
    "hostile/job.html": "6aff04356f91d4071d09334a14d89e4e553987fc255c0ae8c61a398b163b7a86",
    "hostile/activity.html": "c58fce9e0a70ba4c920f65dc48c50df75e4e134a785b5d144e71755a93ef89a0",
    "hostile/efficiency.html": "a6f46a0cb94d6bbc09a75441fa4b52b9861a291ed45f54c38aba8c9ecceddb55",
    "hostile/health.html": "b217c6fdd347688f8930d283c594f5fa0cdc9a1f09a46510ac861020cba6b326",
    "empty/index.html": "409e69373c2a72a0ab8fb7c37070a6c236f9565408ac83c4ade0bad7148f3d5b",
    "empty/job.html": "db8fdf255e49173c908329a153b7b3a2dd7a4095dfbba5cca45e79e42f110faf",
    "empty/activity.html": "c37d25bd3a9f5b93eb743d1069bb3055e2c554b743e2555999026bcd79c26784",
    "empty/efficiency.html": "770e663686a7172a31fb7efb7c9470c60fff5e49293e0d3c97086b0b6303ae40",
    "empty/health.html": "fcf725f3647271358d1507c65e73266b330b22ec6f4db1cad53ba3d4929aab93",
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
