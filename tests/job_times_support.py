import sqlite3


def storing_bad_times(connection: sqlite3.Connection) -> sqlite3.Connection:
    connection.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_TRIGGER, False)
    return connection
