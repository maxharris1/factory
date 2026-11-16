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
    "CSS": "d4df591e86666a87dd1943df050559e109c2613b37d9bdf5e6094dbe3fbcd346",
    "SCRIPT": "4b68cdf57b09b3fd5630fbdc2faa0d842f08f41556ac1ecc2eec3ee99aaf71cf",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "751060055dda257d541de642deecbd367191e9e6a1da9a55524117587c9d7ca0",
    "seeded/index.html": "f022e1c66664a0009217ff320b7e5fa93d7fbfeadf69caed2108540b295d7aa8",
    "seeded/job.html": "922351fec8decb2046781d846d6ca5e88a417e3dec282181d36feabdb2dd50cd",
    "seeded/activity.html": "e471a1854620fd9d426a6d81b43c2cd12c21203d390cf6e1a7a07583335edda0",
    "seeded/efficiency.html": "3a7b97f2332fb38070d5ae4612d9efbef76350788456bf71f2d7a770071833fb",
    "seeded/health.html": "9e8d338018537b4a37a678cdf677397d3d0295dfa20bde288f88839d58e7dec8",
    "hostile/index.html": "48f16416cdccd9b855c1a010e0b6904b1f70ecae324d44beb76de0857750c48d",
    "hostile/job.html": "bf90dd97820bffafd79b40fbe79c84d2ef2b541fa6a752e3149e5f31a475c7fb",
    "hostile/activity.html": "cc4b8d00d009c666806fc0287754c33bae186c4c9a891b14b7ba3722a14893a1",
    "hostile/efficiency.html": "293bd08a5741122d6e08d976ee56f39e28f55b05684f6267d4b3dd7ebf913d08",
    "hostile/health.html": "fb7df2ae092b99e289b5a2b52453e18764e97d5302354123965c00b5150f57ec",
    "empty/index.html": "fb8a373c4fb27a7d71999d11258c06774f76a91e8ffc3f5c25205c9a64251e99",
    "empty/job.html": "9b5fabfae436383ee9409f3ce7198d449cc5bdf1ced0f4f973d5d66770ea63d0",
    "empty/activity.html": "fd2699bef212f8aa211acd5832f05d28bd1be5a5acd3a55b8194c8b652551f8d",
    "empty/efficiency.html": "d7a3b7eb1cc2eba461b52ae70100a5d10bf882e93500ae257f5e3a13faeef315",
    "empty/health.html": "a02f716212698080b8a5c21608db530c54d4ad6913bd2396437b9ec6f2a9d883",
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
