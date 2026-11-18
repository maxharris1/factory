from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from lib.qa_evidence import Recycled, record_evidence_dir, recycle, run_dir
from tests.test_dispatcher import setup

REPO = "Acme/Shop"
TICKET = "12"
DAY = 86400.0
NOW = 1_800_000_000.0


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = (tmp_path / "evidence").resolve()
    folder.mkdir()
    return folder


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    _dispatcher, queue = setup(tmp_path)
    connection = sqlite3.connect(queue.path)
    yield connection
    connection.close()


def _run(
    db: sqlite3.Connection,
    run_id: str,
    folder: str | Path | None,
    *,
    ticket: str = TICKET,
    arm: str = "primary",
    state: str = "reported",
    started_at: float = NOW - DAY,
) -> None:
    db.execute(
        """
        INSERT INTO qa_runs (
            run_id, repo, ticket_id, head, round, scope, runner, arm, job_id,
            state, started_at
        ) VALUES (?, ?, ?, ?, 1, 'full', 'codex', ?, ?, ?, ?)
        """,
        (run_id, REPO.lower(), ticket, run_id.ljust(40, "0"), arm, f"job-{run_id}", state, started_at),
    )
    if folder is not None:
        record_evidence_dir(db, run_id, Path(folder))
    db.commit()


def _closed(db: sqlite3.Connection, ticket: str, state: str, at: float) -> None:
    db.execute(
        "INSERT INTO ticket_state (repo, ticket_id, workflow_state) VALUES (?, ?, ?)",
        (REPO, ticket, state),
    )
    db.execute(
        "INSERT INTO ticket_transitions (repo, ticket_id, from_state, to_state, at, source) "
        "VALUES (?, ?, 'reviewing', ?, ?, 'test')",
        (REPO, ticket, state, at),
    )
    db.commit()


def _lane(folder: Path, lane: int, results: dict[str, str], files: list[str]) -> None:
    lane_dir = folder / f"lane-{lane}"
    lane_dir.mkdir(parents=True)
    scenarios = [{"id": sid, "result": result} for sid, result in results.items()]
    (lane_dir / "result.json").write_text(json.dumps({"lane": lane, "scenarios": scenarios}))
    (lane_dir / "manifest.json").write_text("{}")
    for rel in files:
        path = lane_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(rel.encode())


def _remaining(folder: Path) -> set[str]:
    return {
        path.relative_to(folder).as_posix()
        for path in folder.rglob("*")
        if path.is_file() or path.is_symlink()
    }


def _snapshot(folder: Path) -> dict[str, tuple[int, int, int]]:
    snap = {}
    for dirpath, dirnames, filenames in os.walk(folder):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            stat = path.lstat()
            snap[path.relative_to(folder).as_posix()] = (stat.st_mode, stat.st_size, stat.st_mtime_ns)
    return snap


SCENARIO_FILES = ["shot.png", "step.jpg", "video.webm", "trace.zip", "console.log", "network.json"]


def _files(*sids: str) -> list[str]:
    return [f"{sid}/{name}" for sid in sids for name in SCENARIO_FILES]


def test_run_dir_is_the_collect_layout(root: Path) -> None:
    assert run_dir(root, REPO, TICKET, "run-1") == root / "Acme" / "Shop" / TICKET / "run-1"


def test_passed_scenario_keeps_screenshots_and_video(db: sqlite3.Connection, root: Path) -> None:
    folder = run_dir(root, REPO, TICKET, "run-1")
    _lane(folder, 1, {"s-a": "verified"}, _files("s-a"))
    _run(db, "run-1", folder)

    report = recycle(db, root=root, repo=REPO, now=NOW)

    assert _remaining(folder) == {
        "lane-1/result.json",
        "lane-1/manifest.json",
        "lane-1/s-a/shot.png",
        "lane-1/s-a/step.jpg",
        "lane-1/s-a/video.webm",
    }
    prefix = "Acme/Shop/12/run-1/lane-1/s-a/"
    assert sorted(report.deleted) == sorted(prefix + name for name in ("console.log", "network.json", "trace.zip"))
    assert report.skipped == ()


def test_failed_and_needs_human_keep_everything(db: sqlite3.Connection, root: Path) -> None:
    folder = run_dir(root, REPO, TICKET, "run-1")
    results = {"s-f": "failed", "s-h": "needs-human", "s-i": "inconclusive"}
    _lane(folder, 1, results, _files(*results) + ["stray/trace.zip", "lane.log"])
    _run(db, "run-1", folder)
    before = _snapshot(folder)

    report = recycle(db, root=root, repo=REPO, now=NOW)

    assert report == Recycled(deleted=(), skipped=())
    assert _snapshot(folder) == before


def test_superseded_run_keeps_manifest_and_failed_artifacts(db: sqlite3.Connection, root: Path) -> None:
    old = run_dir(root, REPO, TICKET, "run-1")
    _lane(old, 1, {"s-a": "verified", "s-f": "failed", "s-h": "needs-human"}, _files("s-a", "s-f", "s-h"))
    _lane(old, 2, {"s-b": "verified"}, _files("s-b") + ["lane.log"])
    _run(db, "run-1", old, started_at=NOW - 3 * DAY)
    shootout = run_dir(root, REPO, TICKET, "run-2")
    _lane(shootout, 1, {"s-a": "verified"}, _files("s-a"))
    _run(db, "run-2", shootout, arm="shootout", started_at=NOW - 2 * DAY)
    new = run_dir(root, REPO, TICKET, "run-3")
    _lane(new, 1, {"s-a": "failed"}, _files("s-a"))
    _run(db, "run-3", new, started_at=NOW - DAY)
    new_before = _snapshot(new)

    report = recycle(db, root=root, repo=REPO, now=NOW)

    assert _remaining(old) == {
        "lane-1/result.json",
        "lane-1/manifest.json",
        "lane-2/result.json",
        "lane-2/manifest.json",
        *(f"lane-1/{rel}" for rel in _files("s-f")),
    }
    assert _remaining(shootout) == {
        "lane-1/result.json",
        "lane-1/manifest.json",
        "lane-1/s-a/shot.png",
        "lane-1/s-a/step.jpg",
        "lane-1/s-a/video.webm",
    }
    assert _snapshot(new) == new_before
    assert report.skipped == ()


def test_running_newer_run_does_not_supersede(db: sqlite3.Connection, root: Path) -> None:
    old = run_dir(root, REPO, TICKET, "run-1")
    _lane(old, 1, {"s-f": "needs-human"}, _files("s-f"))
    _run(db, "run-1", old, started_at=NOW - 3 * DAY)
    _run(db, "run-2", None, state="running", started_at=NOW - DAY)
    before = _snapshot(old)

    recycle(db, root=root, repo=REPO, now=NOW)

    assert _snapshot(old) == before


def test_closed_over_7_days_keeps_manifests_only(db: sqlite3.Connection, root: Path) -> None:
    gone = run_dir(root, REPO, "12", "run-1")
    _lane(gone, 1, {"s-a": "verified", "s-f": "failed"}, _files("s-a", "s-f") + ["lane.log"])
    _lane(gone, 2, {"s-h": "needs-human"}, _files("s-h"))
    _run(db, "run-1", gone, ticket="12")
    _closed(db, "12", "merged", NOW - 8 * DAY)
    abandoned = run_dir(root, REPO, "13", "run-2")
    _lane(abandoned, 1, {"s-f": "failed"}, _files("s-f"))
    _run(db, "run-2", abandoned, ticket="13")
    _closed(db, "13", "abandoned", NOW - 7 * DAY - 1)
    recent = run_dir(root, REPO, "14", "run-3")
    _lane(recent, 1, {"s-f": "failed"}, _files("s-f"))
    _run(db, "run-3", recent, ticket="14")
    _closed(db, "14", "merged", NOW - 6 * DAY)
    recent_before = _snapshot(recent)

    report = recycle(db, root=root, repo=REPO, now=NOW)

    manifests = {"lane-1/result.json", "lane-1/manifest.json"}
    assert _remaining(gone) == manifests | {"lane-2/result.json", "lane-2/manifest.json"}
    assert _remaining(abandoned) == manifests
    assert _snapshot(recent) == recent_before
    assert report.skipped == ()


@pytest.mark.parametrize("where", ["sibling", "symlink", "dotdot"])
def test_folder_outside_root_is_skipped_and_reported(
    db: sqlite3.Connection, root: Path, tmp_path: Path, where: str
) -> None:
    outside = tmp_path.resolve() / "outside" / "Acme" / "Shop" / TICKET / "run-1"
    _lane(outside, 1, {"s-a": "verified"}, _files("s-a"))
    (tmp_path / "outside" / "private.txt").write_text("private marker")
    inside = run_dir(root, REPO, TICKET, "run-1")
    if where == "sibling":
        folder = str(outside)
    elif where == "symlink":
        inside.parent.mkdir(parents=True)
        inside.symlink_to(outside, target_is_directory=True)
        folder = str(inside)
    else:
        folder = f"{root}/../outside/Acme/Shop/{TICKET}/run-1"
    _run(db, "run-1", folder)
    _closed(db, TICKET, "merged", NOW - 30 * DAY)
    before = _snapshot(tmp_path)

    report = recycle(db, root=root, repo=REPO, now=NOW)

    assert report.deleted == report.removed == ()
    assert [run_id for run_id, _reason in report.skipped] == ["run-1"]
    assert report.skipped[0][1]
    assert _snapshot(tmp_path) == before


def test_folder_for_another_run_id_is_skipped(db: sqlite3.Connection, root: Path) -> None:
    folder = run_dir(root, REPO, TICKET, "run-9")
    _lane(folder, 1, {"s-a": "verified"}, _files("s-a"))
    _run(db, "run-1", folder)
    before = _snapshot(root)

    report = recycle(db, root=root, repo=REPO, now=NOW)

    assert [run_id for run_id, _reason in report.skipped] == ["run-1"]
    assert _snapshot(root) == before


def test_symlink_inside_a_run_is_reported_and_left(db: sqlite3.Connection, root: Path, tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "trace.zip").write_text("outside trace")
    folder = run_dir(root, REPO, TICKET, "run-1")
    _lane(folder, 1, {"s-a": "verified"}, _files("s-a"))
    (folder / "lane-1" / "s-a" / "link.zip").symlink_to(target / "trace.zip")
    (folder / "lane-1" / "linked").symlink_to(target, target_is_directory=True)
    _run(db, "run-1", folder)

    report = recycle(db, root=root, repo=REPO, now=NOW)

    assert (folder / "lane-1" / "s-a" / "link.zip").is_symlink()
    assert (folder / "lane-1" / "linked").is_symlink()
    assert (target / "trace.zip").read_text() == "outside trace"
    assert not (folder / "lane-1" / "s-a" / "trace.zip").exists()
    assert sorted(run_id for run_id, _reason in report.skipped) == ["run-1", "run-1"]


def _failed_run(db: sqlite3.Connection, root: Path, run_id: str, ticket: str, started_at: float, size: int) -> Path:
    folder = run_dir(root, REPO, ticket, run_id)
    _lane(folder, 1, {"s-f": "failed"}, [])
    (folder / "lane-1" / "s-f").mkdir()
    (folder / "lane-1" / "s-f" / "video.webm").write_bytes(b"v" * size)
    _run(db, run_id, folder, ticket=ticket, started_at=started_at)
    return folder


def _bytes(folder: Path) -> int:
    return sum(path.lstat().st_size for path in folder.rglob("*") if path.is_file() and not path.is_symlink())


def test_over_budget_deletes_merged_runs_oldest_first(db: sqlite3.Connection, root: Path) -> None:
    open_old = _failed_run(db, root, "o-1", "13", NOW - 9 * DAY, 5000)
    merged = [_failed_run(db, root, f"m-{n}", "12", NOW - (6 - n) * DAY, 1000 * n) for n in (1, 2, 3)]
    open_new = _failed_run(db, root, "o-2", "13", NOW - DAY, 7000)
    _closed(db, "12", "merged", NOW - DAY)
    opened = {folder: _snapshot(folder) for folder in (open_old, open_new)}
    total = sum(_bytes(folder) for folder in [open_old, open_new, *merged])
    budget = total - _bytes(merged[0]) - _bytes(merged[1])

    report = recycle(db, root=root, repo=REPO, now=NOW, budget_bytes=budget)

    assert report == Recycled(deleted=(), skipped=(), removed=("Acme/Shop/12/m-1", "Acme/Shop/12/m-2"))
    assert [folder.exists() for folder in merged] == [False, False, True]
    assert {folder: _snapshot(folder) for folder in opened} == opened

    report = recycle(db, root=root, repo=REPO, now=NOW, budget_bytes=1)

    assert report == Recycled(deleted=(), skipped=(), removed=("Acme/Shop/12/m-3",))
    assert {folder: _snapshot(folder) for folder in opened} == opened
    assert recycle(db, root=root, repo=REPO, now=NOW, budget_bytes=1) == Recycled(deleted=(), skipped=())
    assert db.execute("SELECT COUNT(*) FROM qa_runs WHERE evidence_dir IS NOT NULL").fetchone() == (5,)


@pytest.mark.parametrize("level", ["run", "ticket"])
def test_budget_never_follows_a_link_out(db: sqlite3.Connection, root: Path, tmp_path: Path, level: str) -> None:
    outside = tmp_path.resolve() / "outside"
    _lane(outside / "run-1", 1, {"s-a": "verified"}, _files("s-a"))
    link = run_dir(root, REPO, TICKET, "run-1")
    if level == "run":
        link.parent.mkdir(parents=True)
        link.symlink_to(outside / "run-1", target_is_directory=True)
    else:
        link.parent.parent.mkdir(parents=True)
        link.parent.symlink_to(outside, target_is_directory=True)
    _run(db, "run-1", link, started_at=NOW - 3 * DAY)
    _closed(db, TICKET, "merged", NOW - DAY)
    _failed_run(db, root, "o-1", "13", NOW - DAY, 5000)
    before = _snapshot(outside)

    report = recycle(db, root=root, repo=REPO, now=NOW, budget_bytes=1)

    assert report.deleted == report.removed == ()
    assert [run_id for run_id, _reason in report.skipped] == ["run-1"]
    assert report.skipped[0][1]
    assert _snapshot(outside) == before


def test_missing_run_folder_is_nothing_to_do(db: sqlite3.Connection, root: Path) -> None:
    _run(db, "run-1", run_dir(root, REPO, TICKET, "run-1"))
    _closed(db, TICKET, "merged", NOW - 30 * DAY)

    assert recycle(db, root=root, repo=REPO, now=NOW, budget_bytes=1) == Recycled(deleted=(), skipped=())


def test_record_evidence_dir_sets_the_folder_once(db: sqlite3.Connection, root: Path) -> None:
    first = run_dir(root, REPO, TICKET, "run-1")
    _run(db, "run-1", first)
    record_evidence_dir(db, "run-1", root / "elsewhere")

    assert db.execute("SELECT evidence_dir FROM qa_runs WHERE run_id = 'run-1'").fetchall() == [(str(first),)]


def test_run_without_a_folder_is_ignored(db: sqlite3.Connection, root: Path) -> None:
    _run(db, "run-1", None)

    assert recycle(db, root=root, repo=REPO, now=NOW) == Recycled(deleted=(), skipped=())


def test_old_database_gains_the_column(tmp_path: Path) -> None:
    path = tmp_path / "d.db"
    setup(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE qa_runs DROP COLUMN evidence_dir")
    setup(tmp_path)
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(qa_runs)")}
    assert "evidence_dir" in columns
