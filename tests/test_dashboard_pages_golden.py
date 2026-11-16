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
    "CSS": "95158f56d78a43f4657d689d623b84bed185e53d50be194ee9dc704c8bd2abdc",
    "SCRIPT": "24523f9bafeeb97ee0c97b30a7af37431aec46345ff257a6ed3db3fb1b11e8f2",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "edea98cd7bb3caf6ce86d8f324751f8a2fca8e03ca5dbd738d8cf789121f7227",
    "seeded/index.html": "31c6ebbc171be01b87bf1b962113c67923154b5c4b47739934e4b8f66ae52854",
    "seeded/job.html": "f35510793c9a3f573381027725391a8fc42bd05104e57e860da6f11bca8ba693",
    "seeded/activity.html": "1c25478cee59227fdc4dbcfbda970c01bbe84fedf6f20adee1d7678b9e1ad786",
    "seeded/efficiency.html": "e5a09a521541513c841e0b25f0fab2d4c9d426efe51d6677f33d3670035b7c99",
    "seeded/health.html": "563f7ef4ce8161f0a912bb445b34ccc2becd90ca956c7a4de589da9392cd1eab",
    "hostile/index.html": "3265b1b4bb7ac2d545c55e216ec2bcfd647820e1592b429a24fa1984d2a8df68",
    "hostile/job.html": "11aa6d88dadcab4424c1a3a3c1516e549908d01bbfc4077f5a6c7b5acf50c48b",
    "hostile/activity.html": "4dbe94bd4280dab121ee0e8d9c7b9bdc751eff19c45b47f038366ef5c8b0e2a3",
    "hostile/efficiency.html": "bc500993266bb1fd9455c3277b49dc44c3f278041b3d504a025f30734da34b84",
    "hostile/health.html": "82ac89b56cac9ad07c1cc6d1e99ec5e1cb498135459d8336326abea2c02175d1",
    "empty/index.html": "3dcf61a7102c3ee5687d493a3a1f9458843b7e88015b22cff4092bd09ba0af42",
    "empty/job.html": "f397921c74287146b95df77ee523890b0ecb928598ee0b96e237f8a245281917",
    "empty/activity.html": "ca7855e5fde8ed96fb21d7f49c235e26c4cce311ded64b8df73b8997e1b679bf",
    "empty/efficiency.html": "d0e7c4dc98c29130de9dc0719b00e36ca539d30a213473958b066e3a6177aada",
    "empty/health.html": "3e35fa777d5995e9ba527196f6046a71c7168edf324e36ab28976c5c58f3a8cc",
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
