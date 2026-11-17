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
    "CSS": "50eade6b75e4e4a7032c335e58914d4bc653c76510069b1d57bfebd8af5068f8",
    "SCRIPT": "2f149a602b877f863458b2cefb370e576e2a47ee8c7419ea7ad9f84cb192d99d",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "5cb8b12560db33bf2f3856523faabd8c82fc7082ec4de10b16a86a527f0d4add",
    "seeded/index.html": "825c7f9812de0f968b2acf131626cc952d77965b59ecf47068a1c45fa3889da2",
    "seeded/job.html": "e7dcffb6eb9b87d22a763f656eb5756c3715743e21749a8ed350dd1e1ab56d85",
    "seeded/activity.html": "856fd838085c2a3f0a763c515c39f778e1fff551f581fc28f3e5d7439b8ff9d3",
    "seeded/efficiency.html": "941c0166eb2c0a24ec092cc3066f2c5ef64902b3f534cdcb07738017693f9325",
    "seeded/health.html": "a829907da4a5803bea7854a0aa75eee9f060a055467b133c9a5a5e0aee61f8fa",
    "hostile/index.html": "af072087bb5f2520c50c9a08d2d3b0a32ca02a11d271a9cb73c2eb4a3ccbb2ab",
    "hostile/job.html": "b03f88250383c556d7993419624bc1e244e9bb98b7d9fda264ede6d3797859bc",
    "hostile/activity.html": "79b552b176c81d85d33e9a23681bfa1afac61fe64ccff6d42cc65d3db0e1a903",
    "hostile/efficiency.html": "9cbbc9274b6e4b2ba731a38419b87321434e456029fa0ddbfe3c2a221e3cc23a",
    "hostile/health.html": "fc1cca54e9a29f63ec5b338072f6d9344d15db471d9650971986e1b335ed3e23",
    "empty/index.html": "b4669835d64bda5b4cedf3e24a233307237d96450f464b5dc6038b8b13f7775f",
    "empty/job.html": "d5fc3e27a29755a1f40671027bec7f701d81906060d5b860d32c117d417d4cfa",
    "empty/activity.html": "7c9a7be1cfab1c0f4e2aea8fd55772cc371c35c826d1393ec4d97c6fafc481aa",
    "empty/efficiency.html": "d9dcba732a50d484e66471f923646fbd5c07dca52938da3d6e8f9c6e5fec7546",
    "empty/health.html": "f2abea2793612e0bec05f28ef65abb8cf323f8d8f5a34cdbcba0cb748a52f119",
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
