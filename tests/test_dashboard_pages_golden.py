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
    "CSS": "5b58e32c0bf42e9212753c91653726160f5dcc5d1aee7dd22e243f85bf72cc67",
    "SCRIPT": "fa084cbbcd12e833610be9599cacff74bb344ca3b776278c0cb952b821408773",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "1f8980821dfe2f0d6f1790311907b438dc0870aa57e4e7dacafa4d91d631074a",
    "seeded/index.html": "7b9df0ff6df78a4a93535a28a68a7de73ceb8adea99eebc949260367b2a5154c",
    "seeded/job.html": "ff55349d9340a23b9a195d9d2291fc4ea4feb586879f01effde778cad74e7468",
    "seeded/activity.html": "6f5098842ef9557c56e9800ed527b843e4b0fae3037f5c2aca9e1768ee791c7f",
    "seeded/efficiency.html": "fda1257c05fbb8a9b9adc4c8a697dbc9a7f4a5ff88d244c6b6877aa8af3180bf",
    "seeded/health.html": "d0a67aabefd034f276742bf0d959bb43b75bee536db9bc0366f2267195b6f291",
    "hostile/index.html": "57b20c16c2b324dc754d8be32212f1b27a1b566499b2551608d17135e64a4516",
    "hostile/job.html": "9e1542ab2037e96658035bfac58608bc2bbc074a435938437dedcf047ae2eec9",
    "hostile/activity.html": "d10714ef33ec1fcd551bb643b99339f44e0b2bba41d54935e54a23ca8a6a7586",
    "hostile/efficiency.html": "eda23a3ac55aea675533b637004858812966b2a3eaa75b830143ca01c77facb0",
    "hostile/health.html": "9c86d4893076b5a6818b587ddc81c3be46efee7f667e2fa818b4e0342c2dcc2f",
    "empty/index.html": "e029e4a72cf1268723406594dd8dcc6c1a68ff50dbd56d931aeb86a74238a0f1",
    "empty/job.html": "6861275419c9d5769103301f4827ab9b258598a9016c8f212115fd9eb75d8a3d",
    "empty/activity.html": "613308ae33ba52b8c3a1c8594899e384c23e20eaddf7a0c3ed319d0d0620d626",
    "empty/efficiency.html": "0f4fd59f61f4e64525db45f34af4eaf358992f97d4b27c1e635e4cd97338950c",
    "empty/health.html": "efb73f24b9053b7ab23beec8d5826d937c21a1f688cf576f0e9aa7831b18dd29",
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
