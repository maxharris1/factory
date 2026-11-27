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
    connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v)")
    connection.executemany("INSERT INTO t (v) VALUES (?)", [(value,) for value in values])
    return connection


@pytest.mark.parametrize("value", [*FORMS, "2026-11-26 22:15:58.25"])
def test_sql_reads_each_storage_form_as_the_instant_to_the_second(value: object) -> None:
    with contextlib.closing(_table([value])) as connection:
        (read,) = connection.execute(f"SELECT {epoch.sql('v')} FROM t").fetchone()
    assert read == INSTANT


def test_sql_orders_mixed_storage_by_instant() -> None:
    values = [INSTANT + 60, "2026-11-26 22:15:58", INSTANT - 60, "2026-11-26T22:17:58Z"]
    by_instant = [INSTANT - 60, "2026-11-26 22:15:58", INSTANT + 60, "2026-11-26T22:17:58Z"]
    with contextlib.closing(_table(values)) as connection:
        plain = [row[0] for row in connection.execute("SELECT v FROM t ORDER BY v")]
        ordered = [row[0] for row in connection.execute(f"SELECT v FROM t ORDER BY {epoch.sql('v')}")]
    assert plain != by_instant, "plain ORDER BY sorts every text above every number"
    assert ordered == by_instant
