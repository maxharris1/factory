from datetime import datetime, timezone


def read(value: float | str) -> float:
    try:
        return float(value)
    except ValueError:
        moment = datetime.fromisoformat(str(value))
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).timestamp()


def sql(column: str) -> str:
    return (
        f"CASE WHEN typeof({column}) IN ('integer', 'real') THEN {column} "
        f"WHEN {column} GLOB '[0-9][0-9][0-9][0-9]-*' THEN CAST(strftime('%s', {column}) AS REAL) "
        f"ELSE CAST({column} AS REAL) END"
    )
