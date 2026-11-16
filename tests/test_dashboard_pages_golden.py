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
    "page_script": "655aac6b62b60af6acaf82b06a56aaf75b55f160b6ecc79e47c212192c2f06fc",
    "seeded/index.html": "3991ffef37c8023f4dd69d68705de438f811ae3336d3d56107ea1c5b847436cf",
    "seeded/job.html": "c62014254ab1b00d22992f6b2a3f8c38db0c798b08c7bd0f96ab999bda62b3d8",
    "seeded/activity.html": "dfbceac1d44065eb770120eca147e6ffc7bd4887710fdcf2dde6bc7d63ac3764",
    "seeded/efficiency.html": "7e2776f242bb7335238e5ddd9894cbfe8718d48f072e681a786ad396492375fa",
    "seeded/health.html": "59ccd9164a39644817b3d5c25fdbdcb3cda397d446124916cc1ba2ee9e553a65",
    "hostile/index.html": "d762fd77f6ee75a7ce7addef7365cae257fedd3f01041bd4de7c9be515e8c110",
    "hostile/job.html": "bd74123c2977c404b0a3918f9e5162631d4d3874040ed526a5ee13b87a20549b",
    "hostile/activity.html": "00af0a89423a33f1b0bc19675ad9a87bfb8b66c35e70b8ff69126bf75b4ed92a",
    "hostile/efficiency.html": "6d03b7a8a8e12bb34017829bd76c6e2f2542fe079f43885e8f7f89e102a4fd01",
    "hostile/health.html": "daa59b0a961b3719e4d7ae73a7212658b2c036b441adb4583d79b537fb73f911",
    "empty/index.html": "6676a97fc2e96f62c8dfcaf2314866787507efef5014e393ef3b64f7ec4dfe15",
    "empty/job.html": "27a4b18e1a20e3f31de603efea0f39e2baf1c407e50337d91c39ac0bbe584f0a",
    "empty/activity.html": "edf95effd589e276bbd45b7f31fc45b4fed406899a558799a178c937fec94076",
    "empty/efficiency.html": "155fe54f461049622269ffece5f5f6347fe1c781b3966fd8e6f40adf94f04455",
    "empty/health.html": "3bfa3c2d1dbaf27e7a9c24196136d734234012eeb03d80dcb3cd88030125396e",
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
