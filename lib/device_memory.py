from __future__ import annotations

MIN_AVAILABLE_BYTES = 8 * 1024**3
MAX_PSI_SOME_AVG10 = 10.0
MEMORY_WAIT = "memory-wait"
HOLD_SECONDS = 180.0
READ_TIMEOUT_SECONDS = 10.0
_GIB = 1024**3


def over_limit(meminfo: str | None, pressure: str | None) -> str | None:
    available = _available_bytes(meminfo)
    if available is not None and available < MIN_AVAILABLE_BYTES:
        return (
            f"{MEMORY_WAIT}: MemAvailable {available / _GIB:.1f} GiB"
            f" < {MIN_AVAILABLE_BYTES // _GIB} GiB"
        )
    avg10 = _some_avg10(pressure)
    if avg10 is not None and avg10 > MAX_PSI_SOME_AVG10:
        return f"{MEMORY_WAIT}: PSI some avg10 {avg10:g} > {MAX_PSI_SOME_AVG10:g}"
    return None


def _available_bytes(meminfo: str | None) -> int | None:
    for line in (meminfo or "").splitlines():
        fields = line.split()
        if fields[:1] == ["MemAvailable:"] and len(fields) > 1 and fields[1].isdigit():
            try:
                return int(fields[1]) * 1024
            except ValueError:
                return None
    return None


def _some_avg10(pressure: str | None) -> float | None:
    for line in (pressure or "").splitlines():
        fields = line.split()
        if fields[:1] == ["some"]:
            values = dict(field.split("=", 1) for field in fields[1:] if "=" in field)
            try:
                return float(values["avg10"])
            except (KeyError, ValueError):
                return None
    return None
