import contextlib
import sqlite3

import pytest

from lib import epoch

INSTANT = 1795731358.0
FORMS = [
    INSTANT,
    int(INSTANT),
    str(INSTANT),
    "2026-11-26 22:15:58",
    "2026-11-26T22:15:58",
    "2026-11-26T22:15:58Z",
    "2026-11-26T17:15:58-05:00",
]


@pytest.mark.parametrize("value", FORMS)
def test_read_takes_an_epoch_or_a_datetime_string_as_one_instant(value: object) -> None:
    assert epoch.read(value) == INSTANT


def test_read_keeps_fractional_seconds() -> None:
    assert epoch.read("2026-11-26 22:15:58.25") == INSTANT + 0.25


def test_read_refuses_text_that_is_no_time() -> None:
    with pytest.raises(ValueError):
        epoch.read("soon")


def test_read_refuses_nothing() -> None:
    with pytest.raises(TypeError):
        epoch.read(None)


def _table(values: list) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    epoch.register(connection)
    connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v)")
    connection.executemany("INSERT INTO t (v) VALUES (?)", [(value,) for value in values])
    return connection


@pytest.mark.parametrize("value", [*FORMS, INSTANT + 0.25, "2026-11-26 22:15:58.25", "2026-11-26T22:15:58.250000Z"])
def test_sql_reads_each_storage_form_as_the_instant_python_reads(value: object) -> None:
    with contextlib.closing(_table([value])) as connection:
        (read,) = connection.execute(f"SELECT {epoch.sql('v')} FROM t").fetchone()
    assert read == epoch.read(value)


@pytest.mark.parametrize("value", [None, "soon"])
def test_sql_reads_a_value_that_is_no_time_as_null(value: object) -> None:
    with contextlib.closing(_table([value])) as connection:
        assert connection.execute(f"SELECT {epoch.sql('v')} FROM t").fetchone() == (None,)


def test_sql_orders_fractions_of_a_second_across_storage_forms() -> None:
    values = ["2026-11-26 22:15:58.750", INSTANT + 0.25]
    with contextlib.closing(_table(values)) as connection:
        ordered = [row[0] for row in connection.execute(f"SELECT v FROM t ORDER BY {epoch.sql('v')} DESC")]
    assert ordered == values


def test_sql_orders_mixed_storage_by_instant() -> None:
    values = [INSTANT + 60, "2026-11-26 22:15:58", INSTANT - 60, "2026-11-26T22:17:58Z"]
    by_instant = [INSTANT - 60, "2026-11-26 22:15:58", INSTANT + 60, "2026-11-26T22:17:58Z"]
    with contextlib.closing(_table(values)) as connection:
        plain = [row[0] for row in connection.execute("SELECT v FROM t ORDER BY v")]
        ordered = [row[0] for row in connection.execute(f"SELECT v FROM t ORDER BY {epoch.sql('v')}")]
    assert plain != by_instant, "plain ORDER BY sorts every text above every number"
    assert ordered == by_instant
