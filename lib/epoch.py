import sqlite3
from datetime import datetime, timezone

SQL_FUNCTION = "epoch"


def read(value: float | str) -> float:
    try:
        return float(value)
    except ValueError:
        moment = datetime.fromisoformat(str(value))
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).timestamp()


def cell(value: object) -> float | None:
    try:
        return read(value)
    except (TypeError, ValueError):
        return None


def register(connection: sqlite3.Connection) -> None:
    connection.create_function(SQL_FUNCTION, 1, cell, deterministic=True)


def sql(column: str) -> str:
    return f"{SQL_FUNCTION}({column})"
