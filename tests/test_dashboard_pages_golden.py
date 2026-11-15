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
    "CSS": "27a57c223f9e25adf4f0f09152ba90061c6b4a17bcc1460ffb275b1869689882",
    "SCRIPT": "daca2d3584314ace431b36b1fc8e9362f1a426d1156d947d724601f7947ca8d1",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "52ace35d60564f4d12ca7515a4b17b747719f51405498fece8201526972e8eb9",
    "seeded/index.html": "1f82574526eb9a128d9a8fb3ca54086ffbfadbaa1640644182ca25aa93e4c3a6",
    "seeded/job.html": "ec7a2f00c1c65756a3175b4fc41c9765526ac37ceb6e31e64cf8de26d97e7d3e",
    "seeded/activity.html": "c286f784a7729fb35e0d66a89061a63660cc934bde59aa6622fda7a054d17c60",
    "seeded/efficiency.html": "04405f826107267c53875a9633c952117d81cfbae702e649b2980e00b978b50f",
    "seeded/health.html": "0a6652b29aaf9be087bfded8cc7e8df3812b424f49403d292362ea560437cd1a",
    "hostile/index.html": "b8c0c72ea1de98db00e4163c6e5cd55cd0a8685931ea2e08d75f0456aaa62c88",
    "hostile/job.html": "43a61e5e09ed59896f77cbc414f205d63ecc9d29a7b2debe0d79760f0f4c579d",
    "hostile/activity.html": "82c8df66c6cbefb0065463c62850fe14346f1dee16e04713d19620a180c28df7",
    "hostile/efficiency.html": "ebdd1802a1aa98e59b4afad32591303c4b87c970632d7b40b09a1c68f6c33167",
    "hostile/health.html": "f3976c3241882fa7b7b07064abeaa6534abe6b1998f797a8d5ecb1ee4733060f",
    "empty/index.html": "0baeb6f32978024f5f50ef43d1ae73c95509fe553fa8973ca9e89e86fcfa4d96",
    "empty/job.html": "ede76adaa794d1f61d9c63419655ad5aa32742ea066662569d7d6068c11918f2",
    "empty/activity.html": "8220ecaf854b2a53dd00314add74488d37181f62a6524a8cddb09c912bfcdf67",
    "empty/efficiency.html": "00a8923909d5233a4c7fbd9f7eb2d861dfbc779f540413c378ea98e6e88c687a",
    "empty/health.html": "8d72ee53295f61196c907a666458805e621a06c3c1bf430f3cd6dbbcdf87e8d5",
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
