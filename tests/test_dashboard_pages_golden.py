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
    "CSS": "0838fd43367b57b0d2579bd033d398c466a4f626bb7016e4f7d3e387251299fa",
    "SCRIPT": "d0cd418cb35f3904174d532fb6c85733e8307694c61948c3c2b8c469df497ba6",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "48555b6b5ebc059062c4a58e0d2c2bf12b2374dd0b7ee7e6d3d2c67d4eae4ece",
    "seeded/index.html": "a6dbbb3dd071d47685dde045d795c37f33c9acb1142feb7b34b1c223affde27f",
    "seeded/job.html": "3d211816697ff2b5154ca5aa96a467d1655c61dd6e1616be2e809d8425664f48",
    "seeded/activity.html": "ae434eb5385720576708b7deba42640c1d2e9478ce54d007e246eb166edebe7b",
    "seeded/efficiency.html": "169247859ca9ef31b09ec3d74358d5e3bf1ca32f674e4bd97dc7b41af4f7bc70",
    "seeded/health.html": "7084f9088b0768d938a4125e0d7c54a4564ff1d21c502b3633e9d2657e557695",
    "seeded/ticket.html": "d6b27741d5a9dd60ac4096c6abff1fdc823381a2a0fe301afd78a33322f02c67",
    "hostile/index.html": "de9bcee2ba827efba19533cac6a1d65bcabbe3d46474ef1e07cc759afca9c2fe",
    "hostile/job.html": "0253c4a7150c0e4c46c2ccd1c5d107522ab787805f0faf6a06c699ae6d5cd255",
    "hostile/activity.html": "d8e7b8ac227687077b0ffe5923c85181d520d0637c5b5113111caba402b39be4",
    "hostile/efficiency.html": "e2883d41cd09b2d7e75fe52178b44852ba4d14aee0e5c0b2a49cdbc4cd41ebaf",
    "hostile/health.html": "038009a760bcb856d5cdcb4afa7457e8f73e37a6eb5875ad77b8e695152b1025",
    "hostile/ticket.html": "112292734a035d72606cd4dea1be1a028d04bcad083e8b9a59f46beec4550327",
    "empty/index.html": "b9a13c41df5c908d56f2e3325351da4b407c3779d62cb7383609b77af03fe5a5",
    "empty/job.html": "a334405aa97c61d318b7d2640d6c6ace83202e5d4f889299f3e4cbc7e5cb3239",
    "empty/activity.html": "895d42384fd59244e164ea17964b8f13063f1746710ae4198c100ad2fc194924",
    "empty/efficiency.html": "e5a5ee0cc0e2c6bbcbf4f0fe8a9f57598e66ba1d5e7e125fcdc1c7d8d7c49518",
    "empty/health.html": "96946c4f4ef2a10142698aa9e81b3004d18c092ffcc0d910fe923ccf2142347e",
    "empty/ticket.html": "48d37bf626825dd887ace841cf19e5fe398d11f20ce60036acc870744d8b1cff",
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
