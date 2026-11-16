"""#545 ac-5: both eval-report modes run on inputs this install writes."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from lib import cli
from lib.state_backup import CycleBackup
from tests.test_dispatcher import GO, FakeSource, comment, setup
from tests.test_dispatcher_eval_log import CLARIFY, START, bundle_from, cleffy

ET = ZoneInfo("America/New_York")


def test_both_report_modes_run_on_the_backup_snapshot_and_log_the_poller_wrote(
    tmp_path: Path, capsys
) -> None:
    now = [START]
    state = tmp_path / "state"
    dispatcher, queue = setup(state, now)
    go = comment("101", GO.format(state="building"), author="grok-login")
    first = cleffy(go)
    dispatcher.poll_once(FakeSource(first, github=(first,)))
    now[0] += 600
    dispatcher.poll_once(FakeSource(cleffy(go, comment("102", CLARIFY, author="grok-login"))))

    dest = tmp_path / "share" / "backup" / "orch.db"
    errors: list[tuple] = []
    for at in (datetime(2026, 11, 9, 0, 5, tzinfo=ET), datetime(2026, 11, 16, 0, 5, tzinfo=ET)):
        CycleBackup(queue.path, dest, lambda *error: errors.append(error), clock=lambda at=at: at)()
    assert errors == []
    baseline = dest.parent / "weekly" / "orch-2026-11-09.db"
    capsys.readouterr()

    assert cli.main(["eval-report", "--db", str(dest), "--baseline", str(baseline)]) == 0
    assert capsys.readouterr().out.strip()

    bundle = bundle_from(state, tmp_path / "bundle")
    assert cli.main(["eval-report", "--input", str(bundle)]) == 0
    assert "runs: 1" in capsys.readouterr().out
