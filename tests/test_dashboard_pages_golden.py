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
    "CSS": "a6e5f1d8959d0a1f52b2ce8a6af28707f07ecb684b43824de61525865be29bce",
    "SCRIPT": "4ec3442cd6566dd0b6f223e5b46569205c482caa68f370b74245074eb7d9c5e3",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "27dce8adc8e7b61c63dd6344de17e79256c1dbb6ae8c20bb84d20606d6b71a98",
    "seeded/index.html": "91a4444b0cd42d3b98c22ae7781dc844fafff11334c0cf4264bc5ef7e8e83812",
    "seeded/job.html": "08ed9079a43f2d34bf3f9d4e757278f3e8a3f10139e441d49ff421b83c07c823",
    "seeded/activity.html": "7f7207cfe74efe46359e78715427409117b077608e2ddcebeb63b047881065bb",
    "seeded/efficiency.html": "b29b73affe82010559b1e2f1cb07a74cecc56bb53e298bbab3f129850699ca9a",
    "seeded/health.html": "a4015e573bcea4c3cd7e3588dfbe07965d14be434584d0f474f3a2f899149dcc",
    "hostile/index.html": "1841a19d9519024017275d868161480bceda032fd97c44d6ae45d826a138c656",
    "hostile/job.html": "345817cbb1bddd13c14a0fadd3b1627b5f6a63f33466dab072f89bffa4729851",
    "hostile/activity.html": "554c817c7ef76dea7d5a7abdad6501537cd06c87b9b5d16cb8da1d3cad23f6c3",
    "hostile/efficiency.html": "3e8bbea423471694a7511e5f5f2c41e6f08c5d46e80ac4692c7b5d7565803f46",
    "hostile/health.html": "3cc862811d720ee4b8981f4a35e09710d6d44cbcfc41a3e5580e327ce1ac4326",
    "empty/index.html": "6ff3d7b06462258becfb3880da3fd6f9c5e025039475acbd9f4c867d6fe1d1d1",
    "empty/job.html": "956a479db8243fe566f31eed733349a6ee4e737d70aec83663cf3ba11a14abea",
    "empty/activity.html": "df33948b6dc8cc5ca2062d65f9837a2fefc6e67a50d7bbe96f180d6392919215",
    "empty/efficiency.html": "9d532358f73093b3ada57832ae99a9ed110fd351696e40e61c976878e95049a0",
    "empty/health.html": "e2f05648e61259b299dccea1ddab3552985d56887e1d9bb30fffff8f5bba57a0",
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
