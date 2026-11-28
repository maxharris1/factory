"""Webhook health in `orch status` and the dashboard (issue #55, design part 3)."""

import io
import sqlite3
from pathlib import Path

import pytest

from lib import status
from lib.dashboard import collect_snapshot, render_html
from lib.linear_webhook import LinearDirtyQueue, ensure_tables, missed_delivery, read_health
from tests.test_dashboard import NOW, seeded

ROOT = Path(__file__).resolve().parents[1]
QUIET = "quiet: no Linear webhook for 1h (nothing missed by the last reconcile)"


def _with_webhook(
    db: Path,
    *,
    last_received: float | None,
    started: float,
    dirty: int,
    active_linear: int,
) -> None:
    with sqlite3.connect(db) as connection:
        ensure_tables(connection)
        connection.execute(
            "INSERT INTO linear_webhook_health VALUES (1, ?, ?, ?)",
            (
                started,
                last_received,
                "Issue.update" if last_received is not None else None,
            ),
        )
        for index in range(dirty):
            connection.execute(
                "INSERT INTO linear_dirty VALUES (?, ?, 'Issue.update', ?)",
                (f"id-{index}", f"AUT-{index}", NOW - index),
            )
        for index in range(active_linear):
            connection.execute(
                "INSERT INTO tracked_tickets VALUES ('aut0-code/aut0', ?, 'linear', 0)",
                (f"AUT-{100 + index}",),
            )
            connection.execute(
                """
                INSERT INTO ticket_state (repo, ticket_id, gate_count, stuck,
                                          workflow_state, generation)
                VALUES ('aut0-code/aut0', ?, 0, 0, 'building', 1)
                """,
                (f"AUT-{100 + index}",),
            )
        connection.execute(
            "INSERT INTO tracked_tickets VALUES ('aut0-code/aut0', 'AUT-999', 'linear', 0)"
        )
        connection.execute(
            """
            INSERT INTO ticket_state (repo, ticket_id, gate_count, stuck,
                                      workflow_state, generation)
            VALUES ('aut0-code/aut0', 'AUT-999', 0, 0, 'done', 1)
            """
        )


def _status(db: Path, now: float) -> str:
    out = io.StringIO()
    code = status.main(
        ["--db", str(db), "--machines", str(ROOT / "machines.yml")],
        stdout=out,
        clock=lambda: now,
    )
    assert code == 0
    return out.getvalue()


def test_status_shows_last_webhook_and_dirty_queue_depth(tmp_path: Path) -> None:
    db, _log = seeded(tmp_path)
    _with_webhook(db, last_received=NOW - 300, started=NOW - 7200, dirty=2, active_linear=1)

    text = _status(db, NOW)

    assert "Linear webhook\n  last received 5m ago (Issue.update), dirty queue 2\n" in text
    assert "warning" not in text


def test_status_notes_an_hour_of_quiet_while_linear_tickets_are_active(
    tmp_path: Path,
) -> None:
    """#79: a quiet hour with nothing missed is a soft note, not a warning."""
    db, _log = seeded(tmp_path)
    _with_webhook(db, last_received=NOW - 3601, started=NOW - 7200, dirty=0, active_linear=2)

    text = _status(db, NOW)

    assert "  last received 1h ago (Issue.update), dirty queue 0\n" in text
    assert f"  note: {QUIET}\n" in text
    assert "warning" not in text


def test_status_notes_when_a_listener_never_received_anything(tmp_path: Path) -> None:
    db, _log = seeded(tmp_path)
    _with_webhook(db, last_received=None, started=NOW - 4000, dirty=0, active_linear=1)

    text = _status(db, NOW)

    assert "  last received never (listener started 1h ago), dirty queue 0\n" in text
    assert f"  note: {QUIET}\n" in text


def test_no_warning_without_active_linear_tickets(tmp_path: Path) -> None:
    db, _log = seeded(tmp_path)
    _with_webhook(db, last_received=NOW - 86400, started=NOW - 90000, dirty=0, active_linear=0)
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE ticket_state SET workflow_state = 'done', stuck = 0 "
            "WHERE ticket_id = 'AUT-7'"
        )

    assert "warning" not in _status(db, NOW)


def test_status_without_a_webhook_listener_is_unchanged(tmp_path: Path) -> None:
    db, _log = seeded(tmp_path)

    text = _status(db, NOW)

    assert "Linear webhook" not in text
    assert "warning" not in text


def test_dashboard_shows_webhook_health_only_when_a_listener_ran(
    tmp_path: Path,
) -> None:
    db, log = seeded(tmp_path / "plain")
    plain = collect_snapshot(db, log, now=NOW)
    assert "linear_webhook" not in plain["poller"]
    assert "Linear webhook" not in render_html(plain)

    db, log = seeded(tmp_path / "hooked")
    _with_webhook(db, last_received=NOW - 4000, started=NOW - 9000, dirty=3, active_linear=1)
    snapshot = collect_snapshot(db, log, now=NOW)

    assert snapshot["poller"]["linear_webhook"] == {
        "last_received_age_seconds": 4000,
        "last_event": "Issue.update",
        "listener_age_seconds": 9000,
        "dirty": 3,
        "warning": None,
        "note": QUIET,
    }
    page = render_html(snapshot)
    assert '<span class="count" id="linear-webhook">Linear webhook · last received' in page
    assert f'<p class="muted">{QUIET}</p>' in page
    assert page.count(QUIET) == 1


def _poll_once(db: Path, *, at: float, webhook: bool) -> None:
    """One poll cycle at `at`, in webhook mode or not; no listener runs."""
    from lib.linear_webhook import LinearDirtyQueue
    from tests.test_live_poll import config
    from tests.test_live_poll_no_webhook import Clock, Linear, source_for
    from tests.test_live_poll_webhook import webhook_config

    clock = Clock()
    clock.now = at
    source, _gh = source_for(
        webhook_config() if webhook else config(linear=True),
        Linear([], hits=[]),
        clock=clock,
    )
    if webhook:
        source.linear_dirty = LinearDirtyQueue(db)
    source.begin_cycle()
    source.end_cycle()


NEVER_STARTED = "  last received never (listener has not started), dirty queue 0\n"


# F1.6: webhook polling is configured but `orch linear-webhook serve` never
# ran on this database. seeded() tracks one active Linear ticket (AUT-7).
def test_status_and_dashboard_note_when_webhook_polling_has_no_listener(
    tmp_path: Path,
) -> None:
    db, log = seeded(tmp_path)
    _poll_once(db, at=NOW - 3601, webhook=True)

    text = _status(db, NOW)
    snapshot = collect_snapshot(db, log, now=NOW)
    page = render_html(snapshot)

    assert "Linear webhook\n" + NEVER_STARTED in text
    assert f"  note: {QUIET}\n" in text
    assert snapshot["poller"]["linear_webhook"] == {
        "last_received_age_seconds": None,
        "last_event": None,
        "listener_age_seconds": None,
        "dirty": 0,
        "warning": None,
        "note": QUIET,
    }
    assert "last received never (listener has not started)" in page
    assert f'<p class="muted">{QUIET}</p>' in page
    assert f"note: {QUIET}" not in page


def test_webhook_polling_without_a_listener_is_shown_but_not_warned_within_the_hour(
    tmp_path: Path,
) -> None:
    db, log = seeded(tmp_path)
    _poll_once(db, at=NOW - 3600, webhook=True)
    _poll_once(db, at=NOW - 60, webhook=True)  # the first webhook cycle anchors

    text = _status(db, NOW)
    snapshot = collect_snapshot(db, log, now=NOW)

    assert "Linear webhook\n" + NEVER_STARTED in text
    assert "warning" not in text
    assert snapshot["poller"]["linear_webhook"]["warning"] is None


def test_poller_with_no_webhook_configured_leaves_status_and_dashboard_unchanged(
    tmp_path: Path,
) -> None:
    db, log = seeded(tmp_path)
    before = _status(db, NOW)
    _poll_once(db, at=NOW - 7200, webhook=False)

    assert _status(db, NOW) == before
    assert "Linear webhook" not in before
    assert "linear_webhook" not in collect_snapshot(db, log, now=NOW)["poller"]


# --- #79: warn only on a real missed delivery ----------------------------------------------------


@pytest.mark.parametrize(
    "comment_at, seen_at, started_at, missed",
    [
        (NOW - 600, None, NOW - 3600, True),
        (NOW - 600, NOW - 900, NOW - 3600, True),
        (NOW - 600, NOW - 660, NOW - 3600, False),
        (NOW - 600, NOW - 590, NOW - 3600, False),
        (NOW - 60, None, NOW - 3600, False),
        (NOW - 600, None, NOW - 630, False),
        (NOW - 600, None, None, False),
        (None, None, NOW - 3600, False),
    ],
    ids=["never-seen", "seen-before", "seen-within-slack", "seen-after", "too-recent",
         "before-listener-start", "no-listener", "no-comment"],
)
def test_missed_delivery(comment_at, seen_at, started_at, missed) -> None:
    assert missed_delivery(comment_at, seen_at, started_at, NOW) is missed


def test_a_missed_delivery_is_a_warning_and_replaces_the_quiet_note(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    _with_webhook(db, last_received=NOW - 4000, started=NOW - 9000, dirty=0, active_linear=1)
    LinearDirtyQueue(db).record_missed("id-AUT-5", "AUT-5", NOW - 900, NOW - 720)

    text = _status(db, NOW)
    snapshot = collect_snapshot(db, log, now=NOW)

    warning = "Linear webhook missed 1 delivery in 24h (newest AUT-5, found 12m ago)"
    assert f"  warning: {warning}\n" in text
    assert "note:" not in text
    assert snapshot["poller"]["linear_webhook"]["warning"] == warning
    assert snapshot["poller"]["linear_webhook"]["note"] is None
    assert f'<p class="bad">warning: {warning}</p>' in render_html(snapshot)


def test_a_miss_older_than_a_day_no_longer_warns(tmp_path: Path) -> None:
    db, _log = seeded(tmp_path)
    _with_webhook(db, last_received=NOW - 4000, started=NOW - 9000, dirty=0, active_linear=1)
    LinearDirtyQueue(db).record_missed("id-AUT-5", "AUT-5", NOW - 90_000, NOW - 86_401)

    text = _status(db, NOW)

    assert "warning" not in text
    assert f"  note: {QUIET}\n" in text


def test_the_same_miss_found_again_keeps_its_first_found_time(tmp_path: Path) -> None:
    db, _log = seeded(tmp_path)
    _with_webhook(db, last_received=NOW - 60, started=NOW - 9000, dirty=0, active_linear=0)
    queue = LinearDirtyQueue(db)
    queue.record_missed("id-AUT-5", "AUT-5", NOW - 900, NOW - 720)
    queue.record_missed("id-AUT-5", "AUT-5", NOW - 900, NOW - 60)
    queue.record_missed("id-AUT-6", None, NOW - 900, NOW - 30)
    with sqlite3.connect(db) as connection:
        health = read_health(connection)
    assert health is not None
    assert health.missed == ((None, NOW - 30), ("AUT-5", NOW - 720))
    assert health.warning(NOW) == (
        "Linear webhook missed 2 deliveries in 24h (newest an issue, found 30s ago)")
