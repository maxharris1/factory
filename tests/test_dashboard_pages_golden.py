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
    "CSS": "b0ca298e8db21a7f41718ceae17181c90269f02c1ef29ef48f83e08b33425280",
    "SCRIPT": "139f1fc1b6bdaaf21cf87f82d686c6530c88655dd9c008525bbf2196a47bd84c",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "7678889cec6d0c192ed03fee66f2eaf7eafccb3075d8a97591c10fdcd89a1efd",
    "seeded/index.html": "7c45a219f349bf87ef062facd78fd700c915cdd95acfbec6abaef72fe968fa1f",
    "seeded/job.html": "eed82e5cf0b93cb0cc8bbf9069016e66699ec25e8c4c951a654789ae9b45b3a3",
    "seeded/activity.html": "2667ac0f42bb4d3d89fe786ccb9caf882dc3c014a7f9a3c5da62b580238f4752",
    "seeded/efficiency.html": "64d88dfd2fda6f69fd0c0bc27a950f1ee19b1c9047c538713363337f44f366ef",
    "seeded/health.html": "dbee167af5dab10ad012ff5d7198ece0e20bec4d3b5a4867ef19d6c29c555325",
    "hostile/index.html": "41c960de081569a61e324467676e26271c87f8ca4d3590447c1d815f499000d2",
    "hostile/job.html": "cd06ec70a6ba4f9da1164a8c6bcfd335027b710bc491e72033054dfd2244573a",
    "hostile/activity.html": "2e25a9294ae91440f6ca98abe3ed8bdd2b70113c7b92e8602a7e3fc1a84c1254",
    "hostile/efficiency.html": "ab3316b7f3d1e9d7591435b2ce7a7fe8b83d3fef17f5ea0099599dcfa46eb8ef",
    "hostile/health.html": "068aff4b78541704ad17582725ab16742e5e2768c3de52b512baeba1ff50f6db",
    "empty/index.html": "b0192f7297d8b33d6b576666c7d3e943787f4d8ca0660eb9c034e235b04d3679",
    "empty/job.html": "c19637ae308186bbaa601924d5cd2b2691e0bb7f0755cc0c4a807860105510aa",
    "empty/activity.html": "499be08148c16e97e468d2b5f2355dca12969046f81a5d1c6c99254a4e096089",
    "empty/efficiency.html": "35db83c5c1cbe1ff239e6f1a82b2a69175f0ec4daf51075e7f46470677aa20ec",
    "empty/health.html": "00ff72e318575f297a043a51f4df44573cdda09247c4ba1f4379dc64d4f387d6",
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
