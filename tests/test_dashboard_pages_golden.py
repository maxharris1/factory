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
    "CSS": "6c48f90302a098c2b9f201945aaba55d751e834d7604e21bba1c6634fd2e6e9f",
    "SCRIPT": "7807e2e72fb5b64278f8cd6a4b68c94e24022ac0918335be89217c8f7e8a7fa8",
    "PX_SCRIPT": "eb97c011fb3b14fe54d32f8e5b156faad6aa1b619c1df8defb1da936a7f80357",
    "page_script": "086ca5d7b7a9051b11bc7af5ab4dc63320b625afe83d632da7a5fe978e67c0b4",
    "seeded/index.html": "1faae42c37856f96146e81ea7eed8519c19b1c8e28ed23011f82f7d27bbe2229",
    "seeded/job.html": "9d0edf842ee1ea3f1a2f49d7a37bd486f430d548906da37530400bf88b523e57",
    "seeded/activity.html": "2332f35af1d38ee8c0121f7696bba97c615d45e742b15411299ac0f814174113",
    "seeded/efficiency.html": "a53b38a1e9f06bdd7c9b1cc0c658fda4c3b8976c2a2cfdc04c7365597dbb496c",
    "seeded/health.html": "50cddce8ca6bec0283658f70aebd7948143d4228c7fa8af262abddba84512993",
    "hostile/index.html": "fb37e703c809633edaa278f1e6fb327906c5591841c708dcb3abeeeb582305d3",
    "hostile/job.html": "60683fcb59ca61e4cf4e82511f43af81015a415c23f7a7b9cff83e54cbc3b848",
    "hostile/activity.html": "6a4de437fa4e6d97cf7b573d16f5a8c1bf7b364c94111d9cb2ed2b14488637f6",
    "hostile/efficiency.html": "58ecc501d4f3807453150232a131cddd68fa2fadbc80e4f33a4f8c83a92c1913",
    "hostile/health.html": "30f8466a36e65dbf0d87a2accc004c79f777d476067ad9f2d65081b3623bd072",
    "empty/index.html": "ec2ef64290c4bcf94f2a3b33c012ff45e0fba7747e2202eb062df9384ba12a89",
    "empty/job.html": "5dd035809f6fe340489b9583483a243c363d8d27c08853b5ae3fab20058bf1db",
    "empty/activity.html": "4b2fffab7dab4f9fe004eb2c57696aea3b147f6ff0b5cd1f45be48ff69b4ad04",
    "empty/efficiency.html": "69502a42e363c496858be0b50245f85b6e6f723df7f23a474bde6e4fb194dfc9",
    "empty/health.html": "6798565cf6134825161e0b091b266a2905d397b8f3959a6a40b40ae3610db782",
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
