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
    "CSS": "6f170d1fb5dea266a6a59bcde09226efbee1b1ee3f9409f94552663fb439d6e2",
    "SCRIPT": "eb4b18d2a3b70a88c71b7e064937cdd64b3b2e538eb69be2dd430a5aec4b5f06",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "6d9471ea0d81f513972893919af2454e73cd0780db6f080e068a1d2e6f73e5fd",
    "seeded/index.html": "8bead3b0aeac79ed1ee5a88c3f8c7f20d3995deca75a4596c980a19a7156ba4e",
    "seeded/job.html": "cdb9338cf548ea27d2f5b9122d25d81e86ee0deabb3ba1057f24f60aafeabbbf",
    "seeded/activity.html": "3a7d3b1c207c27542e50f0c4b98946ea7d9ad4c4ea3ffabea83c719cb7feeceb",
    "seeded/efficiency.html": "338d3f28c6916b55ee06e9f24e0f8832391b596089b9e832693cf00639b1849f",
    "seeded/health.html": "cad5e4d1f501b929da3b71792fbf3cc569061902a33d15b0be7409363d8ae0aa",
    "hostile/index.html": "2f9e8739f695f27554c7b0668cc6d72f61527a7e73f13f833b6cb647309dda18",
    "hostile/job.html": "19a07e25a11e155788caa515575b33dfb573addd3d87406952915dbeee76927b",
    "hostile/activity.html": "a30d77e13719835e3f8e1f2a20bfc42152a69a889e177120999064aa1cba42f7",
    "hostile/efficiency.html": "00d279a42aebcdb1f5dbfdb7cd1f7ee4891d143848c54bb54cb9118c97c3c40c",
    "hostile/health.html": "df6060db4e87bbfc339df2a4ed40ba04704c16dfb126cf992990fceb2ca0925c",
    "empty/index.html": "de033501aba30e9b2cd604e04c76df265b8504490f9a18baae97d6cef60c5282",
    "empty/job.html": "955d3b838eba8eb47b8d9a11d378e799e91dc19e09f6d88ce0a23d40c4abcb42",
    "empty/activity.html": "05ae2cdb72dccea4f0fede2c088cc4620720b6c6c691b70f4a1d63b2c1150a5d",
    "empty/efficiency.html": "60ea169952230519b544c3973081e162d634783d3f63caa6dc3f90bd731b47b9",
    "empty/health.html": "f1f26c5159dc86c4dba1446db5837e75822273637a1330ba34a9d4449498f3e6",
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
