import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest

from lib import cli
from lib.usage_readings import CLOCK_SKEW_SECONDS, RESET_HORIZON_SECONDS, ensure_table

MACHINES = """
orch_host: box
accounts:
  - {id: cursor-grok-ultra, harness: external, plan: ultra}
  - {id: codex-a, harness: codex}
devices:
  - name: box
    local: true
    slot_max: 1
    harnesses:
      codex: {bot: codex-bot, account: codex-a}
""".lstrip()

PRE_ACCOUNT_DDL = """
CREATE TABLE usage_readings (
    harness TEXT NOT NULL,
    bot TEXT NOT NULL,
    device TEXT NOT NULL,
    window TEXT NOT NULL,
    used REAL,
    resets_at INTEGER,
    observed_at REAL NOT NULL,
    job_id TEXT NOT NULL,
    absent INTEGER NOT NULL DEFAULT 0,
    source_at REAL,
    PRIMARY KEY (harness, bot, device, window)
)
"""

NOW = int(time.time())
RESETS = NOW + 3 * 24 * 3600
OBSERVED = NOW - 600


@pytest.fixture(autouse=True)
def clock_pinned_to_now(monkeypatch) -> None:
    monkeypatch.setattr(time, "time", lambda: float(NOW))


@pytest.fixture
def machines(tmp_path: Path) -> Path:
    path = tmp_path / "machines.yml"
    path.write_text(MACHINES, encoding="utf-8")
    return path


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "orch.db"
    with closing(sqlite3.connect(path)) as connection:
        ensure_table(connection)
        connection.commit()
    return path


def record(db: Path, machines: Path, *extra: str, **values: object) -> int:
    options = {
        "account": "cursor-grok-ultra",
        "window": "five_hour",
        "used": "0.25",
        "resets-at": str(RESETS),
        "observed-at": str(OBSERVED),
    } | {key.replace("_", "-"): value for key, value in values.items()}
    argv = ["usage", "record", "--db", str(db), "--machines", str(machines)]
    for key, value in options.items():
        if value is not None:
            argv += [f"--{key}", str(value)]
    return cli.main([*argv, *extra])


def rows(db: Path) -> list[tuple]:
    with closing(sqlite3.connect(db)) as connection:
        return connection.execute(
            "SELECT harness, bot, device, window, used, resets_at, observed_at, job_id,"
            " absent, source_at, account FROM usage_readings ORDER BY window"
        ).fetchall()


def dump(db: Path) -> str:
    with closing(sqlite3.connect(db)) as connection:
        return "\n".join(connection.iterdump())


def test_records_one_external_reading(db: Path, machines: Path, capsys) -> None:
    assert record(db, machines) == 0

    assert rows(db) == [
        (
            "external",
            "cursor-grok-ultra",
            "external",
            "five_hour",
            0.25,
            RESETS,
            float(OBSERVED),
            "orch-usage-record",
            0,
            float(OBSERVED),
            "cursor-grok-ultra",
        )
    ]
    out = capsys.readouterr().out
    assert "cursor-grok-ultra" in out and "five_hour" in out


def test_database_comes_from_orch_db(db: Path, machines: Path, monkeypatch) -> None:
    monkeypatch.setenv("ORCH_DB", str(db))

    code = cli.main(
        [
            "usage", "record", "--machines", str(machines), "--account", "cursor-grok-ultra",
            "--window", "seven_day", "--used", "0.5", "--resets-at", str(RESETS),
        ]
    )

    assert code == 0
    assert [(row[3], row[4]) for row in rows(db)] == [("seven_day", 0.5)]


def test_observed_at_defaults_to_now(db: Path, machines: Path) -> None:
    assert record(db, machines, observed_at=None) == 0

    (row,) = rows(db)
    assert row[6] == float(NOW)
    assert row[9] == float(NOW)


def test_weekly_is_stored_as_seven_day(db: Path, machines: Path) -> None:
    assert record(db, machines, window="weekly") == 0

    assert [row[3] for row in rows(db)] == ["seven_day"]


def test_an_older_reading_keeps_the_newer_row(db: Path, machines: Path, capsys) -> None:
    assert record(db, machines) == 0
    stored = rows(db)
    capsys.readouterr()

    assert record(db, machines, used="0.9", observed_at=str(OBSERVED - 60)) == 0

    assert rows(db) == stored
    assert "kept newer reading" in capsys.readouterr().out


def test_a_newer_reading_replaces_the_row(db: Path, machines: Path) -> None:
    assert record(db, machines) == 0

    assert record(db, machines, used="0.75", observed_at=str(OBSERVED + 60)) == 0

    assert [(row[4], row[6]) for row in rows(db)] == [(0.75, float(OBSERVED + 60))]


REFUSALS = {
    "unknown account": ({"account": "not-a-declared-account"}, (), "not-a-declared-account"),
    "non-external account": ({"account": "codex-a"}, (), "codex-a"),
    "implicit default account": ({"account": "codex-default"}, (), "codex-default"),
    "negative used": ({"used": "-0.1"}, (), "-0.1"),
    "used above one": ({"used": "1.5"}, (), "1.5"),
    "nan used": ({"used": "nan"}, (), "nan"),
    "infinite used": ({"used": "inf"}, (), "inf"),
    "non-numeric used": ({"used": "abc"}, (), "abc"),
    "reset past the horizon": (
        {"resets_at": str(NOW + RESET_HORIZON_SECONDS + 3600)},
        (),
        str(NOW + RESET_HORIZON_SECONDS + 3600),
    ),
    "reset not an integer": ({"resets_at": f"{RESETS}.5"}, (), f"{RESETS}.5"),
    "bad window": ({"window": "monthly"}, (), "monthly"),
    "future observed_at": (
        {"observed_at": str(NOW + CLOCK_SKEW_SECONDS + 600)},
        (),
        str(NOW + CLOCK_SKEW_SECONDS + 600),
    ),
    "nan observed_at": ({"observed_at": "nan"}, (), "nan"),
    "unknown option": ({}, ("--token", "sk-refused-token-77"), "sk-refused-token-77"),
}


@pytest.mark.parametrize("case", REFUSALS, ids=list(REFUSALS))
def test_refusal_exits_2_and_writes_nothing(db: Path, machines: Path, capsys, case: str) -> None:
    values, extra, refused = REFUSALS[case]
    before = dump(db)

    assert record(db, machines, *extra, **values) == 2

    assert len(capsys.readouterr().err.strip().splitlines()) == 1
    assert dump(db) == before
    assert refused not in dump(db)


def test_refused_values_never_reach_the_database(db: Path, machines: Path, capsys) -> None:
    assert record(db, machines) == 0
    stored = rows(db)

    for values, extra, _ in REFUSALS.values():
        assert record(db, machines, *extra, **values) == 2

    assert rows(db) == stored
    text = dump(db)
    for case, (_, _, refused) in REFUSALS.items():
        assert refused not in text, case
    assert "sk-refused-token-77" not in capsys.readouterr().err


def test_an_unmigrated_table_is_refused(tmp_path: Path, machines: Path, capsys) -> None:
    path = tmp_path / "orch.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(PRE_ACCOUNT_DDL)
        connection.commit()
    before = dump(path)

    assert record(path, machines) == 2

    err = capsys.readouterr().err
    assert "orch usage migrate" in err
    assert len(err.strip().splitlines()) == 1
    assert dump(path) == before


def test_a_missing_database_is_refused(tmp_path: Path, machines: Path) -> None:
    path = tmp_path / "missing.db"

    assert record(path, machines) == 2

    assert not path.exists()


def test_help_lists_usage_record(capsys) -> None:
    assert cli.main(["--help"]) == 0

    out = capsys.readouterr().out
    assert "\n  usage " in out
    assert "usage record --account ID --window" in out
