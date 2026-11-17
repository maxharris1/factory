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
    "CSS": "f7e6d482749443734db356c51f7f2dc618a7e2d66ff64674723c7273bb3bf48b",
    "SCRIPT": "4665d7175d0bce8cc0b01068728478dcae3f62b8340e43d78e8b11df6b62928d",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "43349a6ad3b34bc632c10dd38b48820d9593d6017259e1043dc3d4f461511b21",
    "seeded/index.html": "8e94a6d3313ae9d026afaea5b6abe1d3a6ccb44ff542fd5c3f9325cee2c6225d",
    "seeded/job.html": "d2eea753c1d7fe71200333de0407067579f3b9fed2d454888ada04a14460b761",
    "seeded/activity.html": "62affbbe70d52ab8570bf6ff511b4158a4c88f94060def304a25d319bf17a07b",
    "seeded/efficiency.html": "ecbc26c47900d3def00a77cdb42194edd369077aea9adef6a84cd43d75553739",
    "seeded/health.html": "21ec7fba5b215f3d6ed0f826bf1674145845e27083043942cbf1711a15ff3211",
    "hostile/index.html": "fc914aeb1a4b8f563f62cf122aaf9d50873958980553f226a8e2e6e5e6009ec0",
    "hostile/job.html": "293cb1f9abd21fe44aaaeffbe3e802bd169d06de463c85ff8fbeda7e73ca4958",
    "hostile/activity.html": "5922e71c3ee4a94b25eaaa457d7f7ad522a12af4186f34c733da5752ccadb8af",
    "hostile/efficiency.html": "671934c793fb62597e090e9d01bd49f95e93e4a28d7331adecab4b58ba9b9bb8",
    "hostile/health.html": "7e67036a66ed397bc3bf308458f4129977b54e61d42f1fb1b47b5247dbafc7b3",
    "empty/index.html": "d0f91f94165543a37b97f2e5e00373b06ea797b3eef6601ed0eafc7012bc6125",
    "empty/job.html": "60e95bbffdeeed59c45edbeeee8e12a17a834b38754c9c82da7754118a505efe",
    "empty/activity.html": "8be980104cd0126db440c73c4a21f10f3fa0bbb4db4b97942bba2c67c5056d1e",
    "empty/efficiency.html": "fc012b5dd656fd0ba2f1d4672c8ac405ad57077f511082fff3664440ca1916a7",
    "empty/health.html": "1bfd8884f2e88ad810505abefb6d644d8d307ce4052c943f8ca3be2ed881a925",
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
