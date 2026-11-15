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
    "CSS": "3ddca7e9708dbcb2a19836de98ebd7b53ddf9c3de5c2a0f1919097cea489d932",
    "SCRIPT": "c2c209b6e916cb50a6b4b3aad601f226b0b12b7a3768f8d4b8ae697864f9223d",
    "PX_SCRIPT": "d3c012a9dcc3b97338a731e172ea14cd5d25dfc59c7fab2fdd867bb974b9dc1b",
    "page_script": "4519a60171a364eb3c5737919a2918368f8feaa854f4b40c85df12e88316c998",
    "seeded/index.html": "d853a43ea419ce84a83ea4a68677fef6a36cc91178425ba2064a50b3a4b5a218",
    "seeded/job.html": "56de4fe5d8b6c68614f6711049cf534de5fc3447dd288aab4fabd79c54e70175",
    "seeded/activity.html": "8d93dfab3fe770e269c6a897821ab1cf3dadca5df3762acfdaba9e35fd9b3c3f",
    "seeded/efficiency.html": "253ac8ebf9f5cb9ddcc2cb7c6d35d854bbd417101e50c472da564a891c1c11cd",
    "seeded/health.html": "471e40530db647be179cb9c3c2059588e26025ec3bdce8caca66fc6905fdf982",
    "hostile/index.html": "da9f80103be794c3a83aecdc18ea94dee9de630dd58fad09079f92f8907e3a96",
    "hostile/job.html": "e8b270493dd4850fb5a59eb25e17feb1b5ed874923ae40e273e46f9b55bba067",
    "hostile/activity.html": "2c63b5a7a27943374196914038749216afe956873e366b8d65b91038e4f6f06b",
    "hostile/efficiency.html": "099f6d278a5a28a3ab58d16753240b61bfe40318c86079f8da3b73e952c1761a",
    "hostile/health.html": "244b1f14f943d7d65f0ffe49c1574eb26b2f2edafb424e265505223cd938e6cd",
    "empty/index.html": "8b9dcf450dd8e13ba5d4046653de975b25b51794575a54dfe7db9baea6937c8a",
    "empty/job.html": "40d7f77ebf2142f9c83b4020293785a05c45824dcbbffe37e001725a350a958b",
    "empty/activity.html": "a69a67f83f6e7eb2e8d25d10bdc063e4d935e339427d94ca73ab59d69d5fe92c",
    "empty/efficiency.html": "f164e2d39fbdd70cbc429f0caa5f4fb0d44a1651308b87ef8078a11d53dbd07e",
    "empty/health.html": "75dff6ac2f21548b5fcf6dc48f2f7cdb5c7a74f600174d980b81aeb4be795f69",
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
