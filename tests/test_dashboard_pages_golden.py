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
    "CSS": "47edda4e71143797d657a8f082b068f17a55b2e48e4eda72387d857d5dd21822",
    "SCRIPT": "54e2be4f69d310fcbb4d3c8e2243dc24956de81f6ccee5039e7d61aaa373ef3e",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "dce9719f89e0227bd0d78eb5bd709f24d1df0dac152e33bcfb26152fb400b24e",
    "seeded/index.html": "35026e51db97805ddfbe418961d9370e84a6afa9612065139aa5a3e01c2219bd",
    "seeded/job.html": "751ea11e22bf495834631881a2bda314c13bfe4ccabaeeebdd81ff465eeb29bf",
    "seeded/activity.html": "de4335e13f8f9dca6d6de2c568e83005d4b1dec35349bb2d86c5e54f445fce85",
    "seeded/efficiency.html": "f028d304589981d16f8a618992ffb16191234f92148eb4aa80995135a9e8ec42",
    "seeded/health.html": "ac913c097b31d0320b037f932b1d6a0fa335c47c119a609c90535906647630af",
    "seeded/ticket.html": "383fd8aeb3170e0c8d07884b1ca4544f27dc9b32ffed8d5a78a4b819b229926a",
    "hostile/index.html": "206c89b8006ef2d27a616d1a2ddc75428c94623583f9c82c13ee463a7925408c",
    "hostile/job.html": "f816c10776c9f9d1ec474ae0766fa1c08f552a47125e1c7de5756585e196180f",
    "hostile/activity.html": "2ba4eb909511182baa32ca135c4991ef26552d7605aec8727c7bdd2324e773a4",
    "hostile/efficiency.html": "d81a73229f5af392eacd8233473eceb8ab0f0bc3ef0bd72debda724e1ecf1bcf",
    "hostile/health.html": "c4baf94e145f7ad35bc71c9fd1dcb0bf9be4f305cbf15801b92c8771b048ec69",
    "hostile/ticket.html": "9f8991dd31ad1a43fa3737f1e077790e4c362525f9788b91d55f5f529cc74106",
    "empty/index.html": "36b657a77240be7079e6260b2d9ad610e9eacdc23d0ffded014b760fb3ce35ea",
    "empty/job.html": "9174ff616c10b57164a61f4bd59c85b179950f88a5d56f0ca18fd7c5a9158c36",
    "empty/activity.html": "c44047b9d628a7a1947c1177d342fbb0ab1766a4b57130039511375f37b0874c",
    "empty/efficiency.html": "e2554bf42e51826256fdbc99e75da8184b54b8a8fcb6388cf780ef83570a5249",
    "empty/health.html": "0b2747ba15d4e44067e9cf225f8cddaca123ea1dc951d26d3c03eff99ee06292",
    "empty/ticket.html": "bffda322875313a20cb71eaf77cdc07c21d1d5457124e9d1de2f2b7cf7a79265",
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
