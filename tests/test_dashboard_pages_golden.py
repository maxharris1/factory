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
    "CSS": "d873908b2d6d1e1d7d6d91bded11054c2a44940fbd1beffaa63e2199d27ceb40",
    "SCRIPT": "fdaf9c5c8f5fc712ca1f50d5533eecd4ed26e575cd2e6b1f79a50260b0cd8cfd",
    "PX_SCRIPT": "c468396e0939fde787160777747955fb58f890ece54d70552df65458ccbe4209",
    "page_script": "522715e8db9dc8c82742ab10893daf1009f34c64c3b9fad52b1acd9c2161af67",
    "seeded/index.html": "f29595cbbcf6f69946e320dfba697261865309724e572cd59b457c41b5be1beb",
    "seeded/job.html": "8878ac1496e59d3c031c3772280244b46dbcdb25a6cba7086bea39e9b83d7f57",
    "seeded/activity.html": "f2fa9186a2882f3c69793102c955f32753131f1a7a8666caf4d56acb94242be9",
    "seeded/efficiency.html": "de340d5cd47f7c2b696ac3c659e1e38968b82068ed1af56c04d2d0eb6b456bdb",
    "seeded/health.html": "6b9f13ae02faa7f9341f9d49eede25ad7bda82fff6a790c0044660c7ee6d244d",
    "hostile/index.html": "734da37fb910d1ed7f4d984bc2a5ccfdddb7049e3f9a66d99cb2b37e726f8612",
    "hostile/job.html": "dc69e1ac293a8e8465f7e7b8595f11de1aee10303b244188b60fcc5039b9a8a9",
    "hostile/activity.html": "d6f398f56142e872e4405cf5734ede81e79861ea18bf9c30c12060bacd55d2b1",
    "hostile/efficiency.html": "3ea6f569a3e76e5859066686c51f723cefd196115ac7d40b502cda3110dddbd9",
    "hostile/health.html": "a70faf908f5be85ef66e5e25f5e8c4d2e153fe3945c11f506257f32fcde919e4",
    "empty/index.html": "74120c3af43b576f4bdf53d6d5a03d5eb347d640c88c4d8f051447d962c99fa9",
    "empty/job.html": "ed89b9fb8c223ab701cf4eb57a78659de5912c89418aa15cea45f23cbce6d846",
    "empty/activity.html": "be376dc6ff0fc3b52a165304cfee542047e530ee3d7283a4aa0d8b6c5047db30",
    "empty/efficiency.html": "0dbfd16bc97235aa0f9d631f8543b1464c03d9575f323c1906075b82c5890b0a",
    "empty/health.html": "d1ba5b44a69c38b5c20ab61924c3eaf274257a7a6d5d6e12c53241f465a8ca0c",
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
    "_ATTENTION", "_HEALTH_LINK", "_health_link", "_attention", "_relay", "relay_threads",
    "_VERDICT", "_td", "_tickets", "_log", "_POLL_STATE", "_BUDGET_STATE",
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
