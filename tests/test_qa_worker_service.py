"""#784: the QA worker's user unit, in the waiter unit's style."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy" / "orch-qa-worker.service"


def test_unit_restarts_the_qa_worker_and_reads_the_poller_environment() -> None:
    unit = UNIT.read_text(encoding="utf-8")
    assert "ExecStart=%h/.local/bin/orch qa-worker\n" in unit
    assert "Restart=always" in unit
    assert "RestartSec=5" in unit
    assert "RestartPreventExitStatus=79" in unit
    assert "Environment=PYTHONUNBUFFERED=1" in unit
    assert "EnvironmentFile=-%h/.config/factory/orch-poll.env" in unit
    assert "webhook.env" not in unit


def test_unit_holds_no_credentials_and_nothing_installs_it() -> None:
    text = UNIT.read_text(encoding="utf-8").lower()
    for word in ("token=", "password", "secret", "api_key", "auth.json"):
        assert word not in text, word
    assert "nothing in this repository installs, enables or starts this unit" in text
    for path in (ROOT / "deploy").iterdir():
        if path.is_file() and path != UNIT:
            assert "orch-qa-worker" not in path.read_text(encoding="utf-8", errors="replace"), path
