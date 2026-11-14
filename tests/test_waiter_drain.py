"""#219: a waiter told to drain claims nothing until it runs the new commit."""

from __future__ import annotations

from pathlib import Path

from lib import waiter as waiter_module
from tests.test_waiter import FakeSpawn, enqueue, make_queue, make_waiter, state


def draining_waiter(tmp_path: Path, log: list[str]):
    db, queue = make_queue(tmp_path)
    spawn = FakeSpawn()
    waiter = make_waiter(tmp_path, queue, db, spawn, log=log)
    waiter.drain = tmp_path / "waiter-claude-bot.drain"
    waiter.version = "aaaaaaaaaaaa"
    return db, queue, spawn, waiter


def test_a_draining_waiter_claims_nothing_and_logs_once(tmp_path: Path) -> None:
    log: list[str] = []
    db, queue, spawn, waiter = draining_waiter(tmp_path, log)
    first = enqueue(queue, "a")
    assert waiter.step() == first
    waiter.drain.write_text("bbbbbbbbbbbb\n")
    second = enqueue(queue, "b")
    assert waiter.step() is None
    assert waiter.step() is None
    assert state(db, second) == "queued"
    assert len(spawn.calls) == 1
    assert [line for line in log if "draining" in line] == [
        "draining for bbbbbbbbbbbb: no new claims until restarted (1 running jobs)"]


def test_a_draining_waiter_still_reaps_its_runs(tmp_path: Path) -> None:
    _, queue, spawn, waiter = draining_waiter(tmp_path, [])
    enqueue(queue, "a")
    waiter.step()
    waiter.drain.write_text("bbbbbbbbbbbb\n")
    spawn.children[0].returncode = 0
    assert waiter.step() is None
    assert waiter.running() == set()


def test_a_waiter_on_the_marked_commit_ignores_the_marker(tmp_path: Path) -> None:
    _, queue, _, waiter = draining_waiter(tmp_path, [])
    waiter.drain.write_text("aaaaaaaaaaaa\n")
    job = enqueue(queue, "a")
    assert waiter.step() == job


def test_claims_resume_when_the_marker_goes(tmp_path: Path) -> None:
    _, queue, _, waiter = draining_waiter(tmp_path, [])
    waiter.drain.write_text("bbbbbbbbbbbb\n")
    job = enqueue(queue, "a")
    assert waiter.step() is None
    waiter.drain.unlink()
    assert waiter.step() == job


def test_the_marker_path_follows_the_service_then_the_state_dir(tmp_path: Path) -> None:
    assert waiter_module.drain_marker(
        "codex-bot", {"ORCH_WAITER_DRAIN": "/srv/x.drain"}) == Path("/srv/x.drain")
    assert waiter_module.drain_marker(
        "codex-bot", {"ORCH_STATE_DIR": "/srv/state"}) == Path("/srv/state/waiter-codex-bot.drain")
    assert waiter_module.drain_marker("codex-bot", {}) == (
        Path.home() / ".local/state/factory/waiter-codex-bot.drain")
