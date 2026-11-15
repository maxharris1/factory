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
    "CSS": "f38615213d2a544d8759304e378761ce8c529758b012e6502ab55c0588a2f971",
    "SCRIPT": "f52023bc5a4b935a21b4ad4fa825e9db89354be9f078173e6d178bb0eedf4d65",
    "PX_SCRIPT": "ba6cd972284b80cd4fef5ea3516586058e3be22b65f4cb48478f60e36ea47e3b",
    "page_script": "9dac2266e2e38e909563e20206257f8c8a518e4104ea6803ff8c0a615a2ba5e0",
    "seeded/index.html": "a51b1e114191356c43457076e7e79fdecf33d43980e39970fb88fee8928fd485",
    "seeded/job.html": "a223b67c7bae5c15366ac1a2fb958e93fc7816cbe8b1d6da0599e38390e664c0",
    "seeded/activity.html": "6696b79b52a6821fae9fb57f1cb983bd27a1ed823f5c609dab1c3d6159ff9042",
    "seeded/efficiency.html": "30f16ff0c127eebe6a2f49057e962a78cebe30982c85e9b69e9e1218ebfb71bf",
    "seeded/health.html": "69426940795d7cbfcebe584663b5544a562ce2bbe9c83785fb72a33d75ac9478",
    "hostile/index.html": "feced0d3d568030267b3fac9c28a922e614729282621b8bf25c31bb5e9ee1e2f",
    "hostile/job.html": "8967baf215a1e733baa269dd20aa57dc50eaa02f8a9c3f6efbc1fd4157a6cead",
    "hostile/activity.html": "76aa0f10d22e1ed2c420cd576773beaec8915bd7acdb7f9df0a185ac679bc9b8",
    "hostile/efficiency.html": "4ff854258d7c7c34682fb450443cb664957c0c2d4e4212b3f7de5d3fb36145cd",
    "hostile/health.html": "7494a58232cc27e3ccfa7bec61bd100650ca5e8ea282503fe6be6b44229b93ef",
    "empty/index.html": "93db7527b4846628d076dc5f518f897e1ce50d66755908a3b0b269d359185a55",
    "empty/job.html": "1dadb2ef3811b9a8a385f9821fd372bd5a0f0411688fe0a367c60864cf7ff1a3",
    "empty/activity.html": "d58b6b3ddcbab048a68b0e45fd32bd196fd84b4edde668c5c817e234512c64eb",
    "empty/efficiency.html": "c4d18f16a0428d21b88e84ad2aabdebe6fd6221644580624a90a38aca79ef654",
    "empty/health.html": "b563fe4066fc94d1589516e245a3ca3bfda4abf5ccf3d6b86983162425c63242",
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
    "_ATTENTION", "_HEALTH_LINK", "_health_link", "_attention", "_HANDOFF", "_handoffs",
    "_VERDICT", "_verdicts", "_td", "_tickets", "_log", "_POLL_STATE", "_BUDGET_STATE",
    "_health", "_HEALTH_STATE", "_ANCHOR", "_anchor_attr", "_components", "_no_waiter",
    "_savings", "_active_jobs", "_board", "_detail_model", "_job_article",
    "_jobview", "_PANELS", "_page", "render_pages", "TELEMETRY_CSS", "TELEMETRY_SCRIPT",
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
