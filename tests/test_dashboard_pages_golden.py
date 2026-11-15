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
    "CSS": "840d40794c033a7ed91fc54958a2004318df1516bd2456e1ae1f7540ad219575",
    "SCRIPT": "e6e622f1415ad65c0cfc43b2613146c60a7761d5a13d7a8deead76bb207d8ab2",
    "PX_SCRIPT": "c468396e0939fde787160777747955fb58f890ece54d70552df65458ccbe4209",
    "page_script": "d48f6a7b0fba7374779c570324371c9d1cdac8ecd29d507af7943bb2b75e7ecb",
    "seeded/index.html": "fd1fba833923b55e63deb6fea01890b53f11f0478e681079e9147a19162cf85f",
    "seeded/job.html": "ab51a866215196a1eb4fe2b0f4fd61bc401994532a3fc595a7489644025fc4b2",
    "seeded/activity.html": "22045cce3def7136ff6d559efb30edac6c5fcf05cb54ebff9a97a0724d8a72ce",
    "seeded/efficiency.html": "bdb37cc9c9e31b0264b28853ebbf9783376622c40e953d7fd8bde67bc60bbe9a",
    "seeded/health.html": "2532c22f8f920fe021752f0d5e7846065cc581e749723421f5a9b36416ee2619",
    "hostile/index.html": "a3c4563a4d112fcd4ed65449407d4d5212561d02c38a54aea966a0aaa0a5bc63",
    "hostile/job.html": "4aac2528be1f3adc38c2897a812223f5a5a3611e93a2d50633c6e77d1ed44703",
    "hostile/activity.html": "31755b000ddf630d488f3981c4bb78a410677baf7bfec5351c3996be48969349",
    "hostile/efficiency.html": "7d4ab65b6f72d3da6a2b43dbf410c789597a2c027cb28e2d28aeed6148bfc87d",
    "hostile/health.html": "0b030706f5b3735c1d223cc3c227516d2a1e25e7ba2f87d6bd73345f320a92f3",
    "empty/index.html": "9a95742de50f96130d70fbc3428c1b840e3d47eba91f9d05c612d91dc1d45536",
    "empty/job.html": "207ca3f54f5f0b57b5068ef46812532875a33f2dd31bdbe742f7b6da1b55c038",
    "empty/activity.html": "631b2d958341300dfae3abf374eef101657fe39b791f9029c26d80586e6593ca",
    "empty/efficiency.html": "7a7be3ef1a080ae47462b209864f71968775e6c77fc414a2fc368bbd30ab81ff",
    "empty/health.html": "785c5db48537e2b6291c6db43b6a7dea0ae5d9ceff72e3421dbe793ada6d8d96",
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
    "_linear_webhook", "_JOB_STATE", "_state_chip", "_slot", "lane_devices", "_lanes", "_queue",
    "_ATTENTION", "_HEALTH_LINK", "_health_link", "_attention", "_relay", "relay_threads",
    "_VERDICT", "_td", "_tickets", "_log", "_POLL_STATE", "_BUDGET_STATE",
    "_health", "_HEALTH_STATE", "_ANCHOR", "_anchor_attr", "_components", "_no_waiter",
    "_savings", "_active_jobs", "_board", "_legend", "_TRAIL_LEGEND", "_meta", "_job_article",
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
