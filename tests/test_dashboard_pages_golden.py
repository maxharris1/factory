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
    "CSS": "66255dd57f49efa58b36048c0ce2ce7c7f4c37c81402cf8ef02fe06680f9ac91",
    "SCRIPT": "6b72c26fceaa09f17c56eaccd3cfaaa2c8b15979402efe76331af2715314fafe",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "fa967c8b5eebfa809bf3055f6645f5dadf220c3ffbfa215c8634533633001e10",
    "seeded/index.html": "51e1627a9ebcb64c781004d3abc82f62d6da92afb3435585c0e963e140f9da6b",
    "seeded/job.html": "583e1938b86f12fb2767f6d942dc79714a4da19c527388fac941ed753a9eb73c",
    "seeded/activity.html": "1f40f677e394df2647d06f0c7a4bc8609b00a1930d28ffbb92f57e2f4338d02d",
    "seeded/efficiency.html": "dcadef7120480e05a4aa4720a950471b5737767e0278eecac426ab5a6b24b7d1",
    "seeded/health.html": "edd4f96d0761da638f3c5a94bb458d616dc996c540079467ceea48c8642b3b23",
    "hostile/index.html": "a3c528007744330b6d20b173517e1e0f5202ef3461a5e2e7d853261d756d50e8",
    "hostile/job.html": "6f5e8145a28b938849269bed3e9331fe611300cfe6d1d4faaab206de222f0023",
    "hostile/activity.html": "d144f716c59922a3338c29b69607c00996879c4c435ad3b931b1a02f65eea176",
    "hostile/efficiency.html": "286866cc674acbdf8c9e178c2b83ff3b8b9d3e3360318cc7feb4a7f3912e2a86",
    "hostile/health.html": "8308fd203a2e416df050236c950c265a88fc074e925cec4c5a0e013701990c0c",
    "empty/index.html": "6ab18610efe57476866f32eacc1269863f05e354236b4e3b59415a6f51a52613",
    "empty/job.html": "c8df056476aa39ca6e962f723b9ee9ceb30af42176c4999da5f72f051d06d755",
    "empty/activity.html": "a844071489c727abcb8241ba50b6d677346cf1abd837aca9b5c1967fc554f93e",
    "empty/efficiency.html": "e1573906871e619a2888b2ad96eff74c98a4a146d15d7be33f3fbd3d60719c45",
    "empty/health.html": "a9d46699a8d87f3ef11a0ec1662e82bf28d9cf452917c06f3f1882d3f7e527fd",
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
