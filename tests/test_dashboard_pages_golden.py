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
    "CSS": "8429f8c8059df7218cb5f76c54cae29e0857454329540bf88f55d7fe1d9a373c",
    "SCRIPT": "945751d7e053649a766b63697f9af3b94735d9afe4004ef3b220b4243b0d18bc",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "df842667f2b244756d8a1efaf11955b5cf0432c1ebb57fea5e9d88b1c98d373a",
    "seeded/index.html": "67c099bae58f8208c4ee1041d130957f800c2af0a914740d36cdc637ef351baf",
    "seeded/job.html": "bd47730ff5f68925345a09bbde68625ddbc49716576226453f96a8a2f88f7f21",
    "seeded/activity.html": "0707de487d5c5a61e1be11659fa9d137707fdb3a2ede99111d09ed0d2e8a0b84",
    "seeded/efficiency.html": "a863cdde7700c638dcee437b9a1edda060b324bca733af00b2a22cd354dd1818",
    "seeded/health.html": "3a8999117a73f222602245b7578d37cacb2ef9a228764b90b1bef94674d41b55",
    "hostile/index.html": "aaef0e6a7dee72dc7859311512d61c3b179ffdfc379aafc91f3f011f5bfcc170",
    "hostile/job.html": "7c9bed8eac6588250f8adfd4a7f4fa2dd6e95f5b1da0f11c9836bb22ea8aa504",
    "hostile/activity.html": "b0d2b115b27ec7c1ba186d4028832a914cc149c924953b15677ea642b5505ff8",
    "hostile/efficiency.html": "04e4d5f3bb28ed38af7ce570d9c8b57f53403a70adb2380dbd320f5581cee923",
    "hostile/health.html": "931dfc622a4b35a83692621ecccedb3eef7ffe5ef0cf179304f2ed82db9969bb",
    "empty/index.html": "1de0f8219a9bca0e958e304ea41ea1e8610e8fd1a56098a1c67ffd77c5c5dfd6",
    "empty/job.html": "6f0246fa3e6b612dbeebbd487e00624c8b577afef5a32cd706f198fd9f639b3f",
    "empty/activity.html": "377cc820bcc1c452734832de89cdb703898bef19ab8a43029220d121187cad7f",
    "empty/efficiency.html": "8a4b2ce630f522950f672f86d0faa1fb951bd47212d0a0089a2fda7f740ca21d",
    "empty/health.html": "27fb586701b452dd681a5248de5551d16ee61948459eb78a6fa728c43b9dcc1c",
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
