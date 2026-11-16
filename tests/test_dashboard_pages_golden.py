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
    "CSS": "1c8859ef0f4437f798f3a2f1091f4a81881ef8e2429d44d6a418aa6316eda736",
    "SCRIPT": "8b347499740a87ef68a812aa89665559c046a3831926b3a935facfafd4c7ab4e",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "649c58ecd8a5f47f550e09289ecada627e3a8670719810d5ff9936153623ad8f",
    "seeded/index.html": "60c8e9ae2f034766352b9f54991cb41f1f2a2e18545a7e5452d59846734d1b5a",
    "seeded/job.html": "d183e3f35cc3cbbb4e89036b196d3c0d87876ee4a1d134beccce8d70642126de",
    "seeded/activity.html": "7f39e7cdc3eacb1c9a1d5543bf51657fe86503580dc2d6279efc4f322d17eab5",
    "seeded/efficiency.html": "d25c4bfa2b9b308f98bb7be12b49e13e7e55e283ab659208a6e1dac9c202ed72",
    "seeded/health.html": "8629dee4cb10ca4e43776d26377f0abbcc11a1bba28fdb62be3f4e2386217a1d",
    "hostile/index.html": "ab3285cb359dccb56111b23597f4ebb07d3494f7ab989ec9436b67d5cc215138",
    "hostile/job.html": "19abf98a31658aee1f9f487b41cd469d5e0acc3151e53a9ca4a5aef2f3046307",
    "hostile/activity.html": "e7b3497abcdf953c21c42f06781be8a0621419cfea095738552d5d644bd8b18a",
    "hostile/efficiency.html": "1c505b122e101bdd9e8536c4c06d1cdca212f6f1c469bfb0ee9f2e6b1484b173",
    "hostile/health.html": "7fd423b23693574f4226848e9aa34cfb6d2d4d681cc328b9563a08573a8fe8a6",
    "empty/index.html": "66231ad61e4a4085cb92895de6b2b0c8d982706546ade89a1fc71aa32ea8fbc6",
    "empty/job.html": "2e3468346f7a53004733793555fd270e2b59be7fd450bb211520cb45cbd54b14",
    "empty/activity.html": "b14daa9757a528f834798c71a0d52075cf4983b07d1d9ec9a9ba0dd30e1abbdf",
    "empty/efficiency.html": "aeb10f18b7472d6f102451180faaf7da1aaaa55c32aa062e601a20c68f9a3e8f",
    "empty/health.html": "55604367ed299c574780fe486e1f0fb9410edb542b3b24768528fa41311d4f5d",
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
