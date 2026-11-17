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
    "CSS": "8b07d167a99af1be57998549751afdd14caeca6920068a6f0f888fb37aa73651",
    "SCRIPT": "7e27f6ee420ade27421737eb33a677df2918b048470c667b58c6ab95d243b4cd",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "ee1002716ddb0ce147100d6db8d8dd531f6bc8d31f1d97e43e985026c41899a4",
    "seeded/index.html": "7d9b4d3adec33af6187210485126b53e3e0f0e15c837a6f815b80124dfcdd52f",
    "seeded/job.html": "e63d780fee04ac8efffeb964dd5a98e9b549942c27b3c2a5de5ab2cb003ff9e6",
    "seeded/activity.html": "37a0c1fadf01d2d124521b01dbe688a4c99e0484af32f19aff2ae4915ce66612",
    "seeded/efficiency.html": "2381123af2b37f38c5975e87c477faec756f9a8e40aba42e18f77a63bba9a993",
    "seeded/health.html": "55b330a3c999485d4a0ecf2899a56dae407f716a559dbc405735e3120021b55d",
    "hostile/index.html": "b5ae00d0a4216bd1fdc121a715c2ed1f79d26941967d3762b4649949146fa7e7",
    "hostile/job.html": "8de7099f619dc6051c5a4c086ed13c49900804ae37d260054b23091dd4b9e3ff",
    "hostile/activity.html": "bd29df5957a82e0758f45ca9d7e2ff4f35b6996fd28131434a0e3cc34dbca314",
    "hostile/efficiency.html": "113cd42eda3f1262e8358c30550ac238a68da56771e914319ba8e5ba998d890a",
    "hostile/health.html": "e9408ed6163507548a901f6c1a18b85aa8d2654171f25357f2aeea2e9a5579f2",
    "empty/index.html": "8e80e9b12fd7ebca9014e903ab19288d412907c1cea40d949a299b65a20dbff6",
    "empty/job.html": "bb80a53ead4e5273f0fcaaa73a4ff5f99b8f3462a7efe6e001cc66e52414229f",
    "empty/activity.html": "c38287671a0aef8b2ee5a1ca1bd23fee2532385e7bef2b4314d18fc951619408",
    "empty/efficiency.html": "544d9ed27372e831193442c631a00284b799d86db5811c27da4560109a00d0d0",
    "empty/health.html": "31020cfacbc63079229ef8fdb357f2a95151dc5755de615321e28b9c5b833317",
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
