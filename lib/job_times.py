import sqlite3
from typing import NamedTuple

_COLUMNS = (("created_at", False), ("last_activity_at", False), ("finished_at", True))


def _bad(value: str, nullable: bool) -> str:
    types = "'integer', 'real', 'null'" if nullable else "'integer', 'real'"
    return f"(typeof({value}) NOT IN ({types}) OR abs({value}) = 9e999)"


def _raise(column: str, test: str) -> str:
    return f"SELECT RAISE(ABORT, 'jobs.{column} takes epoch seconds') WHERE {test};"


def install(connection: sqlite3.Connection) -> None:
    inserts = "\n".join(_raise(name, _bad(f"NEW.{name}", nullable)) for name, nullable in _COLUMNS)
    updates = "\n".join(
        _raise(name, f"NEW.{name} IS NOT OLD.{name} AND {_bad(f'NEW.{name}', nullable)}")
        for name, nullable in _COLUMNS
    )
    columns = ", ".join(name for name, _ in _COLUMNS)
    connection.execute(
        f"CREATE TRIGGER IF NOT EXISTS jobs_times_insert BEFORE INSERT ON jobs BEGIN\n{inserts}\nEND"
    )
    connection.execute(
        f"CREATE TRIGGER IF NOT EXISTS jobs_times_update BEFORE UPDATE OF {columns} ON jobs BEGIN\n{updates}\nEND"
    )


class BadTime(NamedTuple):
    job: str
    column: str
    stored: str


class BadTimes(NamedTuple):
    shown: tuple[BadTime, ...] = ()
    total: int = 0


def bad_times(connection: sqlite3.Connection, limit: int) -> BadTimes:
    found = " UNION ALL ".join(
        f"SELECT id, '{name}', "
        f"CASE WHEN typeof({name}) IN ('integer', 'real') THEN 'infinity' ELSE typeof({name}) END "
        f"FROM jobs WHERE {_bad(name, nullable)}"
        for name, nullable in _COLUMNS
    )
    rows = connection.execute(
        f"""
        WITH found(job, name, stored) AS ({found}),
        shown AS (SELECT * FROM found ORDER BY job, name LIMIT ?)
        SELECT (SELECT count(*) FROM found), shown.* FROM (SELECT 1) LEFT JOIN shown
        ORDER BY shown.job, shown.name
        """,
        (limit,),
    ).fetchall()
    return BadTimes(
        tuple(BadTime(str(job), name, stored) for _, job, name, stored in rows if name is not None),
        rows[0][0],
    )
