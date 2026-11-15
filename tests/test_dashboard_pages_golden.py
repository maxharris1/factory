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
    "CSS": "8c69ef518c597ec50b8e48dae3f0314c753fe89be221cd6a20f7e7063c47291e",
    "SCRIPT": "cb9739e506be51fa2952e891609a39ed1efdab70b50dc985f3d2dd713eafbfa9",
    "PX_SCRIPT": "eb97c011fb3b14fe54d32f8e5b156faad6aa1b619c1df8defb1da936a7f80357",
    "page_script": "101e0d42d0d88b28581395b9627d55a1bdd286cb90cd318693c263603a210f62",
    "seeded/index.html": "991a2824b05ecdd534876e9e4647fc3dc6817eb7b66a62ca61cb36401ff3561c",
    "seeded/job.html": "7c4e195581c427c97ae6ab30fe0c657861b48215b65b8f2f4086250a016e9606",
    "seeded/activity.html": "98ef74aa80ab6f17eaed58fec7d00e68f57bb5d8b6ce9ea2e70a148545265faa",
    "seeded/efficiency.html": "5db2b84c40823b85fe47cc380908169c644d7ec863818c552917834dd10bb212",
    "seeded/health.html": "2e2c0f61d96dab3a5a4d7eeb7d0143dddac03b17da3367d85c9b4959b68be61e",
    "hostile/index.html": "af73c3c2b82bf3fd7fb8e4e9cde29caa4b56dda0825d71e6429b0f83746a16c5",
    "hostile/job.html": "7e1bad0bc00fafc5817a8b32d71af850235f8ae673a82854dba8b3c98e36c948",
    "hostile/activity.html": "37a3e2a21c0fee9afc8d225dd06c074c9d669797e1eb7441d145149086eec823",
    "hostile/efficiency.html": "0586cc2d3b3c571985b0e4d972d5f8bcf52de2a1940faaf8d8bb13ba61afee50",
    "hostile/health.html": "38b788e4cad1222125a994bf1ecd1f919e293b062d616b0ddb9e799dbf24ff4a",
    "empty/index.html": "0a768e02da21a4b5abc510663a30ef9e8fd6d0f03cf7d306a916c7f66dbd8d78",
    "empty/job.html": "7bb02a8a69d425b03bf638603cc8f43e0501320d52a2c8263562695ba0fe3bbc",
    "empty/activity.html": "1d6bb56dc01eea2450479074b470b6e17af27672a032eaa677e16f6b502ee27d",
    "empty/efficiency.html": "2ae28d1e21d44cdda042387123f7068b47375a2bd14d6727bef1faa84c397152",
    "empty/health.html": "8145935a5515cefe7d02fcbc10810862c22b7d8d12b2298058d287bf998b9633",
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
