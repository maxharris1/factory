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
    "CSS": "bfd2ec94b0ce4971c527629a16f4f5ac1e03cd4897e1437a252b90da6d8e3118",
    "SCRIPT": "32db04f1dfec941dae27cb5ed4536ab5d2f267a4b748911875187ef0e8d68a20",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "46f01cec979cadc3aa064efb207e5c60c2fb2ef6424f7952f6e24c623736eb18",
    "seeded/index.html": "85841fedc1673f06a40732e594aa482ed898900344aff1cd20fec1195e094227",
    "seeded/job.html": "c127ba3e6f57688319f4dc355409b2fb303a186a3025f9b23f04e3301b00799c",
    "seeded/activity.html": "cacd7457acb61a772bf0f55f0db16c9b9ee3d8556e4db63ce960c980fee89432",
    "seeded/efficiency.html": "f57666a930c0bae9df08d51ebb7783dba4817fb8ab61e6319ddb7ebbe84e8aab",
    "seeded/health.html": "c70b1bb7bb93a43b6c5b011fd3d2436fc236ef555fce5ef9aab9a67a84c21e26",
    "hostile/index.html": "8f75a64458fedd406eb077f820eaeab651ac492aede25ee1702bd3c0e20432ee",
    "hostile/job.html": "dc13bbee394bcff685d5660984943fd1a09d7c9c75b2badaba1a531ec154ed4d",
    "hostile/activity.html": "46e738f7a4c1d25518c37d51e179773c7baefd85cf23ab5ee6b52710cdd3c8be",
    "hostile/efficiency.html": "84a1355333abb1a2f887e6627f8f1d57443c589cd0b07e0b7b20a622ad187c5c",
    "hostile/health.html": "bf647bbae2580c3aaaf6eb637f6a45a0383be6f69783f05afd01a823628938e9",
    "empty/index.html": "819abe0d5060ab6eff81e8f6d6c586c2bdc488d860d71ffaf4dde058d2bd9567",
    "empty/job.html": "499a9a1db0875aa21fd9ea383abf51e2702d0e2182341b66b82b665a846ee5f9",
    "empty/activity.html": "4491e50d40f82366cf6aa0a59e2a77227d5ca716331946d627a976e3ab3916cf",
    "empty/efficiency.html": "d78b830556affea031d2fd7de07b5fc6a5ec302824791d7b5591fab500336f5d",
    "empty/health.html": "62766a17a959dba644a1bdb6a366b3edaf4fa7ef6f9c02f5b2868a6428d63426",
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
