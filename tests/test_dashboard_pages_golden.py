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
    "page_script": "c1770ac2c1f5a82b23b96364d66e4c95f45efe93a4f8434036f4d7a291d0f1c4",
    "seeded/index.html": "366b524ca21ac8238197b5a664fb32240a4036d51232bf668ddffed2cb3a5ae0",
    "seeded/job.html": "5b89935667eae35fa4598b8a0375499ce9d79249bae6f24790c170faced6a8cd",
    "seeded/activity.html": "5adcfaf94c66bfa38c74548989084f079a7037e06f99b627442d29bd683cd314",
    "seeded/efficiency.html": "f088d92e1805dd84d43951d9f0c22b3cc21aceb5a25ae781708729ed4f8730c4",
    "seeded/health.html": "cce8b256723b37bed46628652be35e0fa59d2e2829c996e47bfbb081d5d0976d",
    "hostile/index.html": "999cf8d2bb0de7ef74d9890c2d016592af30f23201c2b5e35dbdb5891891f8d8",
    "hostile/job.html": "2659b4e0388dd91f3492a9a3084b4f613edf8e1417355f0a3e817513ffcb19b3",
    "hostile/activity.html": "7055d7250edd1539056217fdc1e778f346a6d487bbba660219ffbeb6df3aba42",
    "hostile/efficiency.html": "6dabed53ddef085b13dc0cb729aedf6272553f86a7e9bef61c1b54cde854fefb",
    "hostile/health.html": "50e1064ae866c294753a90eb18340fed57cb5eb39b8e6ca2ae98531f6e25f347",
    "empty/index.html": "b3841bb325654a2d1cb06bb78b78db05a949090f703957a0b99f7084832f0bab",
    "empty/job.html": "31f0cd0b9d022ba6fa6828d045ae4a6e7a82bd62680317054d132bd776ce14f3",
    "empty/activity.html": "3590eafabdd1044331aadf2755994be5fe56d9d6eb5389cee0ceeaa80fde0022",
    "empty/efficiency.html": "937b038c4c32d993a6927dd424c57a936cb5de04bca7c4153aeacbef7530d05f",
    "empty/health.html": "ba24227dc0ddc6abb8c52f3b9545b9c4d267b42f1c94155546e11aa38774cf92",
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
