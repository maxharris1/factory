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
    "CSS": "e0a11b5c1fc3e332c1bcf46e441db33291aa1e52b18032ff49ce6e581d08127f",
    "SCRIPT": "7e844593e91ec6bc226ab7208efbbf196fb7a1ae96f6b7f451e6d2996ac053fb",
    "PX_SCRIPT": "c468396e0939fde787160777747955fb58f890ece54d70552df65458ccbe4209",
    "page_script": "7b280140aa5c2e486ca248fd54e08e41a801a9d33452defde504107503da781f",
    "seeded/index.html": "e27033655b1788160653153bb3f5bb78db0ff174904a178076a05217f09d4f4f",
    "seeded/job.html": "ee5e52084baa8e5ae90d9c6a0afc2ff76efa379ac82ae5de696a0c1b4b3b6f9b",
    "seeded/activity.html": "b5ee091470cb3aee693c7517f24e72789bc85b01c3e1ddba45004e647f4c360d",
    "seeded/efficiency.html": "b5d9814a8f04724b7db3a2368b06fb265ea1c927ac3d4f914fd533691fd7cd5a",
    "seeded/health.html": "ba178b3d569580f69c12f4ff524715a03365a70502955101ff2c6228024202ca",
    "hostile/index.html": "f90901a1ad795f98c0b65442995ba1e48f6dd97e236a8ffa484b1b32acc9d195",
    "hostile/job.html": "23268d47d7f6f9932f8a86f37fcb08ea325bfdbed3898818c3dfdf5e5afab03f",
    "hostile/activity.html": "4eaf1b42e3db88e0dca5c5fb3776f6d8d4e017b824f2bd8c90f2415d49c8df2a",
    "hostile/efficiency.html": "21e3395e182ea12dfa7affabc3c33bd866c66f40858e4a53b7051baebb18d729",
    "hostile/health.html": "aacb7749e3c1462b1551d417d6f9393c0521c175c4b73e3d5e243a3449a8bbac",
    "empty/index.html": "7178b53879645aa36b7dc8c2325de40b976cd22c26bf9ac66baf67bd67309d3b",
    "empty/job.html": "78a2674652e6b03b78f87b1b41716576f3893a95f8521ff4007fc6a97aaece3b",
    "empty/activity.html": "3b68ac73eec873ecc0e8d1b2a3be9b383b3676ecb51700afc08364c35458cf22",
    "empty/efficiency.html": "26546d4e6d90a93f57d25babacb2dc6c96c1cb93b1901a3c11fb3a68617fd4f4",
    "empty/health.html": "916efa02632fb4ff6895c3d1fd1736e9ebe2b44b35216c1332d95dc34bcaa8c4",
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
    "_ATTENTION", "_HEALTH_LINK", "_health_link", "_attention", "_HANDOFF", "_handoffs",
    "_VERDICT", "_verdicts", "_td", "_tickets", "_log", "_POLL_STATE", "_BUDGET_STATE",
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
