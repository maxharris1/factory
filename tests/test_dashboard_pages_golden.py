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
    "CSS": "f2dfe2b47525d11ba000903ff49c67aa6ac8a1c889df2acb519ad2f4c353db9d",
    "SCRIPT": "e6775e2fb9a0a925c032c89289aafcf63389de9fdc032873bfd630892deb2a78",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "d65615f40fdfd199be7535b30a3e6f610de871a94ec78a5b2ed96bfbee3f7a9d",
    "seeded/index.html": "139cac96d207661384479003580ce64f41ff231d3ebc6c9b563d93d4218ff7e9",
    "seeded/job.html": "de421504d688ce408c9a3f33e98425fe45acdeeec8bc7dd03b05f3ef02db0ab5",
    "seeded/activity.html": "a0f3fba2677fd2df2079145fcd96fdad8a9715c0c9d1621a136851462ef9adb2",
    "seeded/efficiency.html": "906e1f2481aa6a597b2e7778afe39e61b4d331c6bd403e2e5079ac8172dd3ecc",
    "seeded/health.html": "b340a832191ce14ff8f756b30254df48eae05f0554ad82b1b8bd58b7f7adffee",
    "hostile/index.html": "fb6e8a5645bc1974806958bffa5e200c91dce585d9bcf13a54a1c93966a46ba7",
    "hostile/job.html": "d2bac9a9fe0d04334b0fb01f9c481b9b9538589a2940eb2ef3cb289586bb9314",
    "hostile/activity.html": "da10dae1a97cb71daa2d0efd086ec7e0473c49dfcd991280f0d4b176455bd401",
    "hostile/efficiency.html": "5993ad4e796e6ee5a98bb8fd1ebc186105e7e7acfc6da91f5cda639ca5508149",
    "hostile/health.html": "e0fdd946093c07d7f780e8fdf4cdc9067964e850e0c942c32d89187edcbf43c6",
    "empty/index.html": "c31da38472bb8ee746338bf947efb33968fc4a943cfb1f721b26cd9487c32225",
    "empty/job.html": "1c4935c7ef415fe4dd32d1ad4eafe3ef99aa4770770018789a12573fe629713b",
    "empty/activity.html": "405b7a3c09487a213aeb28d1fe6a661c335bde78a6e04fd78d4860e37854a4e9",
    "empty/efficiency.html": "4eadbc85caa7c7ee7f4fd6d9fa470f15ac6bf916eb0ac51af2d5d35758d76fd2",
    "empty/health.html": "05ce5d57cbfabfc2b02b7d6b9bd36e3805b5dcdb5728b102712a8da1b9d90d8f",
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
