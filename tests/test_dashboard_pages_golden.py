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
    "page_script": "53217d760fce2ad8575c9b8d65358c5e8243ff1c31ecc2e3187ab64d44e3d4c2",
    "seeded/index.html": "72493ca4b6d131c692a4b7937745ebc7e3dc6dd39be38ae31987a8a0ba6086c7",
    "seeded/job.html": "4d3b9379b12967534bb8b6655cb2405965f4eb3fc5b07a8aab38afa1995028e6",
    "seeded/activity.html": "b1dcc9dc3c8364b2c2e5e326fc8fcebb36a0b636bb21019fc63cda1e3949b846",
    "seeded/efficiency.html": "4de28ced4134fdadb98edad179de3c488ac0cd658ca0bdb435b4cf1ca098db8d",
    "seeded/health.html": "773030f69965fcbf181d02b01f4f5448600ee583c515ab0ea4521f9ce1f26cab",
    "hostile/index.html": "ca66041439c866fd3fe192f423563da272fefc22e42a7761159ccd43c83117b2",
    "hostile/job.html": "32f2564f04ca307e17a727c6a77cea9c6a1129dd8f3058982f83c90b1271814d",
    "hostile/activity.html": "d07f9abf951004d63dd29fe684d12f2ae6e2605af3723450b84593057bcfc32e",
    "hostile/efficiency.html": "766b98c5d248e181cb2725e5b293d35f255487b568c62c3edf75753a31c5b1e4",
    "hostile/health.html": "3f3cbd2ab6bdc1c89d778264cfc4c89a042f9671db24c67e76144672a635f267",
    "empty/index.html": "ec21ce9b56c62a1f958b0e137d49ab1ea0c73cad447fe5ebba48bc1cab031970",
    "empty/job.html": "3745665423cc618eb141163d3255a7b60c75c6fb518e8b3e77f65bbbd5ed6821",
    "empty/activity.html": "86fec0241a7c34a4c60969a5cea980b2618abee040bf22fc8f505f2ddba155a1",
    "empty/efficiency.html": "cba7bd573317312d3386d2e2b22dad57668821ed470176ec063c26c8f5e7ef3a",
    "empty/health.html": "8f4d5a03ec6be005c36ad91596e07c99e10472572df0f7488124de8afb444bae",
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
