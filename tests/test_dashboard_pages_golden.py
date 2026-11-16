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
    "CSS": "c00e2c749a47bc6e7279d179ee241c5914c0bf021a56e6a3fa586cef3f170bd4",
    "SCRIPT": "a371d088819b1977abac1d6f3c4f21ca92e5ae69753d91dc4010924af0368956",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "f4f5a6181d0bfb7578a3d236163973290f66bbff312097fc333ad9983b7f1f96",
    "seeded/index.html": "b37c745c418f9b5147a3eb4c52dd0bf9fc7fc3362bdb45120a0b3add146e9eef",
    "seeded/job.html": "8bc0eb4f99896ea819d9bcd36c590fe7b631518c8709a4990ec7382214175606",
    "seeded/activity.html": "ff45cc6a59e21811e67136457ffb2c24652e88c5930dc5bae7beae939f851ae4",
    "seeded/efficiency.html": "9a8a75eda7b0ef196a06d8a1dcd2bac36c6d3d87cbd13de21bd9ca216be016bc",
    "seeded/health.html": "2ea48d9ace935434b3da0e3f94de490bd3fb92e061049150d49e0d44f8b6c214",
    "hostile/index.html": "d897b6678f161d62019e444a1708f1fc0fbbc2d784861e6a9e05964f0c2e4239",
    "hostile/job.html": "73713139be080fe2a04bd8b7c244356548d9a504f4af504b4991780f47146cc2",
    "hostile/activity.html": "5a5a97db8836458b812e114a6902d40b78576b73608a31e58e3009c6109f8cd8",
    "hostile/efficiency.html": "62a3b5c8c7598b3cb773a39106fcd3e85a29ef5deed2b8a53acb3252b1432fc0",
    "hostile/health.html": "682301b5d9fe9276a9afd0aaa01682f747db260fad2919261ee9c72734e6e74f",
    "empty/index.html": "be6b2499f16c7b69f355b2b7458d9babf9aae0288e4bcfb1350d9d4aadbb8064",
    "empty/job.html": "0f725746f638d3dd0dafc5fd71d6b347b039ff82af3bcd4dad6325c753f09517",
    "empty/activity.html": "af0d9993d236cf42adb8f18b256b2911f2c6f693cc571f6f70768d1648f9a98d",
    "empty/efficiency.html": "adeafa501805fc1f24d13f5016e030390a1d786502a08b42a7fa6f2a17a3faa9",
    "empty/health.html": "96e793f45b90f8fab35eda8c81f505acdb843a26b61e8659416095bc86709fa7",
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
