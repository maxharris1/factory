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
    "CSS": "329136086afeda021831f03f8cb6b76318729f4470a70dea466226dc8e72d68a",
    "SCRIPT": "8333a5a0428772069950b15272298474ef9bae72855b310110538aed76bb0c51",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "83078486404774231b7a1df052900c0b715aa28f416de521fdd06fd10802ba12",
    "seeded/index.html": "879f5c97d70092e72770158657573290f2668fae1bee185532abd7e4ff6ac121",
    "seeded/job.html": "809b3bc9d16f1c98c834c6a8075079978f362c27897e76adfdef4d54f534f25e",
    "seeded/activity.html": "a08e47f539b951a50eff15b5661f4d917551b73e7249fa89a905701b93f460e7",
    "seeded/efficiency.html": "f218c3e8fb68e2d5a50a41a457b4f3268b4fdda77af862431a74aebc2a2be4c6",
    "seeded/health.html": "368b65271584761e93554364bc737e1b7e2518bdc5a7f442e7b1df461cdbb29f",
    "hostile/index.html": "6065d1ccd09bf81b93b12f775b0f2c59acc01633491f4e0d25d41e2554bf627a",
    "hostile/job.html": "600e4334bb6877c3b673e05ffe7f566acbd9d505a4f05c5cb519e853860c347c",
    "hostile/activity.html": "b01b4f2c2326eb3d3f5526dac4d3b2e09311998479aecdbc070048dc6072a32a",
    "hostile/efficiency.html": "ad53ced8947fe28aced8d8a42a5687523d49f076b1f45fad1caeb009197cb3e8",
    "hostile/health.html": "59222f8c3c348078a8eb74ce82e088bca503bb65bf56a24922bfd900a67620db",
    "empty/index.html": "9c83e6912e38ff899fdd23b1f5301ad023a83f203a005b32adcb62325b771d4e",
    "empty/job.html": "9589895b21c6762dd307ce30bdb27483285ef8ddd3f357dbaaafdeaae5d8cd0e",
    "empty/activity.html": "f566037c98eccdacaf02d631a519af5167d6163cf077227c2096038f924116b8",
    "empty/efficiency.html": "de3f724848d36f9bd787c95ad96181a39463bfecf0f370b41b3a2c1f9847dbc7",
    "empty/health.html": "e45f9358e4b5caa725b6e1bd923b544335927055861444619a16ae4b50d292d2",
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
