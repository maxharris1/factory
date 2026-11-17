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
    "CSS": "b923e176904b6bdcb8759919f183e9f79723300cc221aa3808de8cf41952f4cd",
    "SCRIPT": "43ef667518896adccbf90738f1a3c6719324c48104d3afb2db29ada2c1c318ba",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "03336ab61ca8ab9ae3f84cdd053362a3c7658a491ab2a610ade67aff4c6b345a",
    "seeded/index.html": "e59b4317efdbbeb92889bbaeff80bfc1a65c35d77f194b6a8e5c48e772e1ceb9",
    "seeded/job.html": "823a59314040f9a85f3e2572cfa8360b3ec039fc9a0e95987237af04f320e8b8",
    "seeded/activity.html": "e5f346ae280541914e37b1dc9178774a555e7983569391f095928fd083c8c28d",
    "seeded/efficiency.html": "7fafbd8a2eb94868ce4772e8b748e12f847bdf562f402e96ec0f69f6633c59a4",
    "seeded/health.html": "f77b30415d7c7abf9d8eed08d8a9a849ea199de381ce45849cfc06ff9392ef1f",
    "hostile/index.html": "bcc56549f257213f835f231bd3c84fc0a043cbfe44255c2ebdd4256e2df4a613",
    "hostile/job.html": "b24c958df7fbfee65fbd34c27c9820e4aee406c4234fe96bd1555fcd805b8e86",
    "hostile/activity.html": "be7ffdd2f4d8d7efaad1ed1187acbf1628617f2ccc9f57f97ce479110d7e6aab",
    "hostile/efficiency.html": "37e788ac99596a471cb0bc3c60da83a33fb2c0e346eda8401b57ccd0511f678f",
    "hostile/health.html": "3780e3889befab046d6c1d8fde8bc5fb20ad6ad3ea4020dc4fbe638a8951acca",
    "empty/index.html": "fba6c7cc4af2b2e243782d06d211a815fd155c8c15ec135780830517234322de",
    "empty/job.html": "564aa722f6dec798ff3696d893b07a4d1e3877aafd8b8a12d6bb2d4b4e8f9f3d",
    "empty/activity.html": "e2ca0b40cc135da93840f0c9c7fc7b9c55875807090cd80b15618526d251099c",
    "empty/efficiency.html": "803dbdbd19086ef46445d3bff8f01b166f55577aef4b60953423e824453d7201",
    "empty/health.html": "82ff5eb50cdf0e0f8aadc7b436bd9bcfabe254dfcb3ecc13e32e371cb39034fe",
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
