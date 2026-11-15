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
    "CSS": "bc721bdb25c353748b02cad3bf9d6ac0191887e3cc8216fd2f9f9e6f0f47649e",
    "SCRIPT": "af8b58a8bc27a069a2b28abc284a89a5b710dfee0842423d585eda8b00887159",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "befce3ed41c2a96aad7ceea196e29a07676fee7f2b0a4a62bbac459db80588e9",
    "seeded/index.html": "bec923fb66e03d0e281c126d40abe43465d54558cce995cbe692c844be983829",
    "seeded/job.html": "b56d6f28f275cabd8d4acecc78975f70e4a6288f4ce2bdfe0fe831695616dec4",
    "seeded/activity.html": "dc853cfdcb1a19fda27563a8dc3b2166dddb36fe6c0fe4684fb65483f34d1634",
    "seeded/efficiency.html": "b569215a47af182b30b3be77e43eea6685381817261ab3f7378d0db75be2830d",
    "seeded/health.html": "bde9e1ce1d9aeb3d1b2ecdec065967052039267dbdefbd9b959b39db51b794da",
    "hostile/index.html": "f6b597c4a60c5e7524b749dc0e7d388c9982118d21611c305bda83a133aa3c71",
    "hostile/job.html": "27353a8cf36799e1b532208920bf5821e38382ec4726cb1eb8f4f9f84ca53e2c",
    "hostile/activity.html": "cc0c2658bda8ef39fb2938d12a8aafcc8fdc430f77eaa62991452816c4bb3e4c",
    "hostile/efficiency.html": "d5bb75eae0c3b399ec6e7acd0a78777688a2aa7c209b6e9776cd47da5d6bce25",
    "hostile/health.html": "9a9421ef20de5cd1130bfe16352af9e61713eaa287a422b74bddc0879f3bdf1c",
    "empty/index.html": "282a446270c27a3cd392b7c956edbfac53d3b6aa018d3949f62557fdf3a5a0d8",
    "empty/job.html": "fe61d104fb34c68d0515abfe20492ecd4dfb85065ee962bf0df01196f67d11d8",
    "empty/activity.html": "f3441b1e1addc8c36a7944753dc532114ba65d48f9247a472212ee39b1f0bf40",
    "empty/efficiency.html": "5e1cee07c4a3c34c6a2bb2d30d6120ef6a6cfe23ca3a00d9359bc754a01e52f9",
    "empty/health.html": "e9565ca662d6f25b0d17da1a264dcdaca12e92084b793bdaa6b58661c788c2ec",
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
