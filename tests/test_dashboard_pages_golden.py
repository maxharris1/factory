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
    "CSS": "ee1c957ec7a6fbb6c30f5c4105c2482340ab8536b5225c1366ff7c6b0ba3bd19",
    "SCRIPT": "7807e2e72fb5b64278f8cd6a4b68c94e24022ac0918335be89217c8f7e8a7fa8",
    "PX_SCRIPT": "69296cb1a71ee6a3debefca6b16c4de3581f72a3779b05ba50f47fb6466954dc",
    "page_script": "e78db3f025847337a6251259eb5834ce65353b2dcfdf793fbfd653e9ce573451",
    "seeded/index.html": "61ffa758931cc04299970018efc2f65e7adabef47bbe020ce14500e990299582",
    "seeded/job.html": "7b151cba6019834dff5a42375383169c1327fb58bcc531f37d66770b9236bfde",
    "seeded/activity.html": "f68393296faa1e30bcbf365df2ea4e23ad301401606f891a159a17bbeb445bad",
    "seeded/efficiency.html": "7c68f6e557464915dfe9ed21fb5089f8cec3497ca59bcaaaff44f3720d4be9eb",
    "seeded/health.html": "285d1204e8b559d330b1425da529c54ace48f8535bc6700dc6c5e08ec82e99f9",
    "hostile/index.html": "aedc1c9bf15343c33838c711359b4b52dabbaec4b760375c102001cf8b95a1d8",
    "hostile/job.html": "3d032e675d65296f19306706a7b99d494d3939b0a46b2577694a2c49eec7e5df",
    "hostile/activity.html": "7c83c5fb27555fb0d1b79e11c96258bdcee27f3202855e68dec3e532adc6b347",
    "hostile/efficiency.html": "0bbfec67941705ca82ecd0536b2a66f7394cffec8bda987175396a01defbd0e1",
    "hostile/health.html": "6bcab072be23d919af13b40036b8a0dcbc16f0a279f13a5624847120a0a28324",
    "empty/index.html": "a053d5330a8f1d58a7bd35c169f8176fabd11f9876c73668a0bd3a1a6c2e2a8a",
    "empty/job.html": "1f5ebf6c5e8cbd9747753ad833e521e3750e6b336940f30f3c88ff7dccd163a5",
    "empty/activity.html": "6138df596d241bb5f576d8dee38f94a2e067c14d4a248dc0b38ed25420cb2f05",
    "empty/efficiency.html": "193e517de88707fbb836319b237c1ab02087f24be21c3e93c346bb9a87532ba6",
    "empty/health.html": "8cec18c01198a73be5e1dd1792112f60c065b67f12165f7984878c06f90fbc14",
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
    "_savings", "_active_jobs", "_jobs", "_legend", "_TRAIL_LEGEND", "_meta", "_job_article",
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
