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
    "CSS": "099a7a3c86c220af43d205a6e19089834403609f58005153a749a83c76dd60f0",
    "SCRIPT": "46bbdf59033f64f6ccfee5683f4022afb13b81970750204e9b2fc620933c8d32",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "20b54b26905e844fc13b3a504c9fce72d34279abf4748f527dd188d2fa3a3572",
    "seeded/index.html": "1009271c8bdf9619c8f38d2387cc46828eafe0852b3c68e66bfba2fbcbb8b268",
    "seeded/job.html": "dd825ad1c7eeb2e6b4f003421acde96ba0422d99892b441b9fb132af88b20c49",
    "seeded/activity.html": "02a7968f91048e67ea504063bfa2277461b2d84b7bdae011c5754004e7ed8d70",
    "seeded/efficiency.html": "61447d0cb0b0ba159dda6854b6b632936ec8bd89468166742861e149e883d8d8",
    "seeded/health.html": "1608305117e2f6058945b45bce2857e31b73e8b967a39f88c91256a3433d18eb",
    "hostile/index.html": "06ac67cc30ebc4ca842ae3b36ee76f4c95de6c54754cb5969696492426c49353",
    "hostile/job.html": "dcc3763a012f0ef906a2523bda368411bf16cb203e07f744ff3a4e7d9afc82d5",
    "hostile/activity.html": "45891708175b2699b5a050115100453e8c40f51ca1ffde0ed587e377622b37d9",
    "hostile/efficiency.html": "3aeb09c9f4b2a4ab633390f5b013527e3655b538a0bcffdc1ff0b628b3386a77",
    "hostile/health.html": "92c64520cb785d70ebec50ef3be8ad64a79ccc9faa34766a254489b6c2e45858",
    "empty/index.html": "bc55a266c2f02e4dad868602be907e8dd8271a725b59dbea50900df5faabb1cc",
    "empty/job.html": "ceb26683483b6d8bb98901708c9451a1b7bc010f62022cb5dcf62d8e49d3cab8",
    "empty/activity.html": "12bc0da0500bffa3d3c1167416a131e90b95f3c69c7d931decdf090002b31169",
    "empty/efficiency.html": "7cf70db6f6b43c655a34f57a7eb7c199995371f7abc3851b50b55f751e98e155",
    "empty/health.html": "9b23afca6091a6b92adb923a84fb86cf436c9e09c442a344c3a15430d8482417",
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
