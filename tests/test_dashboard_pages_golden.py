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
    "CSS": "4516e4e5cf35918cf4e4b3e3347c8f71d082ea9de6158452b9e0202ef3a2e5eb",
    "SCRIPT": "66fea52fb3d8d984c8c10992de97263fa1fab0f241012aeb4828db4b8cba8c2b",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "5a63c72938c96514d6c5f76a2f65d734e3340c6c41fb799c8d94c87940189233",
    "seeded/index.html": "7b1d2f4ce8b3f49b5388b82ce27d4e32c6581d3a0d734747fe61c98547df452b",
    "seeded/job.html": "f6995fa6ba9577b8f1dad0e403934dc7e5dd77aa4757e8b9c6c035e5a4e0832c",
    "seeded/activity.html": "7c0ba199c2bf8d13d421cb4b82243084a6a00f17f63153e2cb8c1f64648d8ab2",
    "seeded/efficiency.html": "a04cdbde246a34b9b37dafa2d664ba54824525ff33685f474e1865eabca5b560",
    "seeded/health.html": "7fba493e851526492b9928f744c31b38a82ca96d0f086655bf68a73143c0c92d",
    "hostile/index.html": "943c4cc2a6665e5597bb035b5f2f0768cde117a84dc975a7ff3c74f0cb2ed10a",
    "hostile/job.html": "478dc5332b8a15d1cba395897c75246c4eacbaceb93462da3bc5e01041386d46",
    "hostile/activity.html": "201d369a80ee768073942343619cb89e970cd3a87bc1bb84a567caca0cc21bf8",
    "hostile/efficiency.html": "0beaa667a24182e126c3cbc749fa4a5d179b81b5de314813c49f51867b9ceb0b",
    "hostile/health.html": "ceeff8a7981f008f68f7a0f1ea2d36d7ecc292a33cce293606b8a317d162237e",
    "empty/index.html": "7fdb86bcc8ae7fd8349afa5f003a60f936606fad47a71044bdf28af5b37e00a5",
    "empty/job.html": "2624747dd9c283f5e5b40e7a04d0fee8abaf3f729fd271c70b61dd15b533a32d",
    "empty/activity.html": "8d3a7fd9f1684f2f2107b17b65331ec03d035c9ed5cf3b79280a7ab6f5180213",
    "empty/efficiency.html": "a1a0acf34bfca33517f577684e7f47024aab4517d15aef481058e3ed0bab5194",
    "empty/health.html": "a589a60318c36077e1215a254c5d8614d861edca4090fe81d7bca6723d55dba6",
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
