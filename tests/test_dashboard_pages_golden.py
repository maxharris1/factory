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
    "CSS": "ca51da642d2f6522d95e78e816a65631d1b2dddfa6543bad179553bc697767f2",
    "SCRIPT": "69f541de34ba10011164a5557c758ccd1e24618a7e16db0c2cb84c7425f90d59",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "bffc45c0075439f6ef0d9ba55193b692edc4355f774c292e4f590f26a1a593c3",
    "seeded/index.html": "c1d8291a965d787afc40e796212c03f3231c9ed07ffe6310e40c96084850f941",
    "seeded/job.html": "895cba281377065f4377f343faea860b62d099d955bf47cd91c3385130be815a",
    "seeded/activity.html": "26d06f5815a50f0328b43f653973f57b8b8db90ce7b7ce06e690f8f78a61c0de",
    "seeded/efficiency.html": "29172ef6805df1195940419e03a7b307a8dd2a710ba20ebef5d44c82dac29cca",
    "seeded/health.html": "8e23e8e2bff7a6d4e5df1f1f29a7fe585844dd401d0c8e59dacaff7bfb865c20",
    "hostile/index.html": "ec3dbe6f9d2d14e9fedf0edca143e20c65df775d18c50bac54dd73d93f89a9bd",
    "hostile/job.html": "22e6a2ced267ada1f70cd406da988e47e1ffdbb40b3afa70f543965ae9bc32f4",
    "hostile/activity.html": "47273dd2a80ac763e331bd21074a7b2f17411c5f398b0d7ba560c77d7d8004a2",
    "hostile/efficiency.html": "20c78a57316f36fd0544884712e400a71c36f440c70ca63b62a0ef7f42d345ca",
    "hostile/health.html": "dc29c1a0757960a770f032878d5ab299d827d31d2a5ab63e4ee292dc1cc0216d",
    "empty/index.html": "2afd21ce6bc58ae4bebad9fb83e1de6f93dac648cceca449b71ca9bcda2d1fd7",
    "empty/job.html": "6d5bc8571688b997bda84175ab645cfd9a493842907aa244c23ad82eebd2a1ae",
    "empty/activity.html": "578ab44526bd2d72f26c47b7d26d2e4b0c83325f1be2e9e788b5fb0f0ffa18b5",
    "empty/efficiency.html": "3614a490659209d050a1570c78fd26fcab80c084f85d8c16cd588927d6588c7a",
    "empty/health.html": "3e9aff60ae2a4bef38cdfbd3b82459c28db6f812e9b33e64b55d4d5b84559869",
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
