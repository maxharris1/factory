"""Linear webhook health (#56) on the lanes-first main page (#65): a small
block inside Intake, server-rendered and refreshed by the script
from status.json on every poll."""

import json
from pathlib import Path

from lib.dashboard import collect_snapshot, render_pages, write_snapshot
from tests.test_dashboard import NOW, seeded
from tests.test_dashboard_layout import BAR, _no_script, _run_page, _section, needs_node
from tests.test_linear_webhook_health import _with_webhook


def _hooked(tmp_path: Path, **kwargs) -> dict:
    db, log = seeded(tmp_path)
    _with_webhook(db, **kwargs)
    return collect_snapshot(db, log, now=NOW)


def test_status_json_carries_webhook_health_and_the_panel_sits_in_factory_status(
    tmp_path: Path,
) -> None:
    snapshot = _hooked(
        tmp_path / "db", last_received=NOW - 4000, started=NOW - 9000, dirty=3, active_linear=1
    )
    out = tmp_path / "out"
    write_snapshot(out, snapshot)
    status = json.loads((out / "status.json").read_text(encoding="utf-8"))
    assert status["poller"]["linear_webhook"]["dirty"] == 3
    assert status["poller"]["linear_webhook"]["warning"] is None
    assert status["poller"]["linear_webhook"]["note"].startswith("quiet: no Linear webhook")

    kpis = _section(_no_script((out / "index.html").read_text(encoding="utf-8")), "intake")
    assert ('<h2><span>Intake</span><span class="count" id="linear-webhook">'
            "Linear webhook · last received 1h 06m ago (Issue.update) · dirty queue 3</span></h2>") in kpis
    assert '<p class="muted">quiet: no Linear webhook for 1h' in kpis
    assert "note: quiet" not in kpis
    for name in ("activity.html", "efficiency.html", "job.html"):
        assert "Linear webhook" not in render_pages(snapshot)[name], name


@needs_node
def test_script_redraws_webhook_health_from_status_json(tmp_path: Path) -> None:
    fresh = _hooked(
        tmp_path / "fresh", last_received=NOW - 300, started=NOW - 7200, dirty=2, active_linear=1
    )
    out = _run_page(tmp_path / "fresh", fresh, "main", BAR + ["hud", "lanes", "intake"])
    kpis = out["text"]["intake"]
    assert "Intake" in kpis
    assert "last received 5m 00s ago (Issue.update) · dirty queue 2" in kpis
    assert "warning" not in kpis

    silent = _hooked(
        tmp_path / "silent", last_received=NOW - 4000, started=NOW - 9000, dirty=3, active_linear=1
    )
    out = _run_page(tmp_path / "silent", silent, "main", BAR + ["hud", "lanes", "intake"])
    assert "last received 1h 06m ago (Issue.update) · dirty queue 3" in out["text"]["intake"]
    assert "p.muted \"quiet: no Linear webhook for 1h" in out["dump"]["intake"]
    assert "note: quiet" not in out["dump"]["intake"]
    assert "warning" not in out["text"]["intake"]

    waiting = _hooked(
        tmp_path / "waiting", last_received=None, started=NOW - 600, dirty=0, active_linear=1
    )
    out = _run_page(tmp_path / "waiting", waiting, "main", BAR + ["hud", "lanes", "intake"])
    assert "last received never · dirty queue 0" in out["text"]["intake"]

    db, log = seeded(tmp_path / "plain")
    plain = collect_snapshot(db, log, now=NOW)
    out = _run_page(tmp_path / "plain", plain, "main", BAR + ["hud", "lanes", "intake"])
    assert "Linear webhook" not in out["text"]["intake"]
