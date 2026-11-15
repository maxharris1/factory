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
    "CSS": "5875e8d15ee66f92872b73f4851519dd48d9ec15f03876caca183c440c6d8384",
    "SCRIPT": "2a15b63cb081b6505cc06ee4baf331055bd0832b5bf8affeb2493d4f4ccfa329",
    "PX_SCRIPT": "c468396e0939fde787160777747955fb58f890ece54d70552df65458ccbe4209",
    "page_script": "37b13caaa23df82960c019d604b6397e119f522a470d66ce379a4ecf23e12008",
    "seeded/index.html": "3c1d2805c3bf60b0b9a75cf03aa161b5da2d0647b3ef216039beb0359b21c50a",
    "seeded/job.html": "c02fed14c6e5c77f6f5eb0c16039c31d47ed0fe564114992e4a224ccf556bc63",
    "seeded/activity.html": "fd4deddcb311ee739bb979d9717ac5d81bc3530dfab9c8f1d18ec5287d64c4a2",
    "seeded/efficiency.html": "c3c9470dbaff19b259e1cf3dea4601b8b263b30ce9fe7853859660a19f6c6fa1",
    "seeded/health.html": "aa47a355604e70690cf4a38ffeee7ded7da343c42f9075d23b94de9b37b69c97",
    "hostile/index.html": "f5271b9fd0ccdcb786ae2e0a1373e94aef9979a093e9a64ec4682d867583ed26",
    "hostile/job.html": "c9e5f7efbb2f6e74557fed9ea57cc42ebefa9a8eac1ea8fe858cc7bdeff696b5",
    "hostile/activity.html": "656c59ccdc45bb2004fefee9a5a3744339dd3038d32ac4283a9567b294b9ca58",
    "hostile/efficiency.html": "5cab8ba3815fd34cbf1715dad3b4b9fce299d687d4876bc49216158a4d6c9bb3",
    "hostile/health.html": "d6ba4114c5f0351400a9463eb2597024ed566e2a4cde6c3c5395568dab2a48d5",
    "empty/index.html": "c75cd38ee856a19a016186561788f08f8dee5f2f707708cc374337f66163fc52",
    "empty/job.html": "4e77afe90a89bbb261ebbd84eef43ac5ef55b5637d7b9d02e8ff7219c63e2631",
    "empty/activity.html": "d80569c726f2ab71630b8d7ea6740a59257dd471a937f8a614e582988cffee78",
    "empty/efficiency.html": "1799a2cd092aacdefab9a615e0e7622a92225739a844e9863aeb758a75d31cf2",
    "empty/health.html": "b7d6ed7035006d0d2c614f8cfade6f1f876637ce27f796af1ce06d38e69d53e6",
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
    "_savings", "_active_jobs", "_board", "_legend", "_TRAIL_LEGEND", "_meta", "_job_article",
    "_jobview", "_flow_template", "_PANELS", "_page", "render_pages",
    "path_geometry", "render_path_svg", "render_svg", "TELEMETRY_CSS", "TELEMETRY_SCRIPT",
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
