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
    "CSS": "11730ceabde6daf9bcfd3e1136633e84127c6c3a59b285618cd052050f00f709",
    "SCRIPT": "b6272d901e8fc9bb5b4f3c1588da543749bb79358be9c2f2b22bc4a5d298c4dd",
    "PX_SCRIPT": "9638f3995b27e67c16e5830b74f8b506fbd48c79258e7d04d40d95e9bddf1d98",
    "page_script": "f5bc9a8fbbc253043f2d12c90ae49a3c7547371cdbdcee3b8e14eee97a676082",
    "seeded/index.html": "4baab3707815631ca7e7c7e19da1f6d7075b2e3db267df3c5d48faf1b3d00e0c",
    "seeded/job.html": "6f4ac97a7c2b6e41cfe36cf8533d5d2b2f8ab7f5c80e452f7ea177b4afaa6d06",
    "seeded/activity.html": "5caeb9b68eff7766c5298099b0347dd3d830e706602e34d4d1327912747614a3",
    "seeded/efficiency.html": "1382879c02c78958445380e1dd2a69d7bdf371c9d46b74bf00cae7d2bb9c9646",
    "seeded/health.html": "71061ffb10ac934c5b2e81c6c7c2e2a3f171dd038de7d3faff5247a74cd1cbbf",
    "hostile/index.html": "a3bfd271eee5aea4bce251b56e7b25c363ab7decb97e3bc783779d6e83b98721",
    "hostile/job.html": "39d5735ec317afcb25dd9214eb14c215948acb992fe00b7e360603369f0acc6f",
    "hostile/activity.html": "e862ed501c8512146151932383e821f00b4c9d908c583ccee0108d37ee8ec99b",
    "hostile/efficiency.html": "8941c9218324d45048f8092f9ca66b97f784742317ec013b1905080eeaa3f618",
    "hostile/health.html": "e86d1fd34cd05f2821493039d9fbc2747bdfe531bc69336ee636861e3fd60685",
    "empty/index.html": "a0d4a18e6a6e06da36beddb80d20aec6dbb3ca43808bac52979884372a365973",
    "empty/job.html": "ffb39ba3fafe39f95c609e0b69324c8a658706e902fb5a3bea3c682b93ab9284",
    "empty/activity.html": "8f4a95d4b64b81ce07a4de30cd1389a7dfcb791a436268e486a1918c01159081",
    "empty/efficiency.html": "cd8570f37708331d24bdca26696811ec4a44f79f4cbb7ef94c98d72a07f5a971",
    "empty/health.html": "60819189364a2475c78910ca75d56cacfd2a2f2a974f08b02ae69898619a6211",
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
    "_savings", "_active_jobs", "_board", "_detail_model", "_job_article",
    "_jobview", "_PANELS", "_page", "render_pages", "TELEMETRY_CSS", "TELEMETRY_SCRIPT",
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
