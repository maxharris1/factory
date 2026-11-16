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
    "CSS": "6c59de2e7c62a62ed27e21bbe1f249ff9756b1b7bf45081e4acb00d47e420f00",
    "SCRIPT": "30bb253817b1fb36802711ae927fbf15a5096ac5f940902a9d075c23af47374d",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "2a8b4f71a3925d9cb6131c9cf11760f9f070ca3a26a611f7958a25ebd30f76d8",
    "seeded/index.html": "2ab6d972398365a178c8351ff66aa68fcebf2d679b21e23febb492d10a99930f",
    "seeded/job.html": "730506fd9c844d45bf01b9c4529b344abec3ecb6d31ecfe963e5ee68ff98cbaf",
    "seeded/activity.html": "d6a15f66ea1693dcd2ca3b78438018e6b0ca95d67d4f0f8281832750f99c20e8",
    "seeded/efficiency.html": "6285377e8c355a3a43077078b364b308c1665b1ae779eb4190536100684d8e45",
    "seeded/health.html": "4eed6383c2cc9de72f06d758cf34aa399a91ef8a5dbace31564c3eda43148451",
    "hostile/index.html": "479af2cfd2f965f426edf62403c454cf0bfe8062d0e4a93f4856393ec0430dbe",
    "hostile/job.html": "4db46ae7fe6702c47fe88fddc06c6757cd83a9e8ab7d6d462ccc84a2edd3b93c",
    "hostile/activity.html": "1b96e60f9703ce8c842b124ec6715c4db78e62aad7c61d730ff049b0ecc52b4f",
    "hostile/efficiency.html": "9e76695de86aabd80599d42c22340aabcc64f3c0a4672b214eda467f818a5b8d",
    "hostile/health.html": "66a84e039ca33f507fdea0e49628b7c4fd24299208084a45e95a57b7ea732733",
    "empty/index.html": "5616adebb119ea972b5dd82ec029e109032191806cd0064b42b812caa68d72b1",
    "empty/job.html": "fbd41ab1ed3d5573a3bbe4c6d17f5715b8e002e24be11b77c43b1dd4ef1d23d7",
    "empty/activity.html": "6db57798656cc8f1756b69cea34afebffd3cbdb9ea00a5cb543bf4b0da72494c",
    "empty/efficiency.html": "d0ce2542bc023b9b037bc052a945cde1250dd75fa53e16be8cb432402cbab9f7",
    "empty/health.html": "ae316de8c8c11097b014c029a043a0ff316a35856ee88cea4c02da064d4f8a8a",
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
