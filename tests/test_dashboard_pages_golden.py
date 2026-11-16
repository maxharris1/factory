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
    "CSS": "4026dce031a307589e8d5f9732e3abd93b8f85099d0eaeecccc5edc26b3ddc58",
    "SCRIPT": "d03b4cccf7a6d90915962160279721eceaecf717555426f6c4aaca0830793352",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "1441d15ac1bf95b80bbd5224dacfa27433345111df7b8e5734277fb721aefb92",
    "seeded/index.html": "aed2e426e455452c46e3b14594afdd6820c3de5bda76bcf054ad976deae483d4",
    "seeded/job.html": "c6369524d432a0e7a07d08d229368e11cdf73d3a546b46e2e5c59288fa37edd0",
    "seeded/activity.html": "9b71feaf34545267a1dfd8e682ab2c53c1e535d14cc9baf1fc648f8c93248a28",
    "seeded/efficiency.html": "a14cb8e6c7119bacc2e0ad4928922464c8d40acf91c6e9dee426a0bf468e2697",
    "seeded/health.html": "145ba9e723ca0d72e1bdf9e21f33e697597de43dbcb71a77c44140a26be800b3",
    "hostile/index.html": "a2dab46c8835477707b2a3f06ba7c81eb89edf272ce17e393e40672fb64c36ed",
    "hostile/job.html": "14d71690f0ffd92d2319b3cf27f0cde19e8aa6929349e5b5f14c090b11ff5942",
    "hostile/activity.html": "5264146f50901ed09b2837567ef03d4387c13a9a3efa37b9580cd35370f7e4f8",
    "hostile/efficiency.html": "c297845b44af9849bbad7e408764b053d9338246686c7eda9247bb86e8a84b00",
    "hostile/health.html": "d8a2ac0bc912704d4762627ae08fb4f86bace34abdbc87bcdae3fd418a4a6df2",
    "empty/index.html": "541f9fab2bed6b67391adf8f047e7bb6b5c9492801b21c427edf9ba26afd9d12",
    "empty/job.html": "011fd78bdb48079d318ab092feb2a5c2358200281dfbd14f8fc6d2539aab818f",
    "empty/activity.html": "b3241a851f513746a85a777bb7f98c1b32a9197e1cde49bb5b1181b313a0a136",
    "empty/efficiency.html": "a379fe614ff8966f7bfa3460b4c674c6203b3f6f85759a4f14bd6e20e55b167b",
    "empty/health.html": "80dd9d3685b7bbba81bf49c0d1451a4e344a61d5e35982405925bce1bb7d9692",
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
