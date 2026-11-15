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
    "CSS": "840d40794c033a7ed91fc54958a2004318df1516bd2456e1ae1f7540ad219575",
    "SCRIPT": "f1a1dd8edcbd074b6b66fdfa4381af9617f6552ac5de4b13e8ff95bb3ef2ec75",
    "PX_SCRIPT": "c468396e0939fde787160777747955fb58f890ece54d70552df65458ccbe4209",
    "page_script": "365bfcdc3fc4f74aee514b420267a82af2a049f08716b5c0fc046c9af97b147f",
    "seeded/index.html": "29ae8d703996d0ed00a4f0538818bff8fe6c2f7099c7a0b2e1fa28e1d1c20dc9",
    "seeded/job.html": "bf6dbdb815a721d7d15483660cd0123ed90fb6b089e82a38c1902bdd066ee012",
    "seeded/activity.html": "fd6b46725e2df8573e237a310da3ceec44b93444707a2c1391ef2c834b7acff9",
    "seeded/efficiency.html": "2b59365c90080308b788487df74ca01a8aff9a89f4295a33110ed7fd12ee2419",
    "seeded/health.html": "91d69942facc5440487b92809eba6e71df0f90a2c492f832ce90c519928bfff7",
    "hostile/index.html": "9ff9052ad046212c40744f314d5d01cce0e2f68dd53712e7be95e8a8ba1f409d",
    "hostile/job.html": "c7f7f7b18276d8682ad1dbd651525240d3b679a7a031312cb959e0e3ac66207a",
    "hostile/activity.html": "0f9d096449645e166ff65708db1fce67c4369ef31b7d88077c3d19ba570d201a",
    "hostile/efficiency.html": "672a5fd49fa0eb2b2ccf0f6ab5477969502146e522a4734d3a660f20f1151fa8",
    "hostile/health.html": "978fbd74084d9b34842b7ccd5953dcda39c273244d9a1ed6cdd8adeef2815134",
    "empty/index.html": "465c4ec02b4bc17f5cedc304a3a113510b6dd57b9dff33df69d0b982abe1392f",
    "empty/job.html": "8970ae8ad88e95e93b915f192dbc0922f22f85d463101fc59a50211751056c66",
    "empty/activity.html": "78df4dd7761b0a9b0bb736daea9dc954384ae1a1903133c73c1c6cc384abd8ff",
    "empty/efficiency.html": "846a6d5b9b6218d10484095bb634bba40fefd4e93cdcd03dcd124b3675f419e2",
    "empty/health.html": "59d0f439f4bb6360676ffcd688c62b91341b96d07e126be727bc4cedc6925abf",
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
