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
    "CSS": "5875e8d15ee66f92872b73f4851519dd48d9ec15f03876caca183c440c6d8384",
    "SCRIPT": "f0c1bedadff023bce31335bc1212b915e88cfd13c75c4948d3364849845fd3e8",
    "PX_SCRIPT": "c468396e0939fde787160777747955fb58f890ece54d70552df65458ccbe4209",
    "page_script": "3355ed0f5109eaf0a3f1ecf4575726ba326d5107109f15b51871931ecddab5f0",
    "seeded/index.html": "ef0c540baac9f75d5ede8d8282b35e0b33e21094ed92a6f2a03a069ffe27ba94",
    "seeded/job.html": "63daa9b4d39028f4f46f718c44ad91bbf821f1bef5bf3e5ae596a7931541e3d8",
    "seeded/activity.html": "c6652b2c952e82c06c23457684e3c0d2c97a636e69df6c4fab14f369e662df35",
    "seeded/efficiency.html": "f8465892c279a88bad3f95bd96803b61a6d4870d6b4d92cabaedb1c52e0bee74",
    "seeded/health.html": "262eba0699a5791c2bb1b66c46eb5697961f7e472cba9b19f74631aee5734fd4",
    "hostile/index.html": "90fd1d4aeb5c6bfc73faf6690a4e07b91c56ec17b7351441ceb6d4bbec6ca52a",
    "hostile/job.html": "b654440fd326e76cb645dc3dbc5778f4aec869e8874207830446b9e8ad515554",
    "hostile/activity.html": "a2f5dbcc5c5d291f598de76a8fc08f03dd2d8d9ba75b287f60c4e8f15820df5e",
    "hostile/efficiency.html": "1b0f23660473b336614ae46b40c8b8d33b9e2330cfecf2ed95da29fc4c57bfc3",
    "hostile/health.html": "d9ead40f1e22840a1ba2bb6495c72f066924c8c673434e7160848f90490d5e2e",
    "empty/index.html": "855bb14995b2dafd5e4faa76ba0b38a28be8c96f3e430c9a881140ef05b45a07",
    "empty/job.html": "b2334ff2226549bb07c47e83aead8ed26a7a69967bf7f36eb0b16a9a6e1f3812",
    "empty/activity.html": "80dab12ae11f79c5c9b805c943eb6f88fc22d60e2f144cf7d0b0c50872b7f602",
    "empty/efficiency.html": "b841473ef03fe3552282efb7dddcf19b94e8000a2a5ffdabe1f2168ae5c17e81",
    "empty/health.html": "53153a434e766661f9a3fa1a8cb40b04fe7c865c22c4557b376cf7999aa35c03",
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
    "_ATTENTION", "_HEALTH_LINK", "_health_link", "_attention", "_HANDOFF", "_handoffs",
    "_VERDICT", "_verdicts", "_td", "_tickets", "_log", "_POLL_STATE", "_BUDGET_STATE",
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
