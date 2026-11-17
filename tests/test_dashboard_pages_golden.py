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
    "CSS": "d0d8e37320e15ddb43dcf2ee020f91f68fed72615d15e0a478785bf60dcbaa03",
    "SCRIPT": "f105a6d809725c40ef554e12046cb0736cf22bc14e95159802c96b1d58337899",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "0e5c6be87daac2ce741b367e27f32d3e039a5ec4dfe53eb23c906ed0ea4afe09",
    "seeded/index.html": "d4c790456387a7185b6fed584f5f935238c13fc9a646793ddcd92672ae517531",
    "seeded/job.html": "6b32a7040a7ad5d5df979b327155cf66ba31538dbdd652904519815c3082381c",
    "seeded/activity.html": "c89a507d029a42b0b04733ec564b511f2deddfe7c5067b71874c6805a42452c6",
    "seeded/efficiency.html": "511b9a064c7b87351351d19a43048ff0c015b497695625d467559de324129982",
    "seeded/health.html": "e1381e0a55b84649879274c558f526dcb131fc72a6f0e5ead2c09eff5a687866",
    "hostile/index.html": "c43c4a9f85b726381d47abebccb7f3708fd0e72752d1c32a11bda1f852510100",
    "hostile/job.html": "ec881c275fc811d73c59a9ac7228bd6889de0bb7865fb597342a6ac503581373",
    "hostile/activity.html": "efcaf00f779304f78ccd36318273e0f9ef091ed6639dfce4a588adbf34a95879",
    "hostile/efficiency.html": "74e28750de114119a5ac674047d9bea5bed0ad9b3ffaef025ab05cbda295fee2",
    "hostile/health.html": "419fc7094fa0e841d76847558ad82977e2d74a089e1c4fe8426e4d986f74bce6",
    "empty/index.html": "7a0cd1639234652e55f9559a7e102efe5f6625d0e771bcbdccaae166f5ce5e5c",
    "empty/job.html": "e00e747203cdb01528c105b4cf9771c9e2d0448d7be4c792553149a76250ddfc",
    "empty/activity.html": "e240c3e8784da46fe90d3a77d021b7c43a50fc4c0ec39efb4c22734f735d90d4",
    "empty/efficiency.html": "5233165db6553780d871495b2520b5df6181406860e04143f9c4090214df5b5d",
    "empty/health.html": "d6cb48cd555033ac6c7cab9c8efa5e1166c9aa1ae7b7604e13cdfd59f3aaf2dc",
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
