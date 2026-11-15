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
    "CSS": "258ee6480d1c0d478b7e925f737a6f9fbabe9b46286dff2a76cc1de2ba496dc7",
    "SCRIPT": "7832f7c913cc3e6b5a870e0df8952aee2fd1208d1f6086fc84a342f0da9a9ee1",
    "PX_SCRIPT": "d3c012a9dcc3b97338a731e172ea14cd5d25dfc59c7fab2fdd867bb974b9dc1b",
    "page_script": "4072e75c9b035515be54635bde02515c7353546fbd7acf0c15b7f752a9daea18",
    "seeded/index.html": "a1542789dfa916c160ecd64929d6fe979ee5588a0416ad08603fbfc0e129af76",
    "seeded/job.html": "9fa17bc0f884ea33f21c6491ed8073f3f01af90cfdaa9335b0949907b8966f9c",
    "seeded/activity.html": "65cae708de4bd9cf170db5a80c350e1fc1015c9c2061db55d981a53f46e6ea66",
    "seeded/efficiency.html": "d8143c0c77a11139f331d69db8264ec5b5da93b9e5f54664d81273bdc01e4f5f",
    "seeded/health.html": "3b3b17390128fb64026da6206f7ee4a72e3206b0e569f9a5b55f47e70cadb946",
    "hostile/index.html": "198d2130f40c81463cb8b416b9d1b1b02b1cd25a2ac155ea1eab67260fe1ccab",
    "hostile/job.html": "7a68b69b3b65387b64260ebedc3ad1ade075cc68abb27d5157591454e4c37d17",
    "hostile/activity.html": "e91dee45d277acb9974a3d8b468549009c92128ba557f5c4e7e4c94dc633867d",
    "hostile/efficiency.html": "76ef96abff0d9e1b50b4f5633a0f9be4ae91298beab02271e1aaf6a0ad6cc761",
    "hostile/health.html": "ce5903a280a02962ae0a036277af5269dc5636dd7e3204eb8c598c8fcba376a7",
    "empty/index.html": "edf0eb555ff3df47b46d6202d205321809a5187acb162462dd2ded54ae5a42e8",
    "empty/job.html": "dcca0e43cec3e8a8a6941c51f2c3a134b04bfce9cc313cfe0c81a8aecb7b47b6",
    "empty/activity.html": "a80e7bb93f8d061e80a169ffb47a2b2481604a36c8ed5a47729e84f115ee679b",
    "empty/efficiency.html": "be6e070bcd4a9999ef2dd47af250890e59f55dcb687f9201b52218e1dca0ecdb",
    "empty/health.html": "396804c16ed585a69dd33e5b121c06b10a77b7c4a541d8f8a40758653e9ee149",
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
    "_savings", "_active_jobs", "_jobs", "_legend", "_TRAIL_LEGEND", "_meta", "_job_article",
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
