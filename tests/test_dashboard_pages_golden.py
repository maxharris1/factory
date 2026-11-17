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
    "CSS": "1d679cbe03d71f0ce2a7bc91ad71fedc3b6841abc760da51ed4f2e4c2b16ce4a",
    "SCRIPT": "5a5910d4dfcc78de57a6eacc0da54cf4618a0e691f95cee0a2db1b7a0e291636",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "6e86ba22b93f188705e5ab6dba4269c939343ad2ef6f89792d9c315fdf1ef8d6",
    "seeded/index.html": "50bf9c3ff5eac4ce4661a36d75307ca3ec54f0ed6f3aa015ef6300ae5068124e",
    "seeded/job.html": "c02de9dd0a0470f240ea7ef957a752327cdc8b61566618bb057bc07a2e3c4e85",
    "seeded/activity.html": "8e5599de59b9663ed22fa6966fb7f0e81088492c9d755173af614eb730ce5667",
    "seeded/efficiency.html": "cc12464fe41a1f99543f47bef1897c7870ed17b97997ee8a67d54b96f5cd5289",
    "seeded/health.html": "b5840441663b60a2fde9b0e4e85d10045d5616927a28988640b03d87d01b3473",
    "hostile/index.html": "abc49640a7392e788384927837e160d83aa2696ce72d6036e8069044f212fa62",
    "hostile/job.html": "8ccc98f7792f85ad01273fd67f857940da09b6eed5630c5426c4e12d90570407",
    "hostile/activity.html": "968d7661f232094edf6f509c80ddec93cb97429574f48449f146359b269eb509",
    "hostile/efficiency.html": "05ff685e4c6cc71d8c5d76f56d3607e192e3a7625ee10db1962d9939c1f1b19a",
    "hostile/health.html": "750bab3fed552f4d647f058cc92ccef31e0d7074eccd27c8ceef421ee8fbb63b",
    "empty/index.html": "c626b17c8c9ab086dbdd9c37a3a3aa94c3abf48f0856541829d335d87c4a0b08",
    "empty/job.html": "c08bd0d471a5e3d6735aec0a1fc78792c1d9bb0d32f56ace908996d7fcac2317",
    "empty/activity.html": "c4d33720b1ded2cdc9f3ead3141f665cd11683e4d6d44dbfd341da318f49f972",
    "empty/efficiency.html": "ad26e3dbfa3fb4430ea2b31f2184a8c61561e77a95844e6802c7cdfb6953f465",
    "empty/health.html": "8b9a3ab3a9ffbcd740d4b1c8cfa092e270434b56b662cd53681a59350a31baf4",
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
