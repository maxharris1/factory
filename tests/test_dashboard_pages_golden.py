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
    "CSS": "ad6ca7dc83d799e622a12c2fed06ef9175dc2a83db03a2e8c59247de64d247e4",
    "SCRIPT": "53e4ea5206181ce6311897ce148a30adafc62303b8f19b8235dd2466d55da23b",
    "PX_SCRIPT": "c468396e0939fde787160777747955fb58f890ece54d70552df65458ccbe4209",
    "page_script": "cbb1877045c28298e3430c61e1d5d992edb8f61010eb028739ef96f9b27af936",
    "seeded/index.html": "1b050920c39c54c03c36869933fdd6589d58cfcccc4d3ef86d37061e4ea59667",
    "seeded/job.html": "055836230457dc263304de104803db520a9b4008fb5f407d438058c1ff26421a",
    "seeded/activity.html": "b7bc5b153a87a8021e2a73cfc921b62a5e9cbb8ebe1f4191a73c18d7c4827d13",
    "seeded/efficiency.html": "9b2b4b03e8868bcd438635a97fd8e4575cbb6ff0f5011fdfc305b3b70240a7ef",
    "seeded/health.html": "bcf370ce3fce0c34820761dcc850efdb4f9d0f72b4d4523ac6479fe17f664bd6",
    "hostile/index.html": "c14ca5d9f2025584db78014ec4d0665bbcf9f018ef30e12c0f532200de0e0787",
    "hostile/job.html": "ecd503a9fa669dfbe56288cdade002b22607262e10e5cb568dd20bcb49e68ce8",
    "hostile/activity.html": "703bd7e81a187fadc66350382e0d8ffb55de7712aa3c1cd99b061642ff9303d7",
    "hostile/efficiency.html": "5b1e48c6e1050b7b8d1ea6bf23736314338ef78bf05c86a2da564b30c76215e9",
    "hostile/health.html": "9f86a348db6bd37e0872e77301eb893fde1eb4a9847c44b3896ab7fd2ae29a3f",
    "empty/index.html": "e8fd65d2754a48e1601de173f8d0705a7f03146b1fed0e30ac78368ccb474923",
    "empty/job.html": "db2de64e69ec7601c51ea33fa2debba1eb91a209635703d48533793fc4d1c61a",
    "empty/activity.html": "49604d8142558effc4cc5efc63c5202e0bc06ca0beea39bb79ad146e5a745404",
    "empty/efficiency.html": "22d03f4e228e7aa546219579a0c6d2973e7d45e780e57b65271c20faf7a3ee3c",
    "empty/health.html": "687d427ff7868781e44d214e743f7bc792e41342abc5a42d35cfa18b6f1b7ee4",
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
